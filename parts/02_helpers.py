"""Pure helper functions (no I/O, no network) for the Honcho memory filter."""

import copy
import json
import re
from typing import Any

_MEM_OPEN = "<honcho_memory>"
_MEM_CLOSE = "</honcho_memory>"
_MEMORY_BLOCK_RE = re.compile(r"<honcho_memory>.*?</honcho_memory>", re.DOTALL | re.IGNORECASE)
# An unterminated opening tag (e.g. truncated copy) swallows everything after it.
_MEMORY_OPEN_RE = re.compile(r"<honcho_memory>.*\Z", re.DOTALL | re.IGNORECASE)
_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think(?:ing)?>.*\Z", re.DOTALL | re.IGNORECASE)
_REASONING_DETAILS_RE = re.compile(
    r"<details\b[^>]*\btype\s*=\s*[\"']?reasoning[\"']?[^>]*>.*?</details>",
    re.DOTALL | re.IGNORECASE,
)
_COMMAND_RE = re.compile(r"^\s*/memory(?:\s+(\S+))?(?:\s+(.*))?\s*$", re.DOTALL | re.IGNORECASE)
_MEMORY_NOTE = "Background facts about the user; use naturally, do not mention unless relevant."


def sanitize_id(s: str, max_len: int = 100) -> str:
    """Replace chars outside [A-Za-z0-9_-] with '_' and cap length; empty -> 'unknown'."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(s) if s is not None else "")
    cleaned = cleaned[: max(int(max_len), 1)]
    return cleaned or "unknown"


def user_peer_id(prefix: str, user_id: Any) -> str:
    """Build the sanitized Honcho peer id for an OWUI user."""
    return sanitize_id(f"{prefix or ''}{user_id if user_id is not None else ''}")


def assistant_peer_id(model_id: str | None, per_model: bool) -> str:
    """Build the assistant peer id (fixed 'assistant' unless per_model and a model id is given)."""
    if not per_model or not model_id:
        return "assistant"
    return sanitize_id(f"assistant-{model_id}")


def strip_memory_block(text: str | None) -> str:
    """Remove every <honcho_memory>...</honcho_memory> block (and unterminated openers)."""
    if not text:
        return ""
    out = _MEMORY_BLOCK_RE.sub("", text)
    out = _MEMORY_OPEN_RE.sub("", out)
    return out.strip()


def extract_text(content: Any) -> str:
    """Return plain text from OWUI message content (str, list of parts, or None)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict) and part.get("type", "text") == "text":
                t = part.get("text")
                if isinstance(t, str):
                    pieces.append(t)
        return "\n".join(pieces)
    return ""


