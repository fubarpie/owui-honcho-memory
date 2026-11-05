"""Pure helper functions (no I/O, no network) for the Honcho memory filter."""

import copy
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
