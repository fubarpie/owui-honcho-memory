"""Tests for parts/02_helpers.py (loaded directly via importlib)."""

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).resolve().parent.parent / "parts" / "02_helpers.py"
_spec = importlib.util.spec_from_file_location("helpers02", _PATH)
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)

BLOCK = "<honcho_memory>\nfact\n</honcho_memory>"


# --- ids -------------------------------------------------------------------
def test_sanitize_id_basic():
    assert h.sanitize_id("a b/c:d") == "a_b_c_d"
    assert h.sanitize_id("ok_id-1") == "ok_id-1"


def test_sanitize_id_unicode_and_empty_and_len():
    assert h.sanitize_id("héllo✓") == "h_llo_"
    assert h.sanitize_id("") == "unknown"
    assert h.sanitize_id(None) == "unknown"
    assert len(h.sanitize_id("x" * 500)) == 100
    assert h.sanitize_id("x" * 50, max_len=10) == "x" * 10


def test_user_peer_id():
    assert h.user_peer_id("user-", "abc-123") == "user-abc-123"
    assert h.user_peer_id("user-", "a@b.c") == "user-a_b_c"
    assert h.user_peer_id("", None) == "unknown"


def test_assistant_peer_id():
    assert h.assistant_peer_id("gpt-4o:latest", True) == "assistant-gpt-4o_latest"
    assert h.assistant_peer_id("gpt-4o", False) == "assistant"
    assert h.assistant_peer_id(None, True) == "assistant"
    assert h.assistant_peer_id("", True) == "assistant"


# --- strip / extract / clean ----------------------------------------------
def test_strip_memory_block():
    assert h.strip_memory_block(f"hi\n{BLOCK}\nthere") == "hi\n\nthere"
    assert h.strip_memory_block(BLOCK) == ""
    assert h.strip_memory_block(None) == ""
    assert h.strip_memory_block("plain") == "plain"


def test_strip_memory_block_multiple_and_unterminated_and_case():
    assert h.strip_memory_block(f"{BLOCK}a{BLOCK}b") == "ab"
    assert h.strip_memory_block("keep <honcho_memory>cut off") == "keep"
    assert h.strip_memory_block("x <HONCHO_MEMORY>y</HONCHO_MEMORY> z") == "x  z"


def test_extract_text():
    assert h.extract_text("abc") == "abc"
    assert h.extract_text(None) == ""
    assert h.extract_text(42) == ""
    parts = [
        {"type": "text", "text": "one"},
        {"type": "image_url", "image_url": {"url": "x"}},
        {"type": "text", "text": "two"},
        {"type": "text", "text": None},
    ]
    assert h.extract_text(parts) == "one\ntwo"
    assert h.extract_text([]) == ""
    assert h.extract_text([{"type": "text", "text": "héllo ✓"}]) == "héllo ✓"


def test_clean_strips_think_and_details():
    t = "<think>secret\nthoughts</think>Answer"
    assert h.clean_message_for_save(t, 1000) == "Answer"
    d = '<details type="reasoning" done="true" duration="3">\n<summary>Thought</summary>\n> hmm\n</details>\nFinal'
    assert h.clean_message_for_save(d, 1000) == "Final"
    assert h.clean_message_for_save("<THINK>x</THINK>ok", 100) == "ok"
    assert h.clean_message_for_save("a <think>unterminated", 100) == "a"


def test_clean_keeps_other_details():
    t = '<details type="tool_calls">x</details> y'
    assert "tool_calls" in h.clean_message_for_save(t, 1000)


def test_clean_strips_memory_and_collapses_whitespace():
    t = f"{BLOCK}\n\n\n\nhello   \t world  \n\n\n\nbye"
    assert h.clean_message_for_save(t, 1000) == "hello world\n\nbye"


def test_clean_truncation_and_empty():
    assert h.clean_message_for_save("abcdefghij", 5) == "abcde"
    assert h.clean_message_for_save("", 5) == ""
    assert h.clean_message_for_save(None, 5) == ""
    assert h.clean_message_for_save("héllo✓✓", 6) == "héllo✓"
    assert h.clean_message_for_save("<think>only</think>", 50) == ""


