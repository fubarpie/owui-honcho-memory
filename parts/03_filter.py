import asyncio
import hashlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

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
        mask_peer_id: bool = Field(
            default=True,
            description="Replace the raw peer id (user-<uuid>) in injected memory with user_label.",
        )
        user_label: str = Field(
            default="the user",
            description="Label used instead of the peer id. Use '{name}' for the Open WebUI display name.",
        )
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
            if self.valves.mask_peer_id:
                label = self.valves.user_label.replace("{name}", str(user.get("name") or "the user"))
                rep = mask_peer_ids(rep, [user_pid], label) if rep else rep
                card = [mask_peer_ids(c, [user_pid], label) for c in card] if card else card
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
