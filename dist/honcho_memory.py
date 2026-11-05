"""
title: Honcho Memory
author: fubarpie
author_url: https://github.com/fubarpie
funding: none
version: 0.1.0
license: MIT
description: Open WebUI filter that gives every chat persistent, reasoning-based
  memory via Honcho (https://honcho.dev). Injects a <honcho_memory> block of peer
  context (representation, peer card, recent conclusions) into the system prompt on
  every turn (inlet), and saves user + assistant messages to a Honcho session for
  async background reasoning (outlet). One workspace per install, one peer per OWUI
  user, one session per chat. Supports /memory chat commands and per-user opt-out
  via UserValves. Degrades silently: any Honcho failure leaves the chat untouched.
requirements:
"""

import asyncio
import json
import time
from typing import Any, ClassVar
from urllib.parse import quote
import httpx
import copy
import re
from typing import Any
import hashlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pydantic import BaseModel, Field


# ---- 01_client.py ----
class HonchoError(Exception):
    """Error raised for any failed Honcho call (HTTP error or transport failure).

    Attributes:
        status: HTTP status code, or ``None`` for transport-level failures.
        detail: Short, sanitized description (never contains the API token).
    """

    def __init__(self, status: int | None, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(
            f"Honcho error {status}: {detail}" if status is not None else f"Honcho error: {detail}"
        )


class HonchoClient:
    """Minimal async client for the Honcho v3 REST API.

    A fresh ``httpx.AsyncClient`` is used per request, so the client is safe to
    use from any event loop. Idempotent calls (GETs, get-or-create) are retried
    up to ``max_retries`` times on 5xx / transport errors; message POSTs and other
    non-idempotent writes are never retried.
    """

    # Process-wide TTL caches: key -> expiry (monotonic seconds). Keys include
    # base_url and workspace so differently-configured clients never collide.
    _known_workspaces: ClassVar[dict[tuple, float]] = {}
    _known_peers: ClassVar[dict[tuple, float]] = {}
    _known_sessions: ClassVar[dict[tuple, float]] = {}
    CACHE_TTL: float = 600.0
    _MAX_DETAIL = 300

    def __init__(
        self,
        base_url: str,
        token: str,
        workspace_id: str,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self._token = token or ""
        self.workspace_id = workspace_id
        self.timeout = timeout
        self._transport = transport
        self.max_retries = 2
        self.backoff = 0.3  # seconds; doubled per attempt

    # ------------------------------------------------------------------ internals
    def _scrub(self, text: str) -> str:
        """Remove the token from ``text`` and bound its length."""
        if self._token:
            text = text.replace(self._token, "***")
        return text[: self._MAX_DETAIL]

    def _ws(self) -> str:
        return f"/v3/workspaces/{quote(self.workspace_id, safe='')}"

    def _peer(self, peer_id: str) -> str:
        return f"{self._ws()}/peers/{quote(peer_id, safe='')}"

    def _session(self, session_id: str) -> str:
        return f"{self._ws()}/sessions/{quote(session_id, safe='')}"

    def _cache_key(self, *parts: str) -> tuple:
        return (self.base_url, self.workspace_id, *parts)

    def _is_cached(self, cache: dict[tuple, float], key: tuple) -> bool:
        expiry = cache.get(key)
        if expiry is None:
            return False
        if expiry < time.monotonic():
            cache.pop(key, None)
            return False
        return True

    def _remember(self, cache: dict[tuple, float], key: tuple) -> None:
        cache[key] = time.monotonic() + self.CACHE_TTL

    @staticmethod
    def _drop_none(params: dict[str, Any] | None) -> dict[str, Any] | None:
        if not params:
            return None
        out = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items() if v is not None}
        return out or None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        retry: bool = False,
    ) -> Any:
        """Send one request; return decoded JSON (or ``None`` for empty bodies).

        Raises:
            HonchoError: on non-2xx responses or transport failures.
        """
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        attempts = (self.max_retries + 1) if retry else 1
        last: HonchoError | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self.backoff * (2 ** (attempt - 1)))
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=self.timeout,
                    transport=self._transport,
                    headers=headers,
                ) as client:
                    resp = await client.request(
                        method, path, json=json_body, params=self._drop_none(params)
                    )
            except httpx.HTTPError as exc:
                # Message deliberately excludes str(exc): it may echo URLs/headers.
                last = HonchoError(None, self._scrub(f"{type(exc).__name__} contacting Honcho"))
                if retry:
                    continue
                raise last from None
            if resp.status_code >= 400:
                last = HonchoError(resp.status_code, self._scrub(self._detail(resp)))
                if retry and resp.status_code >= 500:
                    continue
                raise last
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                raise HonchoError(resp.status_code, "invalid JSON in response") from None
        assert last is not None
        raise last

    @staticmethod
    def _detail(resp: httpx.Response) -> str:
        try:
            data = resp.json()
        except ValueError:
            return resp.text or resp.reason_phrase
        if isinstance(data, dict) and "detail" in data:
            d = data["detail"]
            return d if isinstance(d, str) else json.dumps(d)
        return json.dumps(data)

    # ------------------------------------------------------------------ health / ensure
    async def health(self) -> bool:
        """Return True if ``GET /health`` succeeds; never raises."""
        try:
            await self._request("GET", "/health", retry=False)
            return True
        except HonchoError:
            return False

    async def ensure_workspace(self) -> None:
        """Get-or-create the configured workspace (network once per process/TTL)."""
        key = self._cache_key()
        if self._is_cached(self._known_workspaces, key):
            return
        await self._request("POST", "/v3/workspaces", json_body={"id": self.workspace_id}, retry=True)
        self._remember(self._known_workspaces, key)

    async def ensure_peer(
        self,
        peer_id: str,
        metadata: dict | None = None,
        configuration: dict | None = None,
    ) -> None:
        """Get-or-create a peer (cached)."""
        key = self._cache_key(peer_id)
        if self._is_cached(self._known_peers, key):
            return
        body: dict[str, Any] = {"id": peer_id}
        if metadata:
            body["metadata"] = metadata
        if configuration:
            body["configuration"] = configuration
        await self._request("POST", f"{self._ws()}/peers", json_body=body, retry=True)
        self._remember(self._known_peers, key)

    async def ensure_session(self, session_id: str, peers: dict, metadata: dict | None = None) -> None:
        """Get-or-create a session with the given peer configs (cached)."""
        key = self._cache_key(session_id)
        if self._is_cached(self._known_sessions, key):
            return
        body: dict[str, Any] = {"id": session_id, "peers": peers}
        if metadata:
            body["metadata"] = metadata
        await self._request("POST", f"{self._ws()}/sessions", json_body=body, retry=True)
        self._remember(self._known_sessions, key)

    # ------------------------------------------------------------------ messages
    async def add_messages(self, session_id: str, messages: list[dict]) -> list[dict]:
        """Append messages to a session. NEVER retried (would duplicate messages)."""
        data = await self._request(
            "POST",
            f"{self._session(session_id)}/messages",
            json_body={"messages": messages},
            retry=False,
        )
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------ context
    async def get_peer_context(
        self,
        peer_id: str,
        search_query: str | None = None,
        max_conclusions: int = 25,
        target: str | None = None,
    ) -> dict:
        """Fetch representation + peer card for ``peer_id``."""
        data = await self._request(
            "GET",
            f"{self._peer(peer_id)}/context",
            params={"search_query": search_query, "max_conclusions": max_conclusions, "target": target},
            retry=True,
        )
        return data if isinstance(data, dict) else {}

    async def get_session_context(
        self,
        session_id: str,
        tokens: int | None = None,
        peer_target: str | None = None,
        search_query: str | None = None,
        summary: bool = True,
    ) -> dict:
        """Fetch session context (messages, summary, peer representation/card)."""
        data = await self._request(
            "GET",
            f"{self._session(session_id)}/context",
            params={
                "tokens": tokens,
                "peer_target": peer_target,
                "search_query": search_query,
                "summary": summary,
            },
            retry=True,
        )
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ card / representation
    async def get_card(self, peer_id: str) -> list[str]:
        """Return the peer card lines (empty list when none)."""
        data = await self._request("GET", f"{self._peer(peer_id)}/card", retry=True)
        card = data.get("peer_card") if isinstance(data, dict) else None
        return list(card) if card else []

    async def set_card(self, peer_id: str, lines: list[str]) -> None:
        """Replace the peer card (idempotent PUT, retried)."""
        await self._request(
            "PUT", f"{self._peer(peer_id)}/card", json_body={"peer_card": list(lines)}, retry=True
        )

    async def representation(
        self, peer_id: str, search_query: str | None = None, max_conclusions: int = 25
    ) -> str:
        """Return the peer representation text (``""`` if empty)."""
        body: dict[str, Any] = {"max_conclusions": max_conclusions}
        if search_query:
            body["search_query"] = search_query
        data = await self._request("POST", f"{self._peer(peer_id)}/representation", json_body=body)
        rep = data.get("representation") if isinstance(data, dict) else None
        return rep or ""

    async def chat(
        self,
        peer_id: str,
        query: str,
        reasoning_level: str = "low",
        session_id: str | None = None,
    ) -> str:
        """Ask the dialectic endpoint a question about the peer (non-streaming)."""
        body: dict[str, Any] = {"query": query, "stream": False, "reasoning_level": reasoning_level}
        if session_id:
            body["session_id"] = session_id
        data = await self._request("POST", f"{self._peer(peer_id)}/chat", json_body=body)
        content = data.get("content") if isinstance(data, dict) else None
        return content or ""

    # ------------------------------------------------------------------ conclusions
    async def create_conclusion(
        self, observer_id: str, observed_id: str, content: str, session_id: str | None = None
    ) -> list[dict]:
        """Create one conclusion (not retried: a retry could duplicate it)."""
        item: dict[str, Any] = {"content": content, "observer_id": observer_id, "observed_id": observed_id}
        if session_id:
            item["session_id"] = session_id
        data = await self._request(
            "POST", f"{self._ws()}/conclusions", json_body={"conclusions": [item]}
        )
        return data if isinstance(data, list) else []

    async def list_conclusions(
        self, observer_id: str, observed_id: str, page: int = 1, size: int = 50
    ) -> list[dict]:
        """List conclusions for an observer/observed pair (returns the page items)."""
        data = await self._request(
            "POST",
            f"{self._ws()}/conclusions/list",
            json_body={"filters": {"observer_id": observer_id, "observed_id": observed_id}},
            params={"page": page, "size": size},
            retry=True,
        )
        return list(data.get("items") or []) if isinstance(data, dict) else []

    async def query_conclusions(
        self, observer_id: str, observed_id: str, query: str, top_k: int = 10
    ) -> list[dict]:
        """Semantic search over conclusions (observer and observed are required)."""
        data = await self._request(
            "POST",
            f"{self._ws()}/conclusions/query",
            json_body={
                "query": query,
                "top_k": top_k,
                "filters": {"observer_id": observer_id, "observed_id": observed_id},
            },
            retry=True,
        )
        return data if isinstance(data, list) else []

    async def delete_conclusion(self, conclusion_id: str) -> None:
        """Delete a conclusion by id."""
        await self._request("DELETE", f"{self._ws()}/conclusions/{quote(conclusion_id, safe='')}")

    # ------------------------------------------------------------------ misc
    async def schedule_dream(
        self, observer: str, observed: str | None = None, session_id: str | None = None
    ) -> None:
        """Schedule a (omni) dream consolidation pass."""
        body: dict[str, Any] = {"observer": observer, "dream_type": "omni"}
        if observed:
            body["observed"] = observed
        if session_id:
            body["session_id"] = session_id
        await self._request("POST", f"{self._ws()}/schedule_dream", json_body=body)

    async def queue_status(self) -> dict:
        """Return background-queue status counters for the workspace."""
        data = await self._request("GET", f"{self._ws()}/queue/status", retry=True)
        return data if isinstance(data, dict) else {}

    async def delete_session(self, session_id: str) -> None:
        """Delete a session and forget it from the local cache."""
        await self._request("DELETE", self._session(session_id))
        self._known_sessions.pop(self._cache_key(session_id), None)


