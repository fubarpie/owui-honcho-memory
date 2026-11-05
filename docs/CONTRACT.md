# Contract: owui-honcho-memory (single-file Open WebUI Filter)

Final artifact: `dist/honcho_memory.py`, built by `python build.py` which concatenates
`parts/00_header.py` `01_client.py` `02_helpers.py` `03_filter.py` in order into ONE module namespace.
Rules for parts: NO cross-imports between parts (names are shared after concat). Each part may start with
top-level `import x` / `from x import y` lines; build.py hoists and dedupes them. Allowed deps: stdlib, httpx,
pydantic (all ship with Open WebUI). Python 3.11+. Full type hints, docstrings, never log secrets.

## Verified Honcho v3 API facts (server 3.5.0, Bearer JWT; base path /v3; see docs/api_summary.md, docs/openapi.json)
- POST /v3/workspaces {"id"} get-or-create (201). POST /v3/workspaces/{ws}/peers {"id","metadata","configuration"} get-or-create.
- POST .../sessions {"id","peers":{peer_id:{"observe_me":bool,"observe_others":bool}},"metadata"} get-or-create.
- POST .../sessions/{sid}/messages {"messages":[{"peer_id","content","metadata","created_at"}]} -> list of messages. Ingest triggers background reasoning.
- GET .../peers/{pid}/context?search_query=&max_conclusions=&search_top_k=&include_most_frequent=&target= -> {"peer_id","target_id","representation": str|null,"peer_card": [str]|null}
- GET .../sessions/{sid}/context?tokens=&summary=&peer_target=&search_query=... -> {"id","messages","summary","peer_representation","peer_card"}
- GET/PUT .../peers/{pid}/card -> {"peer_card":[str]|null} ; PUT body {"peer_card":[...]}
- POST .../peers/{pid}/representation {"search_query","max_conclusions"} -> {"representation": str}
- POST .../peers/{pid}/chat {"query","stream":false,"reasoning_level":"minimal|low|medium|high|max","session_id"} (dialectic Q&A about the user)
- POST .../conclusions {"conclusions":[{"content","observer_id","observed_id","session_id"}]}; POST .../conclusions/list; POST .../conclusions/query {"query","top_k","filters":{"observer_id","observed_id"}} (observer+observed REQUIRED in filters); DELETE .../conclusions/{id}
- POST .../peers/{pid}/search {"query","limit"}; POST .../workspaces/{ws}/schedule_dream; GET .../queue/status
- Errors: 401 {"detail":"No access token provided"}; 500 {"detail":"An unexpected error occurred"} (seen on test server for chat/search/conclusion create when its LLM/embeddings backend is unconfigured) -> client MUST raise HonchoError and callers MUST degrade gracefully (never break the user's chat).
- Empty states are normal: representation "" and peer_card null for new users.

## Identity mapping
workspace = Valve `workspace_id` (default "open-webui"); user peer id = `{peer_prefix}{owui_user_id}` (default prefix "user-");
assistant peer id = `assistant-{sanitized model id}` (or fixed "assistant" if valve `per_model_assistant_peer` false); session id = OWUI chat_id
(for temporary/no chat_id: skip saving, still inject). IDs sanitized to [A-Za-z0-9_-], max 100 chars.
Session peers: user observe_me=True, observe_others=False; assistant observe_me=False, observe_others=False.

## 01_client.py  -> `class HonchoError(Exception)` (status:int|None, detail:str) and `class HonchoClient`
`HonchoClient(base_url:str, token:str, workspace_id:str, timeout:float=10.0, transport: httpx.AsyncBaseTransport|None=None)`; async methods using one
httpx.AsyncClient per call (or lazily-created client; must be safe in OWUI's event loop), retries: 2 retries with backoff on 5xx/connect errors for idempotent calls only (GETs and get-or-create), never retry message POST.
Methods: `health()->bool`, `ensure_workspace()`, `ensure_peer(peer_id, metadata=None, configuration=None)`, `ensure_session(session_id, peers:dict, metadata=None)`,
`add_messages(session_id, messages:list[dict])`, `get_peer_context(peer_id, search_query=None, max_conclusions=25, target=None)->dict`,
`get_session_context(session_id, tokens=None, peer_target=None, search_query=None, summary=True)->dict`, `get_card(peer_id)->list[str]`, `set_card(peer_id, lines)`,
`representation(peer_id, search_query=None, max_conclusions=25)->str`, `chat(peer_id, query, reasoning_level="low", session_id=None)->str`,
`create_conclusion(observer_id, observed_id, content, session_id=None)`, `list_conclusions(observer_id, observed_id, page=1, size=50)->list[dict]`,
`query_conclusions(observer_id, observed_id, query, top_k=10)->list[dict]`, `delete_conclusion(conclusion_id)`, `schedule_dream(observer, observed=None, session_id=None)`, `queue_status()->dict`,
`delete_session(session_id)`. Add small in-process TTL cache sets (`_known_peers`, `_known_sessions`) so ensure_* hits the network once per process.
Token is sent only as `Authorization: Bearer`; HonchoError strings must never contain the token.

## 02_helpers.py -> pure functions (no I/O, no network)
`sanitize_id(s:str, max_len:int=100)->str`, `user_peer_id(prefix, user_id)->str`, `assistant_peer_id(model_id, per_model:bool)->str`,
`strip_memory_block(text:str)->str` (removes our injected `<honcho_memory>...</honcho_memory>` block, and any in-message copies),
`extract_text(content)->str` (OWUI content may be str or list of parts with {"type":"text","text"}), `clean_message_for_save(text, max_chars)->str` (strip memory blocks, <details> reasoning blocks, `<think>` blocks, collapse whitespace, truncate),
`format_memory_block(representation:str|None, card:list[str]|None, recent_conclusions:list[str]|None, max_chars:int, header:str)->str` (returns "" if nothing to inject; wraps in `<honcho_memory>`...`</honcho_memory>` with instructions line "Background facts about the user; use naturally, do not mention unless relevant."),
`inject_into_messages(messages:list[dict], block:str, position:str)->list[dict]` (position "system" merges into/creates first system message; replaces prior block, idempotent; "user" prepends to last user message; never mutates input),
`approx_tokens(text)->int` (len//4), `truncate_to_tokens(text, n)->str`,
`parse_command(text)->tuple[str,str]|None` for chat commands starting with `/memory` (e.g. `/memory`, `/memory show`, `/memory remember <fact>`, `/memory forget`, `/memory off|on`, `/memory ask <q>`, `/memory help`) -> (cmd, arg),
`should_skip(body:dict, user:dict|None, valves)->str|None` (reason string for skipping: task/title generation requests via metadata["task"], missing user, empty prompt, etc.),
`sync_card_with_memory_notes(...)` NOT required.

## 03_filter.py -> `class Filter` per OWUI conventions
`class Valves(BaseModel)`: honcho_url (default http://localhost:8000), api_key (str, secret-ish, default ""), workspace_id, peer_prefix, per_model_assistant_peer(bool), inject_position("system"|"user"),
enable_injection, enable_saving, inject_peer_card, inject_representation, context_max_chars(int 3000), search_top_k/max_conclusions, save_assistant_messages(bool True), save_max_chars,
timeout_seconds, show_status(bool), debug(bool), priority(int 0), mirror_owui_memories(bool, default True: also read OWUI built-in memories is NOT done here; instead leave hook for roadmap -> keep valve but document).
`class UserValves(BaseModel)`: enabled(bool True), share_memory(bool True), show_status(bool True), reasoning_level, extra_instructions(str).
`async inlet(body, __user__, __event_emitter__=None, __metadata__=None, __model__=None)`: skip logic; handle `/memory` commands (answer WITHOUT calling the LLM is not possible in a filter, so instead rewrite the user message into an instruction and/or perform the action and inject a note; document this); ensure workspace/peer/session (best effort), fetch peer context (search_query = last user message), build block, inject, emit status "Recalling memories…" then "Recalled N facts" (done=True). Any exception -> log + status "Memory unavailable" + return body unchanged.
`async outlet(body, __user__, __event_emitter__=None, __metadata__=None, __model__=None)`: save the latest user message and the final assistant message to session via add_messages (fire-and-forget with asyncio.create_task + exception swallowing, so UI is never delayed), idempotent per message id (keep a small LRU of saved OWUI message ids), respects UserValves.enabled/share_memory.
`stream()` not used. Must work when `__user__` lacks valves (use defaults). All Honcho failures degrade silently to the user (status only).