def clean_message_for_save(text: str | None, max_chars: int = 4000) -> str:
    """Strip memory/reasoning blocks, collapse whitespace and truncate to max_chars."""
    if not text:
        return ""
    out = strip_memory_block(text)
    out = _REASONING_DETAILS_RE.sub("", out)
    out = _THINK_RE.sub("", out)
    out = _THINK_OPEN_RE.sub("", out)
    out = re.sub(r"[ \t\r\f\v]+", " ", out)
    out = re.sub(r" ?\n ?", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    if max_chars is not None and max_chars > 0 and len(out) > max_chars:
        out = out[:max_chars].rstrip()
    return out


def mask_peer_ids(text: str | None, peer_ids: list[str] | tuple[str, ...], label: str) -> str:
    """Replace raw Honcho peer ids (e.g. 'user-<uuid>') with a readable label.

    Honcho's extraction prompts name the user by peer id, which wastes context and
    reads badly. Longest ids are replaced first; blank ids/labels are no-ops.
    """
    out = text or ""
    label = (label or "").strip()
    if not label:
        return out
    for pid in sorted({p for p in peer_ids if p}, key=len, reverse=True):
        out = out.replace(pid, label)
    return out


def format_memory_block(
    representation: str | None,
    card: list[str] | None,
    recent_conclusions: list[str] | None,
    max_chars: int,
    header: str = "Memory",
) -> str:
    """Render the injectable memory block; '' if there is nothing to inject.

    Truncation happens on line boundaries so total length (tags included) stays
    <= max_chars. max_chars <= 0 yields ''.
    """
    rep = (representation or "").strip()
    card_lines = [c.strip() for c in (card or []) if isinstance(c, str) and c.strip()]
    recent = [c.strip() for c in (recent_conclusions or []) if isinstance(c, str) and c.strip()]
    if not (rep or card_lines or recent):
        return ""
    if max_chars is None or max_chars <= 0:
        return ""

    body: list[str] = []
    if card_lines:
        body.append("Profile:")
        body.extend(f"- {c}" for c in card_lines)
    if rep:
        body.append("Known about the user:")
        body.extend(line.rstrip() for line in rep.splitlines() if line.strip())
    if recent:
        body.append("Recent:")
        body.extend(f"- {c}" for c in recent)

    head = [_MEM_OPEN]
    if header and header.strip():
        head.append(header.strip())
    head.append(_MEMORY_NOTE)

    budget = max_chars - len("\n".join(head + [_MEM_CLOSE])) - 2  # newlines around body
    kept: list[str] = []
    used = 0
    for line in body:
        cost = len(line) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    # Drop dangling section titles that lost all their items.
    while kept and kept[-1].endswith(":"):
        kept.pop()
    if not kept:
        return ""
    return "\n".join(head + kept + [_MEM_CLOSE])


def _strip_content(content: Any) -> Any:
    """Strip memory blocks from str/list content, preserving structure."""
    if isinstance(content, str):
        return strip_memory_block(content)
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                text = strip_memory_block(part["text"])
                if not text and part["text"]:
                    continue  # part was only our block
                part = {**part, "text": text}
            out.append(part)
        return out
    return content


def _content_is_empty(content: Any) -> bool:
    return not content or (isinstance(content, str) and not content.strip())


def inject_into_messages(messages: list[dict] | None, block: str, position: str = "system") -> list[dict]:
    """Return a copy of messages with `block` injected; idempotent, input untouched.

    Any prior <honcho_memory> block is removed from every message first.
    position 'system': appended to the first system message (created at index 0 if absent).
    position 'user': prepended to the last user message.
    """
    msgs = copy.deepcopy(messages) if messages else []
    cleaned: list[dict] = []
    for m in msgs:
        if not isinstance(m, dict):
            cleaned.append(m)
            continue
        had_block = _MEM_OPEN in extract_text(m.get("content")).lower()
        if "content" in m:
            m["content"] = _strip_content(m["content"])
        # A system message that only held our old block is dropped (recreated below if needed).
        if had_block and m.get("role") == "system" and _content_is_empty(m.get("content")):
            continue
        cleaned.append(m)
    msgs = cleaned
    if not block:
        return msgs

    if position == "user":
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, list):
                    m["content"] = [{"type": "text", "text": block}] + c
                else:
                    c = c or ""
                    m["content"] = f"{block}\n\n{c}" if c else block
                return msgs
        return msgs  # no user message: nothing to inject into

    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "system":
            c = m.get("content")
            if isinstance(c, list):
                m["content"] = c + [{"type": "text", "text": block}]
            else:
                m["content"] = f"{c}\n\n{block}" if c else block
            return msgs
    return [{"role": "system", "content": block}] + msgs


def approx_tokens(text: str | None) -> int:
    """Rough token estimate: len(text) // 4."""
    return len(text or "") // 4


def truncate_to_tokens(text: str | None, n: int) -> str:
    """Truncate to roughly n tokens (4 chars each), preferring a line boundary."""
    if not text or n is None or n <= 0:
        return ""
    limit = n * 4
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl > 0 and text[limit] != "\n":
        cut = cut[:nl]
    return cut.rstrip()


def parse_command(text: str | None) -> tuple[str, str] | None:
    """Parse '/memory [cmd] [arg]' (case-insensitive) -> (cmd lowercase, arg) or None.

    A bare '/memory' maps to ('show', ''). '/memoryx' is not a command.
    """
    if not text or not isinstance(text, str):
        return None
    m = _COMMAND_RE.match(text)
    if not m:
        return None
    cmd = (m.group(1) or "show").lower()
    arg = (m.group(2) or "").strip()
    return cmd, arg


def _last_user_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return strip_memory_block(extract_text(m.get("content"))).strip()
    return ""