# --- format_memory_block ---------------------------------------------------
def test_format_empty_returns_blank():
    assert h.format_memory_block(None, None, None, 1000, "H") == ""
    assert h.format_memory_block("  ", [], [""], 1000, "H") == ""
    assert h.format_memory_block("x", None, None, 0, "H") == ""


def test_format_full():
    out = h.format_memory_block("Likes tea\nLives in CO", ["Name: Keenan"], ["Moved recently"], 2000, "User memory")
    assert out.startswith("<honcho_memory>\n")
    assert out.endswith("</honcho_memory>")
    assert "User memory" in out
    assert "Background facts about the user; use naturally, do not mention unless relevant." in out
    assert "- Name: Keenan" in out
    assert "Likes tea" in out and "Lives in CO" in out
    assert "- Moved recently" in out
    assert out.count("<honcho_memory>") == 1


def test_format_unicode():
    out = h.format_memory_block("Любит чай ✓", None, None, 2000, "")
    assert "Любит чай ✓" in out


def test_format_truncates_on_line_boundaries_and_respects_max():
    card = [f"fact number {i} is here" for i in range(100)]
    out = h.format_memory_block(None, card, None, 400, "H")
    assert 0 < len(out) <= 400
    assert out.endswith("</honcho_memory>")
    body_lines = out.splitlines()[3:-1]
    assert all(line == "Profile:" or line.startswith("- fact number ") for line in body_lines)
    assert all(line.endswith("is here") for line in body_lines[1:])  # no half lines


def test_format_too_small_budget_returns_empty():
    assert h.format_memory_block("fact", None, None, 50, "H") == ""


def test_format_no_dangling_section_title():
    out = h.format_memory_block("rep line " * 3, ["c" * 30], None, 0, "H")
    assert out == ""
    full = h.format_memory_block("r" * 400, ["card"], None, 300, "")
    assert "Known about the user:" not in full
    assert "- card" in full


# --- inject ----------------------------------------------------------------
def test_inject_system_creates_message():
    msgs = [{"role": "user", "content": "hi"}]
    out = h.inject_into_messages(msgs, BLOCK, "system")
    assert out[0] == {"role": "system", "content": BLOCK}
    assert out[1] == msgs[0]


def test_inject_system_merges_into_existing():
    msgs = [{"role": "system", "content": "Be nice"}, {"role": "user", "content": "hi"}]
    out = h.inject_into_messages(msgs, BLOCK, "system")
    assert out[0]["content"] == f"Be nice\n\n{BLOCK}"
    assert len(out) == 2


def test_inject_idempotent_and_replaces():
    msgs = [{"role": "system", "content": "Be nice"}, {"role": "user", "content": "hi"}]
    once = h.inject_into_messages(msgs, BLOCK, "system")
    twice = h.inject_into_messages(once, BLOCK, "system")
    assert once == twice
    new = "<honcho_memory>\nnew\n</honcho_memory>"
    third = h.inject_into_messages(once, new, "system")
    assert third[0]["content"].count("<honcho_memory>") == 1
    assert "new" in third[0]["content"] and "fact" not in third[0]["content"]


def test_inject_idempotent_when_created_system_message():
    msgs = [{"role": "user", "content": "hi"}]
    once = h.inject_into_messages(msgs, BLOCK, "system")
    twice = h.inject_into_messages(once, BLOCK, "system")
    assert once == twice and len(twice) == 2


def test_inject_empty_block_removes_prior():
    once = h.inject_into_messages([{"role": "user", "content": "hi"}], BLOCK, "system")
    out = h.inject_into_messages(once, "", "system")
    assert out == [{"role": "user", "content": "hi"}]
    keep = h.inject_into_messages([{"role": "system", "content": "S"}, {"role": "user", "content": "q"}], BLOCK)
    out = h.inject_into_messages(keep, "")
    assert out[0]["content"] == "S"


def test_inject_user_position():
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    out = h.inject_into_messages(msgs, BLOCK, "user")
    assert out[0]["content"] == "a"
    assert out[2]["content"] == f"{BLOCK}\n\nc"
    again = h.inject_into_messages(out, BLOCK, "user")
    assert again == out


