"""Unit tests for parts/01_client.py using httpx.MockTransport (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

TOKEN = "sekret-token-abc123"
BASE = "http://honcho.test"
WS = "ws1"
W = f"/v3/workspaces/{WS}"


class Recorder:
    """MockTransport handler: records requests, answers from a route table or a queue."""

    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request, len(self.requests))

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def body(self, i: int = -1):
        return json.loads(self.requests[i].content)


def make(client_mod, responder):
    rec = Recorder(responder)
    c = client_mod.HonchoClient(BASE, TOKEN, WS, transport=httpx.MockTransport(rec))
    c.backoff = 0.0
    return c, rec


def ok(payload=None, status=200):
    return lambda req, n: httpx.Response(status, json=payload)


# ----------------------------------------------------------------- basics
async def test_health_true_false(client_mod):
    c, rec = make(client_mod, ok({"status": "ok"}))
    assert await c.health() is True
    assert rec.last.url.path == "/health"
    assert rec.last.headers["authorization"] == f"Bearer {TOKEN}"
    c2, rec2 = make(client_mod, lambda r, n: httpx.Response(500, json={"detail": "x"}))
    assert await c2.health() is False
    assert len(rec2.requests) == 1


async def test_ensure_workspace(client_mod):
    c, rec = make(client_mod, ok({"id": WS}, 201))
    await c.ensure_workspace()
    assert rec.last.method == "POST" and rec.last.url.path == "/v3/workspaces"
    assert rec.body() == {"id": WS}


async def test_ensure_peer_shapes(client_mod):
    c, rec = make(client_mod, ok({"id": "p1"}, 201))
    await c.ensure_peer("p1", metadata={"a": 1}, configuration={"observe_me": True})
    assert rec.last.url.path == f"{W}/peers"
    assert rec.body() == {"id": "p1", "metadata": {"a": 1}, "configuration": {"observe_me": True}}
    await c.ensure_peer("p2")
    assert rec.body() == {"id": "p2"}


async def test_ensure_session(client_mod):
    c, rec = make(client_mod, ok({"id": "s1"}, 201))
    peers = {"u": {"observe_me": True, "observe_others": False}}
    await c.ensure_session("s1", peers, metadata={"k": "v"})
    assert rec.last.url.path == f"{W}/sessions"
    assert rec.body() == {"id": "s1", "peers": peers, "metadata": {"k": "v"}}


async def test_ensure_caching(client_mod):
    c, rec = make(client_mod, ok({}, 201))
    for _ in range(3):
        await c.ensure_workspace()
        await c.ensure_peer("p1")
        await c.ensure_session("s1", {})
    assert len(rec.requests) == 3
    await c.ensure_peer("p2")
    assert len(rec.requests) == 4
    # cache is process-wide: a second client with same config hits no network
    c2, rec2 = make(client_mod, ok({}, 201))
    await c2.ensure_peer("p1")
    assert rec2.requests == []
    # different workspace -> separate key
    c3 = client_mod.HonchoClient(BASE, TOKEN, "other", transport=httpx.MockTransport(rec2))
    await c3.ensure_peer("p1")
    assert len(rec2.requests) == 1


async def test_ensure_cache_expires_and_failure_not_cached(client_mod):
    c, rec = make(client_mod, ok({}, 201))
    await c.ensure_peer("p1")
    key = c._cache_key("p1")
    client_mod.HonchoClient._known_peers[key] = 0.0  # expired
    await c.ensure_peer("p1")
    assert len(rec.requests) == 2
    bad, _ = make(client_mod, lambda r, n: httpx.Response(401, json={"detail": "nope"}))
    with pytest.raises(client_mod.HonchoError):
        await bad.ensure_peer("zz")
    assert bad._cache_key("zz") not in client_mod.HonchoClient._known_peers


# ----------------------------------------------------------------- messages
async def test_add_messages(client_mod):
    msgs = [{"peer_id": "u", "content": "hi", "metadata": {"m": 1}}]
    c, rec = make(client_mod, ok([{"id": "m1", "content": "hi"}], 201))
    out = await c.add_messages("s 1", msgs)
    assert out == [{"id": "m1", "content": "hi"}]
    assert rec.last.method == "POST"
    assert rec.last.url.raw_path.decode() == f"{W}/sessions/s%201/messages"
    assert rec.body() == {"messages": msgs}


async def test_add_messages_never_retried(client_mod):
    c, rec = make(client_mod, lambda r, n: httpx.Response(500, json={"detail": "boom"}))
    with pytest.raises(client_mod.HonchoError) as ei:
        await c.add_messages("s1", [{"peer_id": "u", "content": "x"}])
    assert ei.value.status == 500
    assert len(rec.requests) == 1


async def test_add_messages_transport_error_not_retried(client_mod):
    def boom(req, n):
        raise httpx.ConnectError("down")

    c, rec = make(client_mod, boom)
    with pytest.raises(client_mod.HonchoError) as ei:
        await c.add_messages("s1", [])
    assert ei.value.status is None
    assert len(rec.requests) == 1


# ----------------------------------------------------------------- context
async def test_get_peer_context(client_mod):
    payload = {"peer_id": "p", "target_id": "p", "representation": "likes tea", "peer_card": ["a"]}
    c, rec = make(client_mod, ok(payload))
    assert await c.get_peer_context("p", search_query="tea", max_conclusions=5, target="t") == payload
    assert rec.last.method == "GET" and rec.last.url.path == f"{W}/peers/p/context"
    q = dict(rec.last.url.params)
    assert q == {"search_query": "tea", "max_conclusions": "5", "target": "t"}
    await c.get_peer_context("p")
    assert dict(rec.last.url.params) == {"max_conclusions": "25"}


async def test_get_session_context(client_mod):
    payload = {"id": "s", "messages": [], "summary": None, "peer_representation": None, "peer_card": None}
    c, rec = make(client_mod, ok(payload))
    assert await c.get_session_context("s", tokens=500, peer_target="p", search_query="q") == payload
    assert rec.last.url.path == f"{W}/sessions/s/context"
    assert dict(rec.last.url.params) == {
        "tokens": "500", "peer_target": "p", "search_query": "q", "summary": "true"}
    await c.get_session_context("s", summary=False)
    assert dict(rec.last.url.params) == {"summary": "false"}


# ----------------------------------------------------------------- card / representation
async def test_get_card(client_mod):
    c, rec = make(client_mod, ok({"peer_card": ["x", "y"]}))
    assert await c.get_card("p") == ["x", "y"]
    assert rec.last.method == "GET" and rec.last.url.path == f"{W}/peers/p/card"
    c2, _ = make(client_mod, ok({"peer_card": None}))
    assert await c2.get_card("p") == []


async def test_set_card(client_mod):
    c, rec = make(client_mod, ok({"peer_card": ["a"]}))
    await c.set_card("p", ["a"])
    assert rec.last.method == "PUT" and rec.last.url.path == f"{W}/peers/p/card"
    assert rec.body() == {"peer_card": ["a"]}


async def test_representation(client_mod):
    c, rec = make(client_mod, ok({"representation": "rep"}))
    assert await c.representation("p", search_query="q", max_conclusions=3) == "rep"
    assert rec.last.method == "POST" and rec.last.url.path == f"{W}/peers/p/representation"
    assert rec.body() == {"search_query": "q", "max_conclusions": 3}
    await c.representation("p")
    assert rec.body() == {"max_conclusions": 25}
    c2, _ = make(client_mod, ok({"representation": ""}))
    assert await c2.representation("p") == ""


async def test_chat(client_mod):
    c, rec = make(client_mod, ok({"content": "answer"}))
    assert await c.chat("p", "who?", reasoning_level="medium", session_id="s") == "answer"
    assert rec.last.url.path == f"{W}/peers/p/chat"
    assert rec.body() == {"query": "who?", "stream": False, "reasoning_level": "medium", "session_id": "s"}
    await c.chat("p", "q")
    assert rec.body() == {"query": "q", "stream": False, "reasoning_level": "low"}
    c2, _ = make(client_mod, ok({"content": None}))
    assert await c2.chat("p", "q") == ""


# ----------------------------------------------------------------- conclusions
async def test_create_conclusion(client_mod):
    c, rec = make(client_mod, ok([{"id": "c1"}], 201))
    assert await c.create_conclusion("o", "d", "fact", session_id="s") == [{"id": "c1"}]
    assert rec.last.url.path == f"{W}/conclusions"
    assert rec.body() == {"conclusions": [
        {"content": "fact", "observer_id": "o", "observed_id": "d", "session_id": "s"}]}
    await c.create_conclusion("o", "d", "f2")
    assert "session_id" not in rec.body()["conclusions"][0]


async def test_create_conclusion_not_retried(client_mod):
    c, rec = make(client_mod, lambda r, n: httpx.Response(500, json={"detail": "x"}))
    with pytest.raises(client_mod.HonchoError):
        await c.create_conclusion("o", "d", "f")
    assert len(rec.requests) == 1


async def test_list_conclusions(client_mod):
    c, rec = make(client_mod, ok({"items": [{"id": "c1"}], "total": 1, "page": 2, "size": 10, "pages": 1}))
    assert await c.list_conclusions("o", "d", page=2, size=10) == [{"id": "c1"}]
    assert rec.last.method == "POST" and rec.last.url.path == f"{W}/conclusions/list"
    assert dict(rec.last.url.params) == {"page": "2", "size": "10"}
    assert rec.body() == {"filters": {"observer_id": "o", "observed_id": "d"}}


async def test_query_conclusions(client_mod):
    c, rec = make(client_mod, ok([{"id": "c1"}]))
    assert await c.query_conclusions("o", "d", "q", top_k=4) == [{"id": "c1"}]
    assert rec.last.url.path == f"{W}/conclusions/query"
    assert rec.body() == {"query": "q", "top_k": 4, "filters": {"observer_id": "o", "observed_id": "d"}}


async def test_delete_conclusion(client_mod):
    c, rec = make(client_mod, lambda r, n: httpx.Response(204))
    assert await c.delete_conclusion("c1") is None
    assert rec.last.method == "DELETE" and rec.last.url.path == f"{W}/conclusions/c1"


# ----------------------------------------------------------------- misc
async def test_schedule_dream(client_mod):
    c, rec = make(client_mod, lambda r, n: httpx.Response(202))
    await c.schedule_dream("o", observed="d", session_id="s")
    assert rec.last.url.path == f"{W}/schedule_dream"
    assert rec.body() == {"observer": "o", "dream_type": "omni", "observed": "d", "session_id": "s"}
    await c.schedule_dream("o")
    assert rec.body() == {"observer": "o", "dream_type": "omni"}


async def test_queue_status(client_mod):
    payload = {"total_work_units": 3, "pending_work_units": 1}
    c, rec = make(client_mod, ok(payload))
    assert await c.queue_status() == payload
    assert rec.last.method == "GET" and rec.last.url.path == f"{W}/queue/status"


async def test_delete_session_clears_cache(client_mod):
    c, rec = make(client_mod, lambda r, n: httpx.Response(204 if r.method == "DELETE" else 201, json={}))
    await c.ensure_session("s1", {})
    assert c._cache_key("s1") in client_mod.HonchoClient._known_sessions
    await c.delete_session("s1")
    assert rec.last.method == "DELETE" and rec.last.url.path == f"{W}/sessions/s1"
    assert c._cache_key("s1") not in client_mod.HonchoClient._known_sessions


# ----------------------------------------------------------------- errors
async def test_401_raises_honcho_error(client_mod):
    c, rec = make(client_mod, lambda r, n: httpx.Response(401, json={"detail": "No access token provided"}))
    with pytest.raises(client_mod.HonchoError) as ei:
        await c.get_card("p")
    assert ei.value.status == 401
    assert ei.value.detail == "No access token provided"
    assert len(rec.requests) == 1  # 4xx never retried


async def test_500_raises_honcho_error_after_retries(client_mod):
    c, rec = make(client_mod, lambda r, n: httpx.Response(500, json={"detail": "An unexpected error occurred"}))
    with pytest.raises(client_mod.HonchoError) as ei:
        await c.get_card("p")
    assert ei.value.status == 500
    assert len(rec.requests) == 3  # 1 + 2 retries


async def test_non_idempotent_post_500_single_attempt(client_mod):
    c, rec = make(client_mod, lambda r, n: httpx.Response(500, json={"detail": "x"}))
    with pytest.raises(client_mod.HonchoError):
        await c.chat("p", "q")
    assert len(rec.requests) == 1


async def test_422_list_detail_serialised(client_mod):
    c, _ = make(client_mod, lambda r, n: httpx.Response(422, json={"detail": [{"msg": "bad"}]}))
    with pytest.raises(client_mod.HonchoError) as ei:
        await c.get_card("p")
    assert ei.value.status == 422 and "bad" in ei.value.detail


async def test_non_json_error_body(client_mod):
    c, _ = make(client_mod, lambda r, n: httpx.Response(502, text="Bad Gateway"))
    c.max_retries = 0
    with pytest.raises(client_mod.HonchoError) as ei:
        await c.get_card("p")
    assert ei.value.detail == "Bad Gateway"


# ----------------------------------------------------------------- retries
async def test_get_retried_then_succeeds(client_mod):
    def resp(req, n):
        return httpx.Response(503, json={"detail": "x"}) if n < 3 else httpx.Response(200, json={"peer_card": ["ok"]})

    c, rec = make(client_mod, resp)
    assert await c.get_card("p") == ["ok"]
    assert len(rec.requests) == 3


async def test_get_retried_on_connect_error(client_mod):
    def resp(req, n):
        if n == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"id": "s", "messages": []})

    c, rec = make(client_mod, resp)
    assert (await c.get_session_context("s"))["id"] == "s"
    assert len(rec.requests) == 2


async def test_get_connect_error_exhausts(client_mod):
    def resp(req, n):
        raise httpx.ConnectError("refused")

    c, rec = make(client_mod, resp)
    with pytest.raises(client_mod.HonchoError) as ei:
        await c.queue_status()
    assert ei.value.status is None
    assert len(rec.requests) == 3


async def test_ensure_get_or_create_retried(client_mod):
    c, rec = make(client_mod, lambda r, n: httpx.Response(500, json={"detail": "x"}) if n == 1 else httpx.Response(201, json={}))
    await c.ensure_peer("p1")
    assert len(rec.requests) == 2


# ----------------------------------------------------------------- token safety
async def test_token_never_in_error_text(client_mod):
    # server echoing the token in its body must be scrubbed
    c, _ = make(client_mod, lambda r, n: httpx.Response(401, json={"detail": f"bad token {TOKEN}"}))
    with pytest.raises(client_mod.HonchoError) as ei:
        await c.get_card("p")
    e = ei.value
    assert TOKEN not in str(e) and TOKEN not in e.detail and TOKEN not in repr(e)

    def boom(req, n):
        raise httpx.ConnectError(f"cannot connect with {TOKEN}")

    c2, _ = make(client_mod, boom)
    with pytest.raises(client_mod.HonchoError) as ei2:
        await c2.add_messages("s", [])
    assert TOKEN not in str(ei2.value) and TOKEN not in repr(ei2.value)
    assert ei2.value.__cause__ is None


async def test_token_only_in_authorization_header(client_mod):
    c, rec = make(client_mod, ok({"peer_card": []}))
    await c.get_card("p")
    r = rec.last
    assert TOKEN not in str(r.url) and TOKEN.encode() not in r.content
    assert r.headers["authorization"] == f"Bearer {TOKEN}"


async def test_base_url_trailing_slash(client_mod):
    rec = Recorder(ok({}))
    c = client_mod.HonchoClient(BASE + "/", TOKEN, WS, transport=httpx.MockTransport(rec))
    await c.queue_status()
    assert str(rec.last.url) == f"{BASE}{W}/queue/status"


def test_honcho_error_attrs(client_mod):
    e = client_mod.HonchoError(500, "d")
    assert (e.status, e.detail) == (500, "d") and isinstance(e, Exception)
    assert client_mod.HonchoError(None, "net").status is None