def should_skip(body: dict | None, user: dict | None, valves: Any = None) -> str | None:
    """Return a reason string if the filter should do nothing for this request, else None.

    Reasons: 'task:<name>', 'no_user', 'disabled', 'user_disabled', 'no_messages', 'empty_prompt'.
    """
    body = body if isinstance(body, dict) else {}
    meta = body.get("metadata")
    task = meta.get("task") if isinstance(meta, dict) else None
    task = task or body.get("task")
    if task and getattr(valves, "skip_tasks", True):
        return f"task:{task}"
    if not isinstance(user, dict) or not user.get("id"):
        return "no_user"
    if not getattr(valves, "enabled", True):
        return "disabled"
    uv = user.get("valves")
    uv_enabled = uv.get("enabled", True) if isinstance(uv, dict) else getattr(uv, "enabled", True)
    if uv_enabled is False:
        return "user_disabled"
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return "no_messages"
    if not _last_user_text(messages):
        return "empty_prompt"
    return None


# --------------------------------------------------------------------------- curation
# Honcho's "representation" is a dated stream of observations. These helpers distil it
# into a short list of durable, query-relevant facts so we stop re-injecting noise.
_OBS_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\]\s*")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.+#-]*")
_STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with", "from", "by", "as", "is", "are", "was", "were", "be", "been", "being", "it", "its", "this", "that", "these", "those", "user", "users", "their", "his", "her", "they", "them", "he", "she", "we", "you", "i", "me", "my", "our", "your", "do", "does", "did", "has", "have", "had", "will", "would", "can", "could", "should", "may", "might", "about", "into", "over", "also", "not", "no", "yes", "so", "if", "then", "than", "there", "here", "what", "which", "who", "whom", "how", "when", "where", "why", "all", "any", "some", "more", "most", "other", "such", "only", "own", "same", "too", "very", "just"]
)
# One-off session activity: useful for a moment, worthless a week later.
_TRANSIENT_RE = re.compile(
    r"\b(ctrl\s*\+?\s*c|interrupted|traceback|eoferror|stack\s*trace|error message|pasted|"
    r"ran (?:a |the |an )?(?:\w+ )?(?:script|command)|running (?:a |the )?(?:\w+ )?(?:script|command)|"
    r"polling|pending|remaining|stuck at|queue|\d+\s*(?:/|of)\s*\d+|completed \d+|"
    r"(?:asked|wants?) (?:to|for) (?:the )?(?:merge|merged|merging)\b|asked for pr|"
    r"reported (?:on )?\d{4}-\d{2}-\d{2}|at \d{1,2}:\d{2}(?::\d{2})?)\b",
    re.IGNORECASE,
)
_DURABLE_RE = re.compile(
    r"\b(wants?|would like|prefers?|likes?|dislikes?|always|never|uses?|has an?|lives?|works?|"
    r"owns?|maintains?|building|goal|plans?|architecture|decid\w+|roadmap|one \w+ per|self-hosted)\b",
    re.IGNORECASE,
)


def _tokens(text: str) -> set[str]:
    words = (w.rstrip(".+-_") for w in _WORD_RE.findall((text or "").lower()))
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def _similar(a: set[str], b: set[str], threshold: float) -> bool:
    """Jaccard or containment similarity of two token sets."""
    if not a or not b:
        return False
    inter = len(a & b)
    return inter / len(a | b) >= threshold or inter / min(len(a), len(b)) >= 0.8


def _compact(text: str) -> str:
    """Drop the redundant leading 'the user ' and capitalise (saves tokens)."""
    t = re.sub(r"^the user\s+", "", text.strip(), flags=re.IGNORECASE)
    return (t[:1].upper() + t[1:]) if t else text


_JSON_VALUE_RE = re.compile(r'"content"\s*,\s*\{.*?"value"\s*:\s*"((?:[^"\\]|\\.)*)"', re.DOTALL)


