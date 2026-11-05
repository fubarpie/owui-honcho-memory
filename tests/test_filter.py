"""Tests for the Filter in dist/honcho_memory.py (run `python build.py` first).

Set HONCHO_DIST=/path/to/honcho_memory.py to test another build.
A FakeClient is injected by assigning ``filter._client`` (never rebuilt when assigned directly).
"""

import asyncio
import copy
import importlib.util
import os
import pathlib

import pytest

DIST = pathlib.Path(
    os.environ.get("HONCHO_DIST")
    or pathlib.Path(__file__).resolve().parent.parent / "dist" / "honcho_memory.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("honcho_memory_under_test", DIST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()
pytestmark = pytest.mark.asyncio


class FakeClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        self.added = []
        self.conclusions = [{"id": "c1", "content": "likes tea"}]
        self.ctx = {"representation": "Likes tea.\nLives in CO.", "peer_card": ["Name: Sam"]}

    def _rec(self, name, *a, **k):
        self.calls.append((name, a, k))
        if self.fail and name in ("get_peer_context", "add_messages", "create_conclusion", "chat", "schedule_dream"):
            raise mod.HonchoError("boom")

    async def ensure_workspace(self):
        self._rec("ensure_workspace")

    async def ensure_peer(self, peer_id, metadata=None, configuration=None):
        self._rec("ensure_peer", peer_id, metadata=metadata)

    async def ensure_session(self, session_id, peers, metadata=None):
        self._rec("ensure_session", session_id, peers)

    async def add_messages(self, session_id, messages):
        self._rec("add_messages", session_id, messages)
        self.added.append((session_id, messages))
        return []

    async def get_peer_context(self, peer_id, search_query=None, max_conclusions=25, target=None):
        self._rec("get_peer_context", peer_id, search_query=search_query)
        return self.ctx

    async def schedule_dream(self, observer, observed=None, session_id=None):
        self._rec("schedule_dream", observer, observed=observed)

    async def queue_status(self):
        self._rec("queue_status")
        return {"pending_work_units": 3, "in_progress_work_units": 1}

    async def create_conclusion(self, observer_id, observed_id, content, session_id=None):
        self._rec("create_conclusion", observer_id, observed_id, content, session_id)

    async def chat(self, peer_id, query, reasoning_level="low", session_id=None):
        self._rec("chat", peer_id, query, reasoning_level)
        return "They like tea."

    async def query_conclusions(self, observer_id, observed_id, query, top_k=10):
        self._rec("query_conclusions", query)
        return self.conclusions

    async def list_conclusions(self, observer_id, observed_id, page=1, size=50):
        self._rec("list_conclusions")
        return self.conclusions

    async def delete_conclusion(self, conclusion_id):
        self._rec("delete_conclusion", conclusion_id)

    def names(self):
        return [c[0] for c in self.calls]


USER = {"id": "u1", "name": "Sam", "email": "s@x", "role": "user"}


def make(fake=None):
    f = mod.Filter()
    f.valves.api_key = "tok"
    f._client = fake or FakeClient()
    return f


def body(text="hello", chat_id="chat1", extra_msgs=None):
    msgs = [{"role": "user", "content": text, "id": "m1"}]
    if extra_msgs:
        msgs += extra_msgs
    return {"model": "gpt-x", "messages": msgs, "metadata": {"chat_id": chat_id}}


class Emitter:
    def __init__(self):
        self.events = []

    async def __call__(self, ev):
        self.events.append(ev)

    def descs(self):
        return [e["data"]["description"] for e in self.events]


async def drain(f):
    if f._tasks:
        await asyncio.gather(*list(f._tasks))


def system_text(b):
    return " ".join(str(m["content"]) for m in b["messages"] if m["role"] == "system")


async def test_inlet_injects_and_is_idempotent():
    f, em = make(), Emitter()
    b = await f.inlet(body(), __user__=USER, __event_emitter__=em)
    assert "<honcho_memory>" in system_text(b)
    assert "Likes tea" in system_text(b)
    assert em.descs()[0].startswith("Recalling") and em.events[-1]["data"]["done"] is True
    assert "Recalled 3 facts" in em.descs()[-1]
    once = copy.deepcopy(b)
    b2 = await f.inlet(b, __user__=USER, __event_emitter__=em)
    assert system_text(b2).count("<honcho_memory>") == 1
    assert b2["messages"] == once["messages"]


async def test_inlet_sets_user_name_metadata_and_session():
    fake = FakeClient()
    f = make(fake)
    await f.inlet(body(), __user__=USER)
    peers = [c for c in fake.calls if c[0] == "ensure_peer"]
    assert peers[0][2]["metadata"] == {"name": "Sam"}
    assert peers[0][1][0] == "user-u1"
    assert "ensure_session" in fake.names()


async def test_outlet_saves_user_and_assistant_once():
    fake = FakeClient()
    f = make(fake)
    b = body(extra_msgs=[{"role": "assistant", "content": "hi there", "id": "a1"}])
    await f.outlet(b, __user__=USER)
    await drain(f)
    assert len(fake.added) == 1
    sid, msgs = fake.added[0]
    assert sid == "chat1"
    assert [m["content"] for m in msgs] == ["hello", "hi there"]
    assert msgs[0]["peer_id"] == "user-u1" and msgs[1]["peer_id"].startswith("assistant")
    await f.outlet(b, __user__=USER)
    await drain(f)
    assert len(fake.added) == 1  # idempotent


async def test_outlet_does_not_wait_for_save_and_swallows_errors():
    fake = FakeClient(fail=True)
    f = make(fake)
    em = Emitter()
    b = body(extra_msgs=[{"role": "assistant", "content": "yo", "id": "a1"}])
    out = await f.outlet(b, __user__=USER, __event_emitter__=em)
    assert out is b
    await drain(f)  # must not raise
    assert "Memory unavailable" in em.descs()
    # failed save is retried on a later turn
    fake.fail = False
    await f.outlet(b, __user__=USER)
    await drain(f)
    assert len(fake.added) == 1


async def test_saved_ids_is_lru_bounded():
    f = make()
    f._LRU_MAX = 3
    for i in range(10):
        f._remember_saved(f"k{i}")
    assert list(f._saved_ids) == ["k7", "k8", "k9"]


async def test_skip_on_task_requests():
    fake = FakeClient()
    f = make(fake)
    b = body()
    b["metadata"]["task"] = "title_generation"
    before = copy.deepcopy(b)
    assert await f.inlet(b, __user__=USER) == before
    b["messages"].append({"role": "assistant", "content": "t", "id": "a"})
    await f.outlet(b, __user__=USER)
    await drain(f)
    assert fake.calls == []


async def test_honcho_failure_leaves_body_unchanged():
    f, em = make(FakeClient(fail=True)), Emitter()
    b = body()
    before = copy.deepcopy(b)
    out = await f.inlet(b, __user__=USER, __event_emitter__=em)
    assert out == before
    assert em.descs()[-1] == "Memory unavailable" and em.events[-1]["data"]["done"] is True


async def test_status_respects_show_status_valves():
    f, em = make(FakeClient(fail=True)), Emitter()
    f.valves.show_status = False
    await f.inlet(body(), __user__=USER, __event_emitter__=em)
    assert em.events == []
    f.valves.show_status = True
    await f.inlet(body(), __user__={**USER, "valves": {"show_status": False}}, __event_emitter__=em)
    assert em.events == []


async def test_uservalves_disable():
    fake = FakeClient()
    f = make(fake)
    for uv in ({"enabled": False}, f.UserValves(enabled=False)):
        u = {**USER, "valves": uv}
        b = body()
        before = copy.deepcopy(b)
        assert await f.inlet(b, __user__=u) == before
        b["messages"].append({"role": "assistant", "content": "x", "id": "a"})
        await f.outlet(b, __user__=u)
    await drain(f)
    assert fake.calls == []


async def test_share_memory_false_still_injects_but_does_not_save():
    fake = FakeClient()
    f = make(fake)
    u = {**USER, "valves": {"share_memory": False}}
    b = await f.inlet(body(), __user__=u)
    assert "<honcho_memory>" in system_text(b)
    b["messages"].append({"role": "assistant", "content": "x", "id": "a"})
    await f.outlet(b, __user__=u)
    await drain(f)
    assert fake.added == []


async def test_missing_valves_and_user_use_defaults():
    f = make()
    b = await f.inlet(body(), __user__=USER)  # no 'valves' key
    assert "<honcho_memory>" in system_text(b)
    before = copy.deepcopy(body())
    assert await f.inlet(body(), __user__=None) == before  # no user -> skipped, no raise


async def test_temporary_chat_injects_but_does_not_save():
    fake = FakeClient()
    f = make(fake)
    b = body(chat_id="")
    del b["metadata"]["chat_id"]
    out = await f.inlet(b, __user__=USER)
    assert "<honcho_memory>" in system_text(out)
    assert "ensure_session" not in fake.names()
    out["messages"].append({"role": "assistant", "content": "x", "id": "a"})
    await f.outlet(out, __user__=USER)
    await drain(f)
    assert fake.added == []
    lb = body(chat_id="local:abc")
    await f.outlet(
        {**lb, "messages": lb["messages"] + [{"role": "assistant", "content": "x"}]}, __user__=USER
    )
    await drain(f)
    assert fake.added == []


async def test_command_show_injects_instruction():
    fake = FakeClient()
    f = make(fake)
    out = await f.inlet(body("/memory show"), __user__=USER)
    content = out["messages"][-1]["content"]
    assert "Name: Sam" in content and "Likes tea" in content
    assert "<honcho_memory>" not in system_text(out)


async def test_command_remember_creates_conclusion():
    fake = FakeClient()
    f = make(fake)
    out = await f.inlet(body("/memory remember I am vegan"), __user__=USER)
    call = next(c for c in fake.calls if c[0] == "create_conclusion")
    assert call[1][0].startswith("assistant")  # observer
    assert call[1][1] == "user-u1"  # observed
    assert call[1][2] == "I am vegan"
    assert "I am vegan" in out["messages"][-1]["content"]


async def test_command_ask_forget_help_unknown():
    fake = FakeClient()
    f = make(fake)
    out = await f.inlet(body("/memory ask what do I drink"), __user__=USER)
    assert "They like tea." in out["messages"][-1]["content"]
    out = await f.inlet(body("/memory forget"), __user__=USER)
    assert "c1" in out["messages"][-1]["content"] and "delete_conclusion" not in fake.names()
    out = await f.inlet(body("/memory forget c1"), __user__=USER)
    assert ("delete_conclusion", ("c1",), {}) in fake.calls
    out = await f.inlet(body("/memory help"), __user__=USER)
    assert "/memory remember" in out["messages"][-1]["content"]
    out = await f.inlet(body("/memory bogus"), __user__=USER)
    assert "Unknown" in out["messages"][-1]["content"]


async def test_command_failure_degrades():
    f, em = make(FakeClient(fail=True)), Emitter()
    out = await f.inlet(body("/memory remember x"), __user__=USER, __event_emitter__=em)
    assert "unavailable" in out["messages"][-1]["content"]
    assert em.descs()[-1] == "Memory unavailable"


async def test_command_off_on_toggle():
    fake = FakeClient()
    f = make(fake)
    out = await f.inlet(body("/memory off"), __user__=USER)
    assert (
        "restart" in out["messages"][-1]["content"]
        and "UserValves" in out["messages"][-1]["content"]
    )
    assert "u1" in f._disabled
    b = body("hello")
    before = copy.deepcopy(b)
    assert await f.inlet(b, __user__=USER) == before
    b["messages"].append({"role": "assistant", "content": "x", "id": "a"})
    await f.outlet(b, __user__=USER)
    await drain(f)
    assert fake.calls == [] and fake.added == []
    out = await f.inlet(body("/memory show"), __user__=USER)
    assert "OFF" in out["messages"][-1]["content"]
    await f.inlet(body("/memory on"), __user__=USER)
    assert "u1" not in f._disabled
    out = await f.inlet(body("hello"), __user__=USER)
    assert "<honcho_memory>" in system_text(out)


async def test_command_messages_are_not_saved():
    fake = FakeClient()
    f = make(fake)
    b = body("/memory remember x", extra_msgs=[{"role": "assistant", "content": "ok", "id": "a"}])
    await f.outlet(b, __user__=USER)
    await drain(f)
    assert fake.added == []


async def test_client_rebuilt_when_valves_change():
    f = mod.Filter()
    f.valves.api_key = "a"
    c1 = f._get_client()
    assert f._get_client() is c1
    f.valves.honcho_url = "http://other:1"
    c2 = f._get_client()
    assert c2 is not c1
    f.valves.workspace_id = "ws2"
    assert f._get_client() is not c2


async def test_command_dream_schedules_and_reports_queue():
    fake = FakeClient()
    f = make(fake)
    out = await f.inlet(body("/memory dream"), __user__=USER)
    assert ("schedule_dream", ("user-u1",), {"observed": "user-u1"}) in fake.calls
    content = out["messages"][-1]["content"]
    assert "scheduled" in content and "4 background work units" in content


async def test_command_dream_failure_degrades():
    f = make(FakeClient(fail=True))
    out = await f.inlet(body("/memory dream"), __user__=USER)
    assert "unavailable" in out["messages"][-1]["content"]
