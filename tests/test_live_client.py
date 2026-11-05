"""Live smoke tests against a real Honcho server. Skipped unless HONCHO_TOKEN is set.

Config comes from the environment or ``.env.test`` (keys HONCHO_URL, HONCHO_TOKEN).
The token is never printed. Uses workspace 'owui-test' and peer ids prefixed 'pytest-'.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.live]

WORKSPACE = "owui-test"


def _load_env_test() -> None:
    """Tiny .env loader: KEY=VALUE lines, never overrides existing env, never prints."""
    path = Path(__file__).resolve().parent.parent / ".env.test"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        os.environ.setdefault(key, val.strip().strip("'\""))


_load_env_test()
TOKEN = os.environ.get("HONCHO_TOKEN", "")
URL = os.environ.get("HONCHO_URL", "http://localhost:8000")

if not TOKEN:
    pytestmark.append(pytest.mark.skip(reason="HONCHO_TOKEN not set"))


@pytest.fixture
def hc(client_mod):
    return client_mod.HonchoClient(URL, TOKEN, WORKSPACE, timeout=30.0)


@pytest.fixture
def ids():
    run = uuid.uuid4().hex[:8]
    return f"pytest-user-{run}", f"pytest-assistant-{run}", f"pytest-session-{run}"


async def _tolerate_500(client_mod, coro, what: str):
    """Await ``coro``; xfail with a clear message on server-side 500 (LLM backend unconfigured)."""
    try:
        return await coro
    except client_mod.HonchoError as e:
        if e.status == 500:
            pytest.xfail(f"{what}: Honcho returned 500 (server LLM/embeddings backend likely unconfigured)")
        raise


async def test_health(hc):
    assert await hc.health() is True


async def test_ensure_and_message_flow(client_mod, hc, ids):
    user, asst, sess = ids
    await hc.ensure_workspace()
    await hc.ensure_peer(user, metadata={"source": "pytest"})
    await hc.ensure_peer(asst)
    await hc.ensure_session(
        sess,
        {
            user: {"observe_me": True, "observe_others": False},
            asst: {"observe_me": False, "observe_others": False},
        },
        metadata={"source": "pytest"},
    )
    out = await hc.add_messages(
        sess,
        [
            {"peer_id": user, "content": "Hi, I'm a pytest user who likes green tea."},
            {"peer_id": asst, "content": "Nice to meet you!"},
        ],
    )
    assert len(out) == 2 and out[0]["peer_id"] == user

    ctx = await hc.get_peer_context(user, search_query="tea")
    assert ctx.get("peer_id") == user
    assert ctx.get("representation") in (None, "") or isinstance(ctx["representation"], str)

    sctx = await hc.get_session_context(sess, tokens=1000, peer_target=user, search_query="tea")
    assert sctx["id"] == sess
    assert len(sctx["messages"]) >= 2

    card = await hc.get_card(user)
    assert isinstance(card, list)

    qs = await hc.queue_status()
    assert "total_work_units" in qs

    # cache: second ensure makes no calls (would raise if server were unreachable)
    await hc.ensure_peer(user)

    await hc.delete_session(sess)


async def test_set_card_roundtrip(client_mod, hc, ids):
    user, _, _ = ids
    await hc.ensure_workspace()
    await hc.ensure_peer(user)
    await _tolerate_500(client_mod, hc.set_card(user, ["Name: Pytest", "Likes: tea"]), "set_card")
    assert await hc.get_card(user) == ["Name: Pytest", "Likes: tea"]


async def test_bad_token_raises_401(client_mod):
    bad = client_mod.HonchoClient(URL, "not-a-valid-token", WORKSPACE, timeout=15.0)
    with pytest.raises(client_mod.HonchoError) as ei:
        await bad.queue_status()
    assert ei.value.status in (401, 403)
    assert "not-a-valid-token" not in str(ei.value)


async def test_llm_dependent_endpoints_tolerate_500(client_mod, hc, ids):
    user, asst, _ = ids
    await hc.ensure_workspace()
    await hc.ensure_peer(user)
    await hc.ensure_peer(asst)
    await _tolerate_500(client_mod, hc.representation(user, search_query="tea"), "representation")
    await _tolerate_500(client_mod, hc.chat(user, "What does this user like?"), "chat")
    made = await _tolerate_500(
        client_mod, hc.create_conclusion(asst, user, "pytest: likes green tea"), "create_conclusion"
    )
    found = await _tolerate_500(
        client_mod, hc.query_conclusions(asst, user, "tea", top_k=3), "query_conclusions"
    )
    assert isinstance(found, list)
    items = await hc.list_conclusions(asst, user)
    assert isinstance(items, list)
    for c in made or []:
        await hc.delete_conclusion(c["id"])
