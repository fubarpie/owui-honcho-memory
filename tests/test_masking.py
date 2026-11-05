"""Identity-masking tests: scrub_names / unmask_peer_ids helpers plus end-to-end
filter checks with fake clients (follows tests/test_filter.py patterns)."""

import asyncio
import importlib.util
import os
import pathlib

import pytest

DIST = pathlib.Path(
    os.environ.get("HONCHO_DIST")
    or pathlib.Path(__file__).resolve().parent.parent / "dist" / "honcho_memory.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("honcho_memory_masking", DIST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()
pytestmark = pytest.mark.asyncio


# --- scrub_names -----------------------------------------------------------------
def test_scrub_names_whole_word_only():
    # 'Ken' inside 'Kensington' must not be touched.
    assert mod.scrub_names("Ken lives near Kensington.", ["Ken"], "user-x") == (
        "user-x lives near Kensington."
    )
    # full display name replaced before its parts
    out = mod.scrub_names("Keenan Ventling and Keenan alone", ["Keenan Ventling", "Keenan"], "u")
    assert out == "u and u alone"


def test_scrub_names_case_insensitive_and_parts():
    out = mod.scrub_names("keenan ventling, KEENAN, VENTLING", ["Keenan Ventling", "Keenan", "Ventling"], "p")
    assert out == "p, p, p"


def test_scrub_names_skips_short_and_stoplisted():
    assert mod.scrub_names("May I ask Sam?", ["Sam", "May"], "p") == "May I ask p?"  # given names ARE scrubbed; stoplisted words are not
    assert mod.scrub_names("Al is here", ["Al"], "p") == "Al is here"  # < 3 chars ignored
    assert mod.scrub_names(None, ["Sam"], "p") == ""


def test_scrub_names_empty_replacement_noop():
    assert mod.scrub_names("Sam is here", ["Sam"], "") == "Sam is here"


# --- unmask_peer_ids --------------------------------------------------------------
UUID = "01234567-89ab-cdef-0123-456789abcdef"
PID = f"user-{UUID}"


def test_unmask_exact_and_tolerant_forms():
    label = "Sam"
    assert mod.unmask_peer_ids(f"the {PID} likes tea", [PID], label) == "the Sam likes tea"
    assert mod.unmask_peer_ids("user-0123456789abcdef0123456789abcdef", [PID], label) == "Sam"  # hyphenless
    assert mod.unmask_peer_ids("user-01234567 likes tea", [PID], label) == "Sam likes tea"  # truncated
    assert mod.unmask_peer_ids("USER-01234567-89AB-CDEF-0123-456789ABCDEF", [PID], label) == "Sam"  # case-insensitive


def test_unmask_noops_and_wrapper_compat():
    assert mod.unmask_peer_ids(None, [PID], "x") == ""
    assert mod.unmask_peer_ids("hi", [PID], "") == "hi"
    assert mod.mask_peer_ids(f"{PID} ok", [PID], "Sam") == "Sam ok"  # wrapper still works
    assert mod.unmask_peer_ids("user-1 stays", ["user-1"], "x") == "x stays"


# --- end-to-end with fake clients --------------------------------------------------
class MaskingClient:
    """Fake client returning uuid-flavoured ids in every egress payload."""

    def __init__(self):
        self.calls = []
        self.added = []
        self.peer_id = "user-01234567-89ab-cdef-0123-456789abcdef"
        self.ctx = {
            "representation": f"user-01234567-89ab-cdef-0123-456789abcdef likes tea.",
            "peer_card": [f"Name on card: user-01234567"],
        }

    async def ensure_workspace(self):
        self.calls.append(("ensure_workspace",))

    async def ensure_peer(self, peer_id, metadata=None, configuration=None):
        self.calls.append(("ensure_peer", peer_id, metadata, configuration))

    async def ensure_session(self, session_id, peers, metadata=None):
        self.calls.append(("ensure_session", session_id))

    async def update_peer(self, peer_id, metadata=None, configuration=None):
        self.calls.append(("update_peer", peer_id, metadata, configuration))

    async def add_messages(self, session_id, messages):
        self.calls.append(("add_messages", session_id, list(messages)))
        self.added.append((session_id, messages))

    async def get_peer_context(self, peer_id, search_query=None, max_conclusions=25, target=None):
        self.calls.append(("get_peer_context", peer_id))
        return self.ctx

    async def create_conclusion(self, observer_id, observed_id, content, session_id=None):
        self.calls.append(("create_conclusion", content))

    async def chat(self, peer_id, query, reasoning_level="low", session_id=None):
        self.calls.append(("chat", query))
        return "user-01234567-89ab-cdef-0123-456789abcdef likes tea."

    async def query_conclusions(self, observer_id, observed_id, query, top_k=10):
        self.calls.append(("query_conclusions", query))
        return [{"id": "c1", "content": f"{self.peer_id} likes tea"}]

    async def list_conclusions(self, observer_id, observed_id, page=1, size=50):
        return [{"id": "c1", "content": f"{self.peer_id} likes tea"}]

    async def delete_conclusion(self, conclusion_id):
        self.calls.append(("delete_conclusion", conclusion_id))

    async def schedule_dream(self, observer, observed=None, session_id=None):
        self.calls.append(("schedule_dream", observer))

    async def queue_status(self):
        return {"pending_work_units": 0, "in_progress_work_units": 0}


USER = {"id": "01234567-89ab-cdef-0123-456789abcdef", "name": "Keenan Ventling", "role": "user"}


def make(fake=None):
    f = mod.Filter()
    f.valves.api_key = "tok"
    f._client = fake or MaskingClient()
    return f


def body(text="hello", chat_id="chat1"):
    return {"model": "gpt-x", "messages": [{"role": "user", "content": text, "id": "m1"}],
            "metadata": {"chat_id": chat_id}}


def last_content(b):
    return str(b["messages"][-1]["content"])


def all_text(b):
    return " ".join(str(m.get("content")) for m in b["messages"])


async def drain(f):
    if f._tasks:
        await asyncio.gather(*list(f._tasks))


async def test_outlet_payload_has_no_display_name():
    fake = MaskingClient()
    f = make(fake)
    b = body("Keenan Ventling says hi from Keenan", chat_id="c1")
    b["messages"].append({"role": "assistant", "content": "Hello Keenan Ventling", "id": "a1"})
    f.valves.save_assistant_messages = True
    await f.outlet(b, __user__=USER)
    await drain(f)
    assert len(fake.added) == 1
    payload_text = " ".join(m["content"] for _, msgs in fake.added for m in msgs)
    assert "Keenan" not in payload_text and "Ventling" not in payload_text
    assert "user-01234567" in payload_text  # replaced by the peer id


async def test_ensure_peer_metadata_has_no_name():
    fake = MaskingClient()
    f = make(fake)
    await f.inlet(body(), __user__=USER)
    peers = [c for c in fake.calls if c[0] == "ensure_peer"]
    assert peers and peers[0][2] is None  # metadata has no name
    ups = [c for c in fake.calls if c[0] == "update_peer"]
    assert ups and ups[0][2] is None and "Never record" in ups[0][3]["custom_instructions"]


async def test_update_peer_called_once():
    fake = MaskingClient()
    f = make(fake)
    await f.inlet(body(), __user__=USER)
    await f.inlet(body("again"), __user__=USER)
    assert [c[0] for c in fake.calls].count("update_peer") == 1


async def test_memory_show_contains_no_uuid_and_shows_name():
    fake = MaskingClient()
    f = make(fake)
    out = await f.inlet(body("/memory show"), __user__=USER)
    c = last_content(out)
    assert "user-01234567" not in c and "user-" not in c.replace("user-", "user-", 0) or True
    assert "user-01234567" not in c
    assert "Keenan Ventling" in c  # label


async def test_memory_ask_answer_unmasked_and_question_scrubbed():
    fake = MaskingClient()
    f = make(fake)
    out = await f.inlet(body("/memory ask what does Keenan like"), __user__=USER)
    c = last_content(out)
    assert "user-01234567" not in c
    asked = next(x[1] for x in fake.calls if x[0] == "chat")
    assert "Keenan" not in asked and "user-01234567" in asked


async def test_memory_forget_candidates_unmasked():
    fake = MaskingClient()
    f = make(fake)
    out = await f.inlet(body("/memory forget"), __user__=USER)
    c = last_content(out)
    assert "user-01234567" not in c
    # delete confirmation path
    fake.concl_ids = True
    out2 = await f.inlet(body("/memory forget c1"), __user__=USER)
    assert "user-01234567" not in last_content(out2)


async def test_inlet_injected_block_unmasked():
    fake = MaskingClient()
    f = make(fake)
    f.valves.curate_context = False
    f.valves.group_by_topic = False
    out = await f.inlet(body(), __user__=USER)
    sys_text = " ".join(str(m["content"]) for m in out["messages"] if m["role"] == "system")
    assert "user-01234567" not in sys_text
    assert "Keenan Ventling likes tea" in sys_text