def unwrap_observation(text: str) -> str:
    """Recover plain text from an observation the server stored as a structured-output blob.

    Some extraction models leak their wrapper, e.g.
    ``{"completionState":"complete","entries":[["content",{"type":"String","value":"..."}]]}``.
    Returns the inner ``content`` string; ``""`` when a JSON-looking blob has no recoverable
    content (so the caller drops it); plain text is returned unchanged.
    """
    t = (text or "").strip()
    if not t.startswith(("{", "[")):
        return t
    try:
        obj: Any = json.loads(t)
    except ValueError:
        obj = None

    def walk(o: Any) -> str:
        if isinstance(o, dict):
            if isinstance(o.get("content"), str):
                return o["content"]
            for v in o.values():
                r = walk(v)
                if r:
                    return r
        elif isinstance(o, list):
            if len(o) == 2 and o[0] == "content":
                v = o[1]
                if isinstance(v, dict) and isinstance(v.get("value"), str):
                    return v["value"]
                if isinstance(v, str):
                    return v
            for v in o:
                r = walk(v)
                if r:
                    return r
        return ""

    found = walk(obj) if obj is not None else ""
    if not found:
        m = _JSON_VALUE_RE.search(t)
        if m:
            try:
                found = json.loads('"' + m.group(1) + '"')
            except ValueError:
                found = m.group(1)
    return found.strip()