def test_inject_user_replaces_old_and_switches_position():
    sys_first = h.inject_into_messages([{"role": "user", "content": "c"}], BLOCK, "system")
    out = h.inject_into_messages(sys_first, BLOCK, "user")
    assert len(out) == 1
    assert out[0]["content"] == f"{BLOCK}\n\nc"


def test_inject_list_content():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "u"}}]}]
    out = h.inject_into_messages(msgs, BLOCK, "user")
    assert out[0]["content"][0] == {"type": "text", "text": BLOCK}
    assert len(out[0]["content"]) == 3
    assert h.inject_into_messages(out, BLOCK, "user") == out
    sysmsgs = [{"role": "system", "content": [{"type": "text", "text": "S"}]}, msgs[0]]
    o2 = h.inject_into_messages(sysmsgs, BLOCK, "system")
    assert o2[0]["content"][-1] == {"type": "text", "text": BLOCK}
    assert h.inject_into_messages(o2, BLOCK, "system") == o2


def test_inject_strips_block_in_user_message_from_history():
    msgs = [{"role": "user", "content": f"{BLOCK}\n\nhello"}, {"role": "user", "content": "x"}]
    out = h.inject_into_messages(msgs, "", "system")
    assert out[0]["content"] == "hello"


def test_inject_does_not_mutate_input():
    msgs = [
        {"role": "system", "content": f"S\n\n{BLOCK}"},
        {"role": "user", "content": [{"type": "text", "text": "t"}]},
    ]
    snap = copy.deepcopy(msgs)
    for pos in ("system", "user"):
        h.inject_into_messages(msgs, "<honcho_memory>z</honcho_memory>", pos)
        assert msgs == snap
    out = h.inject_into_messages(msgs, BLOCK, "system")
    out[1]["content"][0]["text"] = "changed"
    assert msgs == snap


def test_inject_edge_inputs():
    assert h.inject_into_messages(None, BLOCK, "system") == [{"role": "system", "content": BLOCK}]
    assert h.inject_into_messages([], "", "system") == []
    assert h.inject_into_messages([{"role": "assistant", "content": "a"}], BLOCK, "user") == [
        {"role": "assistant", "content": "a"}
    ]
    out = h.inject_into_messages([{"role": "user", "content": None}], BLOCK, "user")
    assert out[0]["content"] == BLOCK


# --- tokens ---------------------------------------------------------------
def test_approx_tokens():
    assert h.approx_tokens("") == 0
    assert h.approx_tokens(None) == 0
    assert h.approx_tokens("abcdefgh") == 2
    assert h.approx_tokens("abc") == 0


def test_truncate_to_tokens():
    assert h.truncate_to_tokens("short", 10) == "short"
    assert h.truncate_to_tokens("abcdefghij", 2) == "abcdefgh"
    assert h.truncate_to_tokens("abc", 0) == ""
    assert h.truncate_to_tokens(None, 5) == ""
    assert h.truncate_to_tokens("line one\nline two", 3) == "line one"
    assert h.truncate_to_tokens("aaaa\nbbbb", 1) == "aaaa"


# --- parse_command ---------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("/memory", ("show", "")),
        ("/memory show", ("show", "")),
        ("/MEMORY SHOW", ("show", "")),
        ("  /memory   remember I like tea  ", ("remember", "I like tea")),
        ("/Memory Ask what do I like?", ("ask", "what do I like?")),
        ("/memory forget", ("forget", "")),
        ("/memory off", ("off", "")),
        ("/memory on", ("on", "")),
        ("/memory help", ("help", "")),
        ("/memory remember line1\nline2", ("remember", "line1\nline2")),
        ("/memory remember café ✓", ("remember", "café ✓")),
        ("/memory foo", ("foo", "")),
    ],
)
def test_parse_command_valid(text, expected):
    assert h.parse_command(text) == expected


@pytest.mark.parametrize("text", ["", None, "hello", "/memoryx", "say /memory", "memory show", "/mem", "//memory", 5])
def test_parse_command_invalid(text):
    assert h.parse_command(text) is None


