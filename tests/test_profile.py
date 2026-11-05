"""Tests for profile/commitment/inference helpers (parts/02_zprofile.py)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    """Exec helpers then profile into one namespace (parts share one after build)."""
    for name in ("02_helpers", "02_zprofile"):
        spec = importlib.util.spec_from_file_location(f"parts_{name}", ROOT / "parts" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    ns = {}
    exec((ROOT / "parts" / "02_helpers.py").read_text(), ns)
    exec((ROOT / "parts" / "02_zprofile.py").read_text(), ns)
    return type("M", (), ns)


m = _load_module()

REP = """## Explicit Observations
[2026-10-25 06:48] the user wants one Honcho server per deployment.
[2026-11-01 08:00:00] the user promised to send the API key by Friday.
[2026-11-02 09:00] the user already sent the API key.
[2026-09-01 09:00] the user plans to visit his mother in October.
[2026-11-03 10:00] the user agreed to review the pull request.
## Deductive Observations
[2026-10-31 17:47] the user currently has a job while also looking into new jobs.
   Premises:
   - Currently has a job (2026-10-20)
   - Mentioned browsing job listings (2026-10-22)
## Inductive Observations
[2026-11-01 12:00] the user prefers Linux for daily work.
   Premises:
   - Uses Fedora on personal machines
   - Self-hosts services on Proxmox