# ---- 02_helpers.py ----
"""Pure helper functions (no I/O, no network) for the Honcho memory filter."""


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


# ---- 03_filter.py ----
_log = logging.getLogger("honcho_memory")

_HELP_TEXT = (
    "Memory commands:\n"
    "- `/memory` or `/memory show`: show what is remembered about you\n"
    "- `/memory remember <fact>`: store a fact about you\n"
    "- `/memory forget [text|id]`: find a stored fact and delete it by id\n"
    "- `/memory ask <question>`: ask the memory system a question about you\n"
    "- `/memory off` / `/memory on`: pause or resume memory (in-memory switch only)\n"
    "- `/memory help`: show this help"
)

_OFF_LIMITS = (
    "Limits of `/memory off`: it is kept only in the server's RAM, so it is reset when "
    "Open WebUI restarts or the filter is reloaded. It stops recalling and saving new messages "
    "for you, but it does NOT delete anything already stored. For a permanent opt-out, turn off "
    "'enabled' in this filter's user settings (UserValves)."
)


class Filter:
    """Honcho long-term memory filter for Open WebUI.

    Test hook: tests may either set ``filter._client`` to a fake client directly (it is then
    used as-is and never rebuilt) or override ``_get_client()``.
    """

    class Valves(BaseModel):
        """Admin settings."""

        priority: int = Field(default=0, description="Filter priority (lower runs first).")
        honcho_url: str = Field(default="http://localhost:8000", description="Honcho base URL.")
        api_key: str = Field(default="", description="Honcho bearer token (JWT).")
        workspace_id: str = Field(default="open-webui", description="Honcho workspace id.")
        peer_prefix: str = Field(default="user-", description="Prefix for user peer ids.")
        per_model_assistant_peer: bool = Field(
            default=True, description="One assistant peer per model (else a single 'assistant')."
        )
        inject_position: str = Field(default="system", description="'system' or 'user'.")
        enable_injection: bool = Field(default=True, description="Inject recalled memory.")
        enable_saving: bool = Field(default=True, description="Save messages to Honcho.")
        inject_peer_card: bool = Field(default=True, description="Include the peer card.")
        inject_representation: bool = Field(default=True, description="Include the representation.")
        context_max_chars: int = Field(default=3000, description="Max chars of injected memory.")
        search_top_k: int = Field(default=10, description="Candidates shown for /memory forget.")
        max_conclusions: int = Field(default=25, description="Max conclusions fetched per turn.")
        save_assistant_messages: bool = Field(
            default=True, description="Also save assistant replies."
        )
        save_max_chars: int = Field(default=4000, description="Max chars saved per message.")
        timeout_seconds: float = Field(default=10.0, description="HTTP timeout for Honcho calls.")
        show_status: bool = Field(default=True, description="Show status events in the UI.")
        debug: bool = Field(default=False, description="Verbose logging (never logs secrets).")
        mirror_owui_memories: bool = Field(
            default=True,
            description="Roadmap hook, currently unused: reading OWUI built-in memories is not done here.",
        )

    class UserValves(BaseModel):
        """Per-user settings."""

        enabled: bool = Field(default=True, description="Use memory at all.")
        share_memory: bool = Field(default=True, description="Allow saving my messages to memory.")
        show_status: bool = Field(default=True, description="Show memory status in the UI.")
        reasoning_level: str = Field(default="low", description="Reasoning level for /memory ask.")
        extra_instructions: str = Field(
            default="", description="Extra note added to the memory block."
        )

    _LRU_MAX = 1024

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._client: Any = None
        self._client_key: tuple | None = None
        self._saved_ids: OrderedDict[str, None] = OrderedDict()
        self._disabled: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ helpers
    def _get_client(self) -> Any:
        """Return a client, lazily (re)built when url/token/workspace/timeout valves change.

        A client assigned directly to ``self._client`` (key None) is never rebuilt.
        """
        v = self.valves
        key = (v.honcho_url, v.api_key, v.workspace_id, float(v.timeout_seconds))
        if self._client is not None and self._client_key in (None, key):
            return self._client
        self._client = HonchoClient(
            base_url=v.honcho_url,
            token=v.api_key,
            workspace_id=v.workspace_id,
            timeout=float(v.timeout_seconds),
        )
        self._client_key = key
        return self._client

    def _user_valves(self, user: dict | None) -> "Filter.UserValves":
        raw = (user or {}).get("valves") if isinstance(user, dict) else None
        if isinstance(raw, self.UserValves):
            return raw
        try:
            if raw is not None and not isinstance(raw, dict) and hasattr(raw, "model_dump"):
                raw = raw.model_dump()
            if isinstance(raw, dict):
                known = {k: val for k, val in raw.items() if k in self.UserValves.model_fields}
                return self.UserValves(**known)
        except Exception:  # noqa: BLE001
            _log.debug("invalid user valves; using defaults")
        return self.UserValves()

    async def _emit(
        self,
        emitter: Callable[[dict], Awaitable[None]] | None,
        uv: "Filter.UserValves",
        description: str,
        done: bool,
    ) -> None:
        if emitter is None or not (self.valves.show_status and uv.show_status):
            return
        try:
            await emitter({"type": "status", "data": {"description": description, "done": done}})
        except Exception:  # noqa: BLE001
            _log.debug("status emit failed", exc_info=self.valves.debug)

    @staticmethod
    def _chat_id(body: dict, metadata: dict | None) -> str | None:
        """Chat id, or None for temporary chats (no id, or OWUI's 'local:' ids)."""
        cid = (metadata or {}).get("chat_id") or (body.get("metadata") or {}).get("chat_id")
        if not cid or not isinstance(cid, str) or cid.startswith("local:"):
            return None
        return cid

    @staticmethod
    def _model_id(body: dict, model: dict | None) -> str:
        mid = (model or {}).get("id") if isinstance(model, dict) else None
        return str(mid or body.get("model") or "")

    def _peers(self, user: dict, body: dict, model: dict | None) -> tuple[str, str]:
        v = self.valves
        return (
            user_peer_id(v.peer_prefix, user.get("id")),
            assistant_peer_id(self._model_id(body, model), v.per_model_assistant_peer),
        )

    @staticmethod
    def _session_peers(user_pid: str, asst_pid: str) -> dict:
        return {
            user_pid: {"observe_me": True, "observe_others": False},
            asst_pid: {"observe_me": False, "observe_others": False},
        }

    async def _ensure(
        self, client: Any, user: dict, user_pid: str, asst_pid: str, chat_id: str | None
    ) -> None:
        """Best-effort get-or-create of workspace, peers and session; never raises."""
        meta = {"name": user["name"]} if user.get("name") else None
        steps = [
            lambda: client.ensure_workspace(),
            lambda: client.ensure_peer(user_pid, metadata=meta),
            lambda: client.ensure_peer(asst_pid),
        ]
        if chat_id:
            sid = sanitize_id(chat_id)
            steps.append(
                lambda: client.ensure_session(
                    sid, self._session_peers(user_pid, asst_pid), metadata={"source": "open-webui"}
                )
            )
        for step in steps:
            try:
                await step()
            except Exception as exc:  # noqa: BLE001
                _log.warning("honcho ensure step failed: %s", exc)

    @staticmethod
    def _last_index(messages: list, role: str) -> int:
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if isinstance(m, dict) and m.get("role") == role:
                return i
        return -1

    # ------------------------------------------------------------------ inlet
    async def inlet(
        self,
        body: dict,
        __user__: dict | None = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
        __metadata__: dict | None = None,
        __model__: dict | None = None,
    ) -> dict:
        """Inject recalled memory into the request. Never raises; body is returned unchanged on error."""
        uv = self.UserValves()
        try:
            user = __user__ if isinstance(__user__, dict) else {}
            uv = self._user_valves(user)
            if should_skip(body, user, self.valves):
                return body
            messages = body.get("messages") or []
            ui = self._last_index(messages, "user")
            if ui < 0:
                return body
            text = strip_memory_block(extract_text(messages[ui].get("content")))
            uid = str(user.get("id"))
            cmd = parse_command(text)
            if cmd is not None:
                return await self._handle_command(
                    cmd, body, ui, user, uv, __event_emitter__, __metadata__, __model__
                )
            if uid in self._disabled or not self.valves.enable_injection:
                return body

            client = self._get_client()
            user_pid, asst_pid = self._peers(user, body, __model__)
            chat_id = self._chat_id(body, __metadata__)
            await self._emit(__event_emitter__, uv, "Recalling memories…", False)
            await self._ensure(client, user, user_pid, asst_pid, chat_id)
            ctx = await client.get_peer_context(
                user_pid, search_query=text or None, max_conclusions=self.valves.max_conclusions
            )
            ctx = ctx if isinstance(ctx, dict) else {}
            rep = ctx.get("representation") if self.valves.inject_representation else None
            card = ctx.get("peer_card") if self.valves.inject_peer_card else None
            header = "Memory about this user"
            if uv.extra_instructions.strip():
                header += ". " + uv.extra_instructions.strip()
            block = format_memory_block(rep, card, None, self.valves.context_max_chars, header)
            count = len(card or []) + sum(1 for ln in (rep or "").splitlines() if ln.strip())
            if block:
                body["messages"] = inject_into_messages(
                    messages, block, self.valves.inject_position
                )
            await self._emit(
                __event_emitter__, uv, f"Recalled {count} fact{'' if count == 1 else 's'}", True
            )
            return body
        except Exception as exc:  # noqa: BLE001
            _log.warning("honcho inlet failed: %s", exc, exc_info=self.valves.debug)
            await self._emit(__event_emitter__, uv, "Memory unavailable", True)
            return body

    # ------------------------------------------------------------------ commands
    @staticmethod
    def _rewrite(body: dict, ui: int, instruction: str) -> dict:
        """Replace the user's command message with an instruction for the model."""
        msgs = [dict(m) if isinstance(m, dict) else m for m in body["messages"]]
        msgs[ui]["content"] = instruction
        body["messages"] = msgs
        return body

    async def _handle_command(
        self,
        cmd: tuple[str, str],
        body: dict,
        ui: int,
        user: dict,
        uv: "Filter.UserValves",
        emitter: Callable[[dict], Awaitable[None]] | None,
        metadata: dict | None,
        model: dict | None,
    ) -> dict:
        """Run a /memory command and rewrite the user message into a display instruction.

        A filter cannot answer without the LLM, so the action is performed here and the user
        message is replaced by an instruction telling the model to relay the result.
        """
        name, arg = cmd
        uid = str(user.get("id"))
        typed = f"/memory {name}" + (f" {arg}" if arg and name not in ("remember", "ask") else "")
        head = (
            f"The user typed the memory command `{typed}`. It was already handled by the memory "
            "system. Reply briefly with ONLY the result below, formatted as clean markdown, "
            "without adding facts of your own.\n\nRESULT:\n"
        )
        if name == "help":
            return self._rewrite(body, ui, head + _HELP_TEXT)
        if name == "off":
            self._disabled.add(uid)
            return self._rewrite(body, ui, head + "Memory is now OFF for you.\n" + _OFF_LIMITS)
        if name == "on":
            self._disabled.discard(uid)
            return self._rewrite(
                body, ui, head + "Memory is now ON for you again (recall and saving resumed)."
            )
        if uid in self._disabled:
            return self._rewrite(
                body, ui, head + "Memory is currently OFF for you. Use `/memory on` to resume."
            )
        if name not in ("show", "remember", "forget", "ask"):
            return self._rewrite(body, ui, head + f"Unknown command `{name}`.\n\n" + _HELP_TEXT)

        try:
            client = self._get_client()
            user_pid, asst_pid = self._peers(user, body, model)
            chat_id = self._chat_id(body, metadata)
            await self._emit(emitter, uv, "Working on memory command…", False)
            await self._ensure(client, user, user_pid, asst_pid, chat_id)
            sid = sanitize_id(chat_id) if chat_id else None
            if name == "show":
                ctx = await client.get_peer_context(
                    user_pid, max_conclusions=self.valves.max_conclusions
                )
                ctx = ctx if isinstance(ctx, dict) else {}
                card = ctx.get("peer_card") or []
                rep = ctx.get("representation") or ""
                if not card and not rep.strip():
                    result = "Nothing is remembered about the user yet."
                else:
                    result = "Peer card:\n" + (
                        "\n".join(f"- {c}" for c in card) if card else "(empty)"
                    )
                    result += "\n\nRepresentation:\n" + (rep.strip() or "(empty)")
                    result = "Display this memory to the user as-is.\n" + result
            elif name == "remember":
                if not arg:
                    result = "Usage: `/memory remember <fact>`. No fact was given; nothing stored."
                else:
                    await client.create_conclusion(asst_pid, user_pid, arg, session_id=sid)
                    result = f"Stored. Confirm in one short sentence that you will remember: {arg}"
            elif name == "ask":
                if not arg:
                    result = "Usage: `/memory ask <question>`."
                else:
                    answer = await client.chat(
                        user_pid, arg, reasoning_level=uv.reasoning_level, session_id=sid
                    )
                    result = f"Memory's answer to the question '{arg}':\n{answer or '(no answer)'}"
            else:  # forget
                result = await self._forget(client, asst_pid, user_pid, arg)
            await self._emit(emitter, uv, "Memory command done", True)
            return self._rewrite(body, ui, head + result)
        except Exception as exc:  # noqa: BLE001
            _log.warning("honcho command failed: %s", exc, exc_info=self.valves.debug)
            await self._emit(emitter, uv, "Memory unavailable", True)
            return self._rewrite(
                body,
                ui,
                head + "The memory service is unavailable right now; the command did not run.",
            )

    async def _forget(self, client: Any, asst_pid: str, user_pid: str, arg: str) -> str:
        """Delete a conclusion by exact id, else list candidates so the user can pick an id."""
        items: list = []
        if arg:
            items = list(
                await client.query_conclusions(asst_pid, user_pid, arg, self.valves.search_top_k)
            )
        if not items:
            items = list(
                await client.list_conclusions(asst_pid, user_pid, 1, self.valves.search_top_k)
            )
        for it in items:
            if arg and str(it.get("id")) == arg:
                await client.delete_conclusion(arg)
                return f"Deleted memory {arg}: {it.get('content', '')}. Confirm briefly."
        if not items:
            return "No stored memories found to forget."
        lines = "\n".join(f"- `{it.get('id')}`: {it.get('content', '')}" for it in items)
        return (
            "Nothing deleted yet. List these candidates and tell the user to run "
            f"`/memory forget <id>` with the exact id to delete one:\n{lines}"
        )

    # ------------------------------------------------------------------ outlet
    def _remember_saved(self, key: str) -> bool:
        """Record key in the LRU; return False if it was already saved."""
        if key in self._saved_ids:
            self._saved_ids.move_to_end(key)
            return False
        self._saved_ids[key] = None
        while len(self._saved_ids) > self._LRU_MAX:
            self._saved_ids.popitem(last=False)
        return True

    @staticmethod
    def _msg_key(chat_id: str, msg: dict, text: str) -> str:
        mid = msg.get("id")
        if mid:
            return f"{chat_id}:{mid}"
        digest = hashlib.sha1(f"{msg.get('role')}\0{text}".encode()).hexdigest()[:16]
        return f"{chat_id}:h:{digest}"

    async def outlet(
        self,
        body: dict,
        __user__: dict | None = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
        __metadata__: dict | None = None,
        __model__: dict | None = None,
    ) -> dict:
        """Save the latest user + assistant messages in the background. Never raises."""
        try:
            user = __user__ if isinstance(__user__, dict) else {}
            uv = self._user_valves(user)
            v = self.valves
            if should_skip(body, user, v) or not v.enable_saving or not uv.share_memory:
                return body
            if str(user.get("id")) in self._disabled:
                return body
            chat_id = self._chat_id(body, __metadata__)
            if not chat_id:
                return body  # temporary chat: nothing is persisted
            messages = body.get("messages") or []
            ui = self._last_index(messages, "user")
            ai = self._last_index(messages, "assistant")
            if ui < 0:
                return body
            user_text = strip_memory_block(extract_text(messages[ui].get("content")))
            if parse_command(user_text) is not None:
                return body  # commands (and their replies) are never stored
            user_pid, asst_pid = self._peers(user, body, __model__)
            pending: list[tuple[str, dict]] = []
            candidates = [(ui, user_pid)]
            if ai > ui and v.save_assistant_messages:
                candidates.append((ai, asst_pid))
            for idx, pid in candidates:
                msg = messages[idx]
                text = clean_message_for_save(extract_text(msg.get("content")), v.save_max_chars)
                if not text:
                    continue
                key = self._msg_key(chat_id, msg, text)
                if key in self._saved_ids:
                    continue
                payload = {"peer_id": pid, "content": text}
                if msg.get("id"):
                    payload["metadata"] = {"owui_message_id": str(msg["id"])}
                pending.append((key, payload))
            if not pending:
                return body
            for key, _ in pending:
                self._remember_saved(key)
            client = self._get_client()
            task = asyncio.create_task(
                self._save(
                    client, user, user_pid, asst_pid, chat_id, pending, uv, __event_emitter__
                )
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception as exc:  # noqa: BLE001
            _log.warning("honcho outlet failed: %s", exc, exc_info=self.valves.debug)
        return body

    async def _save(
        self,
        client: Any,
        user: dict,
        user_pid: str,
        asst_pid: str,
        chat_id: str,
        pending: list,
        uv: "Filter.UserValves",
        emitter: Callable[[dict], Awaitable[None]] | None,
    ) -> None:
        """Background save; swallows all exceptions and un-marks ids so a later turn can retry."""
        try:
            await self._ensure(client, user, user_pid, asst_pid, chat_id)
            await client.add_messages(sanitize_id(chat_id), [p for _, p in pending])
        except BaseException as exc:  # noqa: BLE001 - must never propagate from a detached task
            for key, _ in pending:
                self._saved_ids.pop(key, None)
            if isinstance(exc, asyncio.CancelledError):
                return
            _log.warning("honcho save failed: %s", exc, exc_info=self.valves.debug)
            try:
                await self._emit(emitter, uv, "Memory unavailable", True)
            except Exception:  # noqa: BLE001
                _log.debug("status emit failed")
