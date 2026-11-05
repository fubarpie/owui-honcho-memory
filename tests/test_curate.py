"""Tests for observation curation (token-waste reduction)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "honcho_helpers_curate", Path(__file__).resolve().parent.parent / "parts" / "02_helpers.py"
)
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)

REP = """## Explicit Observations
[2026-10-25 06:48:02] the user wants to write a filter for OpenWebUI that performs a model handoff.
[2026-10-25 06:48:02] the user would like to write a filter for OpenWebUI that performs a model handoff, moving the chat elsewhere.
[2026-11-05 07:08:35] the user ran a Python script that failed with an EOFError at the input prompt.
[2026-11-05 07:12:25] the user reported the pending count was stuck at 2 remaining out of 1007 total.
[2026-11-05 04:38:09] the user wants one Honcho server per Open WebUI deployment, tracking each user individually.
[2026-11-05 07:24:21] the user asked to merge PR #1.
[2026-11-05 07:40:00] the user interrupted the script with Ctrl+C.
## Deductive Observations
[2026-10-31 17:47:49] the user currently has a job while also looking into new jobs.
   Premises:
   - Currently has a job (2026-10-20)
"""
T = "2026-11-05"


def cur(q, **kw):
    return h.curate_observations(REP, q, today=T, **kw)


def test_parse_skips_headers_and_premises():
    texts = [t for t, _ in h.parse_observations(REP)]
    assert not any("Premises" in t or "Currently has a job (" in t or t.startswith("#") for t in texts)
    assert len(texts) == 8


def test_transient_dropped():
    out = " ".join(cur("honcho script queue"))
    for bad in ("EOFError", "Ctrl+C", "1007", "merge PR"):
        assert bad not in out


def test_duplicates_merged_and_prefix_compacted():
    out = cur("model handoff filter")
    assert sum("handoff" in o for o in out) == 1
    assert all(not o.lower().startswith("the user") for o in out)


def test_relevant_ranks_first_and_durable_kept_when_unrelated():
    assert "Honcho server" in cur("how should the honcho server work")[0]
    assert any("Honcho server" in o for o in cur("weather today"))


def test_known_texts_dedupe():
    out = cur("job", known_texts=["User currently has a job"])
    assert not any("has a job" in o for o in out)


def test_token_budget_and_always_keep():
    assert len(cur("x", max_tokens=15)) <= 1
    assert len(cur("weather", always_keep=1)) <= 1
    assert cur("weather", always_keep=0) == []


def test_empty_inputs():
    assert h.curate_observations(None, "q") == []
    assert h.curate_observations("", None) == []