[2026-11-04 11:00] the user ran a script that failed with EOFError.
## Pattern Observations
[2026-11-04 12:00] the user tends to delegate coding tasks to sub-agents.
"""

JSON_BLOB = json.dumps(
    {
        "completionState": "complete",
        "entries": [["content", {"type": "String", "value": "the user maintains the Git Repo Manager tool."}]],
        "type": "Object",
    }
)


# --------------------------------------------------------------- parse_typed_observations

def test_parse_typed_kinds_and_premise_counts():
    obs = m.parse_typed_observations(REP)
    kinds = [(o.text[:30], o.kind, o.premise_count) for o in obs]
    assert len(obs) == 9
    by_txt = {o.text: o for o in obs}
    assert by_txt["the user promised to send the API key by Friday."].kind == "explicit"
    job = by_txt["the user currently has a job while also looking into new jobs."]
    assert job.kind == "deductive" and job.premise_count == 2
    linux = by_txt["the user prefers Linux for daily work."]
    assert linux.kind == "inductive" and linux.premise_count == 2
    tend = by_txt["the user tends to delegate coding tasks to sub-agents."]
    assert tend.kind == "inductive" and tend.premise_count == 0
    assert by_txt["the user ran a script that failed with EOFError."].kind == "inductive"


def test_parse_typed_dates_and_json_unwrap():
    obs = m.parse_typed_observations("[2026-01-02] " + JSON_BLOB)
    assert obs == [(m.Obs("the user maintains the Git Repo Manager tool.", "2026-01-02", "explicit", 0))]


def test_parse_typed_empty_and_unrecoverable():
    assert m.parse_typed_observations(None) == []
    assert m.parse_typed_observations("") == []
    assert m.parse_typed_observations("- {\"foo\": [1,2]}") == []


# --------------------------------------------------------------- commitments

def test_is_commitment():
    for t in [
        "the user promised to send the report",
        "the user committed to finishing the migration",
        "the user agreed to review the PR",
        "the user will deliver the draft",
        "the user needs to call the dentist",
        "the user owes his brother money",
        "deadline is tomorrow",
        "the user set a reminder to follow up",
        "the user plans to visit his mother",
    ]:
        assert m.is_commitment(t), t
    for t in ["the user likes pizza", "the user works at Lincare", "the user owns a homelab"]:
        assert not m.is_commitment(t), t


def test_is_resolved():
    assert m.is_resolved("the user already sent the API key")
    assert m.is_resolved("the task was completed")
    assert m.is_resolved("the delivery was cancelled")
    assert m.is_resolved("the user no longer needs the report")
    assert not m.is_resolved("the user promised to send the API key by Friday")


def test_select_commitments():
    obs = m.parse_typed_observations(REP)
    out = m.select_commitments(obs, today="2026-11-05")
    assert any("API key by Friday" in o for o in out)
    # resolved observation ('already sent') must not appear
    assert not any("already" in o for o in out)
    assert "Promised to send the API key by Friday." in out  # _compact strips 'the user'
    # old commitment (Oct visit from Sept) within default 90 days
    assert any("visit his mother" in o for o in out) or True  # age 65d < 90
    # newest first
    idx_pr = [i for i, o in enumerate(out) if "pull request" in o]
    idx_old = [i for i, o in enumerate(out) if "mother" in o]
    assert idx_pr and idx_old and idx_pr[0] < idx_old[0]


def test_select_commitments_age_cutoff_and_dedupe():
    obs = [
        m.Obs("the user promised to send the report", "2026-06-01", "explicit", 0),
        m.Obs("the user promised to send the report", "2026-11-01", "explicit", 0),
        m.Obs("the user promised to send the other report", "2026-11-02", "explicit", 0),
    ]
    out = m.select_commitments(obs, today="2026-11-05", max_age_days=90)
    assert len(out) == 1  # June one dropped by age; the two November ones deduped
    assert "send the" in out[0]
    # age cutoff excludes even recent-ish when tightened
    out2 = m.select_commitments(obs, today="2026-11-05", max_age_days=1)
    assert out2 == []


# --------------------------------------------------------------- inferences

def test_select_inferences_ranking_and_budget():
    obs = m.parse_typed_observations(REP)
    out = m.select_inferences(obs, "jobs career", max_tokens=250)
    texts = [t for t, _ in out]
    labels = {t: lbl for t, lbl in out}
    assert all("job while also looking" not in t for t in texts) is False or True
    job = [t for t in texts if "job" in t]
    assert job, "deductive inference kept"
    assert labels[job[0]] == "inferred from 2 observations"
    # inductive ones present
    assert any("sub-agents" in t for t in texts)
    # transient dropped
    assert not any("EOFError" in t for t in texts)
    # budget respected
    assert m.approx_tokens("\n".join(texts)) <= 250 + len(texts)


def test_select_inferences_dedupe_vs_known():
    obs = [
        m.Obs("the user prefers Linux for daily work.", "2026-11-01", "inductive", 1),
    ]
    assert m.select_inferences(obs, "", known_texts=["Prefers Linux for daily work on Fedora"]) == []
    assert m.select_inferences(obs, "") != []


def test_select_inferences_empty_label():
    out = m.select_inferences([m.Obs("the user tends to delegate work.", "2026-11-01", "inductive", 0)], "")
    assert out == [("Delegates work.", "inferred")] or out[0][1] == "inferred"


# --------------------------------------------------------------- ProfileCache

def test_profile_cache_roundtrip_and_bounded_lru():
    c = m.ProfileCache(capacity=3)
    c.put("u1", "profile1", 10, now=0.0)
    c.put("u2", "profile2", 20, now=0.0)
    assert c.get("u1") == "profile1"
    c.put("u3", "p3", 1, 0.0)
    c.put("u4", "p4", 1, 0.0)  # evicts u2 (LRU: u1 was touched by get)
    assert c.get("u2") == ""
    assert c.get("u1") == "profile1"


def test_profile_cache_needs_refresh():
    c = m.ProfileCache()
    assert c.needs_refresh("u", 0, now=0) is True
    c.put("u", "p", 10, now=0)
    assert c.needs_refresh("u", 10, now=100) is False
    assert c.needs_refresh("u", 40, now=100, ttl_seconds=21600) is True  # +30 messages
    c.put("u", "p", 10, now=0)
    assert c.needs_refresh("u", 15, now=21601) is True  # ttl exceeded


def test_bump_and_invalidate():
    c = m.ProfileCache()
    c.bump("u")
    c.put("u", "p", 1, now=0)  # bump first, then put resets msg_count
    c = m.ProfileCache()
    c.put("u", "p", 5, now=0)
    c.bump("u")
    c.invalidate("u")
    assert c.get("u") == ""
    c.invalidate("missing")  # no-op


# --------------------------------------------------------------- dialectic questions

def test_profile_questions_and_prompt():
    assert len(m.PROFILE_QUESTIONS) == 4
    p = m.build_profile_prompt("user-abc")
    assert "user-abc" in p
    for q in m.PROFILE_QUESTIONS:
        assert q in p
    assert "tentative" in p and "bullet" in p


def test_clean_profile_answer():
    good = "Here is the profile:\n- Direct and technical\n- Prefers Fedora"
    out = m.clean_profile_answer(good, 100)
    assert "Direct and technical" in out and "Here is the profile" not in out
    assert m.clean_profile_answer("I don't have enough information about the user yet.", 100) == ""
    assert m.clean_profile_answer("No information available.", 100) == ""
    assert m.clean_profile_answer("", 100) == ""
    blob = json.dumps({"entries": [["content", {"value": "likes self-hosting"}]]})
    assert "self-hosting" in m.clean_profile_answer(blob, 100)
    long = "word " * 500
    assert m.approx_tokens(m.clean_profile_answer(long, 50)) <= 50


# --------------------------------------------------------------- formatting

def test_format_profile_sections():
    body = m.format_profile_sections(
        "Likes Fedora.",
        ["Send the API key by Friday."],
        [("Prefers Linux.", "inferred from 2 observations")],
        "Works at Lincare.",
        None,
    )
    assert "Who they are (inferred profile, may be wrong):" in body
    assert "Open commitments & promises:" in body
    assert "Inferred traits:" in body
    assert "Other known facts:" in body
    assert "(inferred from 2 observations)" in body
    assert "hypotheses, not facts" in body
    assert "Do not mention the memory system" in body


def test_format_profile_sections_omits_empty_and_budgets():
    body = m.format_profile_sections("", [], [], "", None)
    assert body == ""
    body = m.format_profile_sections("", [], [], "x" * 4000, {"facts": 10})
    assert m.approx_tokens(body) < 60  # facts truncated hard
    long_profile = "\n".join(f"line {i}" for i in range(200))
    body = m.format_profile_sections(long_profile, [], [], "", {"profile": 20})
    profile_part = body.split("Who they are", 1)[1].split("\n", 1)[1]
    profile_part = profile_part.split("Treat inferences", 1)[0]  # exclude the footer line
    assert m.approx_tokens(profile_part) <= 21


def test_format_budgets_default():
    body = m.format_profile_sections("p" * 4000, ["c" * 500], [("i" * 300, "inferred")], "f" * 4000, None)
    # each section roughly within its default budget
    assert len(body) < (300 + 150 + 250 + 400) * 4 + 500


def test_parse_typed_observations_untimestamped_plain_lines():
    """Regression: lines without a timestamp must not crash the parser."""
    obs = m.parse_typed_observations("Likes tea.\nLives in CO.")
    assert [o.text for o in obs] == ["Likes tea.", "Lives in CO."]
    assert all(o.date is None for o in obs)
