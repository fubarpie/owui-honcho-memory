import asyncio
import json
import time
from typing import Any, ClassVar
from urllib.parse import quote

import httpx


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