def parse_observations(representation: str | None) -> list[tuple[str, str | None]]:
    """Split a Honcho representation into ``(text, iso_date|None)`` observations.

    Drops section headers ("## Explicit Observations"), "Premises:" sub-lists and blanks.
    """
    out: list[tuple[str, str | None]] = []
    in_premises = False
    for raw in (representation or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or (line.endswith(":") and len(line) < 40 and "[" not in line):
            in_premises = in_premises and line.lower() != "" and not line.startswith("#")
            if line.lower().rstrip(":") == "premises":
                in_premises = True
            continue
        if line.lower().startswith("premises"):
            in_premises = True
            continue
        m = _OBS_TS_RE.match(_BULLET_RE.sub("", line))
        if in_premises and not m:
            continue  # premise detail line
        in_premises = False
        text = _BULLET_RE.sub("", line)
        date = None
        if m:
            date = m.group(1)
            text = text[m.end():]
        text = unwrap_observation(text.strip())
        if text:
            out.append((text, date))
    return out


def is_transient(text: str) -> bool:
    """True for one-off event noise (script runs, interrupts, queue counts...)."""
    return bool(_TRANSIENT_RE.search(text or ""))


def curate_observations(
    representation: str | None,
    query: str | None,
    *,
    known_texts: list[str] | None = None,
    max_tokens: int = 400,
    max_age_days: int = 30,
    always_keep: int = 3,
    today: str | None = None,
) -> list[str]:
    """Distil a representation into durable, relevant, de-duplicated fact lines.

    * transient event noise is dropped;
    * facts already present in ``known_texts`` (peer card, OWUI memories) are dropped;
    * near-duplicates are merged (newest kept);
    * lines overlapping the ``query`` rank first; unrelated lines survive only if durable-looking
      (top ``always_keep``) and, when not durable, only if newer than ``max_age_days``;
    * output fits ``max_tokens`` (approx_tokens) and carries no timestamps.
    """
    import datetime as _dt

    try:
        now = _dt.date.fromisoformat(today) if today else _dt.date.today()
    except ValueError:
        now = _dt.date.today()
    qtok = _tokens(query or "")
    known = [_tokens(k) for k in (known_texts or []) if k]

    cands: list[dict] = []
    for text, date in parse_observations(representation):
        if is_transient(text):
            continue
        tok = _tokens(text)
        if not tok or any(_similar(tok, k, 0.6) for k in known):
            continue
        try:
            age = (now - _dt.date.fromisoformat(date)).days if date else 0
        except ValueError:
            age = 0
        cands.append({"text": text, "tok": tok, "age": max(age, 0), "date": date or ""})

    # Merge near-duplicates, preferring the newest (then longest) phrasing.
    cands.sort(key=lambda c: (c["age"], -len(c["text"])))
    uniq: list[dict] = []
    for c in cands:
        if not any(_similar(c["tok"], u["tok"], 0.7) for u in uniq):
            uniq.append(c)

    scored: list[tuple[float, dict, bool, bool]] = []
    for c in uniq:
        overlap = len(qtok & c["tok"]) / len(qtok) if qtok else 0.0
        relevant = overlap > 0
        durable = bool(_DURABLE_RE.search(c["text"]))
        recency = 0.5 / (1 + c["age"] / 30)
        score = 2.0 * overlap + (0.5 if durable else 0.0) + recency
        scored.append((score, c, relevant, durable))
    scored.sort(key=lambda t: -t[0])

    kept: list[str] = []
    used = 0
    unrelated = 0
    for score, c, relevant, durable in scored:
        if not relevant:
            if not durable and max_age_days and c["age"] > max_age_days:
                continue
            if not durable:
                continue  # unrelated + not clearly durable: skip entirely
            if unrelated >= always_keep:
                continue
            unrelated += 1
        cost = approx_tokens(c["text"]) + 1
        if used + cost > max_tokens:
            continue
        kept.append(_compact(c["text"]))
        used += cost
    return kept


# --------------------------------------------------------------------------- topic grouping
# Default topic paths, mirroring the style of Open WebUI memory paths. Matched by keyword.
DEFAULT_TOPIC_RULES: tuple[tuple[str, frozenset[str]], ...] = (
    ("homelab", frozenset(["proxmox", "lxc", "homelab", "casaos", "docker", "vm", "container", "nas", "ntfy", "webhook", "selfhosted"])),
    ("devices", frozenset(["laptop", "phone", "android", "fedora", "secureblue", "linux", "desktop", "device"])),
    ("work", frozenset(["job", "jobs", "lincare", "manager", "career", "employer"])),
    ("home", frozenset(["airbnb", "hostex", "rental", "guests", "address"])),
    (
        "projects/open-webui",
        frozenset(["openwebui", "open-webui", "filter", "tool", "honcho", "handoff", "plugin", "memory", "function"]),
    ),
    ("projects", frozenset(["repo", "github", "pr", "merge", "roadmap", "project", "build", "app"])),
)
_GENERAL_PATH = "general"


def classify_topic(
    text: str,
    owui_memories: list[tuple[str, str | None]] | None = None,
    rules: tuple[tuple[str, frozenset[str]], ...] = DEFAULT_TOPIC_RULES,
) -> str:
    """Pick a topic path for one fact.

    1. Best token overlap with an Open WebUI memory that has a path (so facts land under the
       paths the user already uses, e.g. ``projects/riffle-accounting``).
    2. Else the keyword rule with most hits (ties -> earlier rule).
    3. Else ``general``.
    """
    tok = _tokens(text)
    best_path, best = None, 0.0
    for content, path in owui_memories or []:
        if not path:
            continue
        mt = _tokens(content) | _tokens(path.replace("/", " ").replace("-", " "))
        if not mt:
            continue
        overlap = len(tok & mt)
        score = overlap / max(len(tok), 1)
        if overlap >= 2 and score > best:
            best_path, best = path, score
    if best_path and best >= 0.25:
        return best_path
    hits_best, rule_path = 0, None
    for path, kws in rules:
        hits = len(tok & kws)
        if hits > hits_best:
            hits_best, rule_path = hits, path
    return rule_path or _GENERAL_PATH


def group_by_topic(
    facts: list[str],
    owui_memories: list[tuple[str, str | None]] | None = None,
) -> dict[str, list[str]]:
    """Group fact lines by topic path, preserving input (relevance) order within and across groups."""
    groups: dict[str, list[str]] = {}
    for f in facts:
        groups.setdefault(classify_topic(f, owui_memories), []).append(f)
    return groups


def format_grouped_facts(groups: dict[str, list[str]], query: str | None = None) -> str:
    """Render ``{path: [facts]}`` as ``path:`` headings with bullet items.

    Groups whose path words overlap the query come first; otherwise first-seen order is kept.
    A lone ``general`` group is rendered flat (a heading adds tokens but no information).
    """
    if not groups:
        return ""
    if list(groups) == [_GENERAL_PATH]:
        return "\n".join(f"- {f}" for f in groups[_GENERAL_PATH])
    qtok = _tokens(query or "")
    order = sorted(
        groups,
        key=lambda p: (-len(qtok & _tokens(p.replace("/", " ").replace("-", " "))), list(groups).index(p)),
    )
    lines: list[str] = []
    for p in order:
        lines.append(f"{p}:")
        lines.extend(f"- {f}" for f in groups[p])
    return "\n".join(lines)