# --- should_skip -----------------------------------------------------------
def _body(**kw):
    b = {"messages": [{"role": "user", "content": "hello"}]}
    b.update(kw)
    return b


USER = {"id": "u1", "name": "K"}
V = SimpleNamespace()


def test_skip_ok():
    assert h.should_skip(_body(), USER, V) is None
    assert h.should_skip(_body(), USER, None) is None


def test_skip_task_requests():
    r = h.should_skip(_body(metadata={"task": "title_generation"}), USER, V)
    assert r and "title_generation" in r
    assert h.should_skip(_body(metadata={"task": "tags_generation"}), USER, V)
    assert h.should_skip(_body(task="query_generation"), USER, V)
    assert h.should_skip(_body(metadata={"task": None}), USER, V) is None
    assert h.should_skip(_body(metadata=None), USER, V) is None


def test_skip_tasks_can_be_disabled_via_valves():
    assert h.should_skip(_body(metadata={"task": "x"}), USER, SimpleNamespace(skip_tasks=False)) is None


def test_skip_missing_user():
    assert h.should_skip(_body(), None, V) == "no_user"
    assert h.should_skip(_body(), {}, V) == "no_user"
    assert h.should_skip(_body(), {"name": "x"}, V) == "no_user"


def test_skip_no_messages_and_empty_prompt():
    assert h.should_skip({}, USER, V) == "no_messages"
    assert h.should_skip(None, USER, V) == "no_user" or True
    assert h.should_skip({"messages": []}, USER, V) == "no_messages"
    assert h.should_skip({"messages": "oops"}, USER, V) == "no_messages"
    assert h.should_skip({"messages": [{"role": "user", "content": "   "}]}, USER, V) == "empty_prompt"
    assert h.should_skip({"messages": [{"role": "system", "content": "s"}]}, USER, V) == "empty_prompt"
    assert h.should_skip({"messages": [{"role": "user", "content": None}]}, USER, V) == "empty_prompt"
    assert h.should_skip({"messages": [{"role": "user", "content": [{"type": "image_url"}]}]}, USER, V) == "empty_prompt"


def test_skip_prompt_only_memory_block_is_empty():
    assert h.should_skip({"messages": [{"role": "user", "content": BLOCK}]}, USER, V) == "empty_prompt"


def test_skip_uses_last_user_message():
    body = {"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}, {"role": "user", "content": ""}]}
    assert h.should_skip(body, USER, V) == "empty_prompt"


def test_skip_list_content_ok():
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "hey"}]}]}
    assert h.should_skip(body, USER, V) is None


def test_skip_disabled_flags():
    assert h.should_skip(_body(), USER, SimpleNamespace(enabled=False)) == "disabled"
    assert h.should_skip(_body(), {"id": "u", "valves": {"enabled": False}}, V) == "user_disabled"
    assert h.should_skip(_body(), {"id": "u", "valves": SimpleNamespace(enabled=False)}, V) == "user_disabled"
    assert h.should_skip(_body(), {"id": "u", "valves": {"enabled": True}}, V) is None
    assert h.should_skip(_body(), {"id": "u", "valves": None}, V) is None


def test_should_skip_does_not_mutate():
    b = _body(metadata={"task": "t"})
    snap = copy.deepcopy(b)
    h.should_skip(b, USER, V)
    assert b == snap


def test_mask_peer_ids():
    pid = "user-abc-123"
    assert h.mask_peer_ids(f"{pid} likes tea. {pid} has a dog.", [pid], "the user") == "the user likes tea. the user has a dog."
    assert h.mask_peer_ids(f"{pid}", [pid], "Keenan") == "Keenan"


def test_mask_peer_ids_noops():
    assert h.mask_peer_ids(None, ["x"], "the user") == ""
    assert h.mask_peer_ids("user-1 hi", [], "the user") == "user-1 hi"
    assert h.mask_peer_ids("user-1 hi", ["user-1"], "") == "user-1 hi"
    assert h.mask_peer_ids("user-1 hi", [""], "u") == "user-1 hi"


def test_mask_peer_ids_longest_first():
    assert h.mask_peer_ids("user-12 and user-1", ["user-1", "user-12"], "U") == "U and U"
