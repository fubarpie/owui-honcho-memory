"""Typed observations, commitments, inferences, profile cache, and profile sections.

Pure helpers (stdlib only, no I/O). Depends at runtime on helpers defined earlier in
the built module namespace (unwrap_observation, approx_tokens, _tokens, _similar,
is_transient, _compact, truncate_to_tokens) — parts share one namespace after build.
"""


import datetime as _dt
import json
import re
from collections import OrderedDict
from typing import Any, NamedTuple

from typing import Callable, Iterable, Sequence


class Obs(NamedTuple):
    """One observation with its section-derived metadata."""

    text: str
    date: str | None
    kind: str  # 'explicit' | 'deductive' | 'inductive' | 'unknown'
    premise_count: int


_SECTION_KIND_RE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"explicit", re.I), "explicit"),
    (re.compile(r"deduct", re.I), "deductive"),
    (re.compile(r"induct|pattern|tendenc", re.I), "inductive"),
]


def _section_kind(header: str) -> str:
    """Map a section header line to a kind; 'unknown' when unmatched."""
    for pat, kind in _SECTION_KIND_RE:
        if pat.search(header):
            return kind
    return "unknown"


_HEADER_LINE_RE = re.compile(r"^\s*#{1,6}\s*(.*)$")
_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\]\s*")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def parse_typed_observations(representation: str | None) -> list[Obs]:
    """Split a representation into Obs records, tracking section kind and premise counts.

    Honely mirrors parse_observations but keeps the section kind and counts 'Premises:'
    sub-list lines, so commitments/inferences can be weighted by evidence strength.
    """
    out: list[Obs] = []
    kind = "explicit"
    in_premises = False
    for raw in (representation or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _HEADER_LINE_RE.match(line)
        if m:
            kind = _section_kind(m.group(1))
            in_premises = False
            continue
        low = line.lower()
        if low.startswith("premises"):
            in_premises = True
            continue
        text = _BULLET_RE.sub("", _TS_RE.sub("", line))
        text = text.strip()
        if in_premises and not _TS_RE.match(line):
            # premise detail line: count it toward the current observation
            if out:
                out[-1] = out[-1]._replace(premise_count=out[-1].premise_count + 1)
            continue
        in_premises = False  # a timestamped observation line ends any premise list
        if line.endswith(":") and len(line) < 40 and "[" not in line:
            continue
        unwrapped = unwrap_observation(text)
        if not unwrapped:
            continue
        date_m = _TS_RE.match(line)
        out.append(Obs(text=unwrapped, date=(date_m.group(1) or None) if date_m else None, kind=kind, premise_count=0))
    return out


# --------------------------------------------------------------------------- commitments

_COMMITMENT_RE = re.compile(
    r"\b(promis\w*|commit(?:ted|ment)|agree[ds]? to|plans? to|needs? to|owes?|"
    r"will (?:send|deliver|share|provide|get|give|call|write|review|finish|follow up)|"
    r"to-do|todo|deadline|follow[- ]?up|reminder|deliver(?:s|ed)? (?:the|a|an)?)\b"
    r"|\bby (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|next week|tomorrow|today"
    r"|january|february|march|april|may|june|july|august|september|october|november|december)\b",
    re.IGNORECASE,
)
_RESOLVED_RE = re.compile(
    r"\b(done|completed|finished|sent|delivered|cancelled|canceled|no longer|already|"
    r"resolved|abandon\w*|withdr\w+|gave up)\b",
    re.IGNORECASE,
)


def is_commitment(text: str) -> bool:
    """True if the observation reads as a promise, todo, or deadline (third person)."""
    return bool(_COMMITMENT_RE.search(text or ""))


def is_resolved(text: str) -> bool:
    """True if the observation states the commitment was done/cancelled/superseded."""
    return bool(_RESOLVED_RE.search(text or ""))


def _age_days(date: str | None, today: _dt.date) -> int:
    if not date:
        return 0
    try:
        return max((today - _dt.date.fromisoformat(date)).days, 0)
    except ValueError:
        return 0


def select_commitments(
    obs: list[Obs],
    max_items: int = 6,
    max_age_days: int = 90,
    today: str | None = None,
) -> list[str]:
    """Return open (unresolved, recent) commitment texts, newest first, deduped.

    Never filtered by the current query: commitments must always reach the model.
    """
    try:
        now = _dt.date.fromisoformat(today) if today else _dt.date.today()
    except ValueError:
        now = _dt.date.today()
    cands: list[tuple[int, str, frozenset[str]]] = []
    for o in obs:
        if o.kind not in ("explicit", "unknown"):
            continue  # commitments come from explicit statements, not deductions
        if is_transient(o.text) or is_resolved(o.text) or not is_commitment(o.text):
            continue
        if _age_days(o.date, now) > max_age_days:
            continue
        cands.append((_age_days(o.date, now), o.text, frozenset(_tokens(o.text))))
    cands.sort(key=lambda c: c[0])  # newest (smallest age) first
    kept: list[str] = []
    seen: list[frozenset[str]] = []
    for _, text, tok in cands:
        if any(_similar(tok, s, 0.7) for s in seen):
            continue
        kept.append(_compact(text))
        seen.append(tok)
        if len(kept) >= max_items:
            break
    return kept


# --------------------------------------------------------------------------- inferences

def _days_between(later: str, earlier: str) -> int:
    """Days from earlier to later iso date (0 on parse failure or inverted)."""
    try:
        return max((_dt.date.fromisoformat(later) - _dt.date.fromisoformat(earlier)).days, 0)
    except ValueError:
        return 0


def select_inferences(
    obs: list[Obs],
    query: str | None,
    max_tokens: int = 250,
    known_texts: Sequence[str] = (),
) -> list[tuple[str, str]]:
    """Pick deductive/inductive observations as (text, evidence_label), budgeted by tokens.

    Ranked by premise_count, then query relevance, then recency. Exempt from the
    'overlap with the query' rule that applies to ordinary facts.
    """
    qtok = _tokens(query or "")
    known = [_tokens(k) for k in known_texts if k]

    # Newest observation date in the set anchors relative recency (no hidden clock).
    dates = [o.date for o in obs if o.date]
    newest = max(dates) if dates else None

    cands: list[tuple[tuple[int, float, int], Obs]] = []
    cands_seen: list[frozenset[str]] = []
    for o in obs:
        if o.kind not in ("deductive", "inductive"):
            continue
        if is_transient(o.text):
            continue
        tok = frozenset(_tokens(o.text))
        if not tok or any(_similar(tok, k, 0.6) for k in known):
            continue
        if any(_similar(tok, s, 0.8) for s in cands_seen):
            continue
        overlap = len(qtok & tok) / len(qtok) if qtok else 0.0
        recency = _days_between(newest, o.date) if (o.date and newest) else 0
        cands.append(((o.premise_count, overlap, -recency), o))
        cands_seen.append(tok)
    cands.sort(key=lambda c: (-c[0][0], -c[0][1], -c[0][2]))

    out: list[tuple[str, str]] = []
    used = 0
    for _, o in cands:
        text = _compact(o.text)
        label = f"inferred from {o.premise_count} observations" if o.premise_count else "inferred"
        cost = approx_tokens(text) + 1
        if used + cost > max_tokens:
            continue
        out.append((text, label))
        used += cost
    return out


# --------------------------------------------------------------------------- profile cache

class ProfileCache:
    """Bounded in-memory LRU cache of distilled profile text keyed by user id.

    All time is passed in explicitly (no hidden clock) so behaviour is testable.
    """

    def __init__(self, capacity: int = 512) -> None:
        self._capacity = max(1, int(capacity))
        self._data: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    def get(self, uid: str) -> str:
        """Return cached profile text ('' if none); refreshes LRU position."""
        entry = self._data.get(uid)
        if entry is None:
            return ""
        self._data.move_to_end(uid)
        return entry["text"]

    def put(self, uid: str, text: str, msg_count: int, now: float) -> None:
        """Store/replace the profile for uid with metadata (msg_count, timestamp)."""
        self._data[uid] = {"text": text, "msg_count": int(msg_count), "ts": float(now)}
        self._data.move_to_end(uid)
        while len(self._data) > self._capacity:
            self._data.popitem(last=False)

    def needs_refresh(
        self,
        uid: str,
        msg_count: int,
        now: float,
        ttl_seconds: float = 21600,
        every_n_messages: int = 30,
    ) -> bool:
        """True when the entry is missing/stale (ttl) or msg_count jumped by >= every_n."""
        e = self._data.get(uid)
        if e is None:
            return True
        if now - e["ts"] > ttl_seconds:
            return True
        return (int(msg_count) - e["msg_count"]) >= every_n_messages

    def msg_count(self, uid: str) -> int:
        """Messages counted so far for uid (0 if unknown)."""
        e = self._data.get(uid)
        return int(e["msg_count"]) if e else 0

    def bump(self, uid: str) -> None:
        """Count one message seen for uid (creates a stub entry if absent)."""
        e = self._data.get(uid)
        if e is None:
            self._data[uid] = {"text": "", "msg_count": 1, "ts": float("-inf")}
        else:
            e["msg_count"] = e["msg_count"] + 1
            self._data.move_to_end(uid)

    def invalidate(self, uid: str) -> None:
        """Drop the cached profile for uid (no-op when absent)."""
        self._data.pop(uid, None)


# --------------------------------------------------------------------------- dialectic questions

PROFILE_QUESTIONS: list[str] = [
    "What is the user's communication style and preferred answer format (prose, bullets, code-first, level of detail)?",
    "What are the user's main interests and values (e.g. open-source, self-hosting, privacy, specific projects)?",
    "What is the user's technical level and working style (languages, tools, how they like tasks broken down)?",
    "What personality traits and helping style does the user respond best to (e.g. directness, autonomy, delegation to sub-agents)?",
]


def build_profile_prompt(label_or_peer_id: str) -> str:
    """Build one compact prompt asking Honcho to answer all PROFILE_QUESTIONS at once."""
    label = (label_or_peer_id or "the user").strip() or "the user"
    questions = "\n".join(f"- {q}" for q in PROFILE_QUESTIONS)
    return (
        f"Answer the following about {label} in one compact bullet list:\n"
        f"{questions}\n"
        "State only what the evidence supports; mark uncertain items with (tentative). "
        "No names or addresses. Max ~12 bullets, <=150 words per answer. "
        "If there is no evidence for a question, skip it."
    )


_ABSTAIN_RE = re.compile(
    r"\b(no (?:information|evidence|data)|not enough|insufficient|unknown|cannot determine|"
    r"don'?t (?:know|have)|do not (?:know|have)|unable to (?:determine|answer)|no clear|nothing (?:is )?known)\b",
    re.IGNORECASE,
)
_FLUFF_RE = re.compile(
    r"^\s*(?:here(?:'s| is)?|based on|according to|from)\b.*?(?:profile|observation|evidence|data|conversation)s?\s*(?:,|:|for the user)?\s*",
    re.IGNORECASE,
)


def clean_profile_answer(text: str, max_tokens: int = 300) -> str:
    """Clean a profile answer: unwrap JSON, strip fluff, drop abstentions, truncate.

    Returns '' for abstaining answers ('I don't have enough information...').
    """
    t = unwrap_observation(text or "")
    if not t:
        return ""
    t = re.sub(r"\s+", " ", t).strip()
    if _ABSTAIN_RE.search(t) and approx_tokens(t) < 60:
        return ""
    t = _FLUFF_RE.sub("", t)
    t = truncate_to_tokens(t, int(max_tokens))
    return t.strip()


# --------------------------------------------------------------------------- section formatting

_DEFAULT_BUDGETS = {"profile": 300, "commitments": 150, "inferences": 250, "facts": 400}


def format_profile_sections(
    profile: str,
    commitments: list[str],
    inferences: list[tuple[str, str]],
    facts: str,
    budgets: dict[str, int] | None = None,
) -> str:
    """Render the memory block body with labelled sections; empty sections omitted."""
    b = {**_DEFAULT_BUDGETS, **(budgets or {})}
    lines: list[str] = []
    p = truncate_to_tokens((profile or "").strip(), b["profile"])
    if p:
        lines += ["Who they are (inferred profile, may be wrong):", p]
    if commitments:
        cl = truncate_to_tokens("\n".join(f"- {_compact(c)}" for c in commitments), b["commitments"])
        if cl:
            lines += ["Open commitments & promises:"] + cl.splitlines()
    if inferences:
        chunk = "\n".join(f"- {t} ({lbl})" for t, lbl in inferences)
        chunk = truncate_to_tokens(chunk, b["inferences"])
        if chunk:
            lines += ["Inferred traits:"] + chunk.splitlines()
    f = truncate_to_tokens((facts or "").strip(), b["facts"])
    if f:
        lines += ["Other known facts:"] + [ln.rstrip() for ln in f.splitlines() if ln.strip()]
    if not lines:
        return ""
    lines.append(
        "Treat inferences as hypotheses, not facts. Do not mention the memory system unless asked."
    )
    return "\n".join(lines)
