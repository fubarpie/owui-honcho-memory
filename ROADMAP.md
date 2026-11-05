# Roadmap

Phased plan for [owui-honcho-memory](https://github.com/fubarpie/owui-honcho-memory):
a single-file Open WebUI filter giving chats persistent, reasoning-based memory via
[Honcho](https://honcho.dev).

## v0.1 — MVP (shipped)

- [x] Inlet: fetch peer context (representation, peer card, conclusions) and inject a `<honcho_memory>` block into the system prompt.
- [x] Outlet: fire-and-forget save of user + assistant messages; Honcho derives conclusions asynchronously.
- [x] Identity mapping: workspace `open-webui`, one peer per OWUI user, one session per chat, assistant peer per model.
- [x] `/memory` chat commands (show / remember / forget / off / on / ask / help).
- [x] Per-user opt-out UserValves (`enabled`, `share_memory`), graceful degradation on every Honcho failure.

## v0.2 — Companion Tool (~0.2)

- [ ] **Dialectic companion Tool** — expose `chat`, `query_conclusions`, `create_conclusion` as an OWUI Tool so the model can query Honcho on demand (memory *pull*, not just push). The filter injects automatically; a tool lets the model ask when it needs more.
- [ ] **Session summaries for long chats** — use `get_session_context(summary=true)` so old long chats stop blowing the context budget; the filter summarizes instead of replaying.
- [ ] **Token-budget aware injection** — size the `<honcho_memory>` block by `approx_tokens` of remaining context, not just `context_max_chars`.
- [ ] **Status UX polish** — queue-aware status ("deriving…", backlog count) using `queue_status`.

## v0.3 — Sync & migration

- [ ] **Import OWUI built-in memories** — seed Honcho `create_conclusion` from the user's existing Open WebUI memory notes so migration is loss-free on day one.
- [ ] **Two-way sync with OWUI memory** (`mirror_owui_memories` valve already reserved) — keep OWUI's built-in notes and Honcho conclusions reconciled so both ecosystems stay useful.
- [ ] **Peer card editor / auto-maintained card** — surface + edit `get_card`/`set_card` from the UI ("what Honcho thinks of you") and/or auto-maintain the card after each session.
- [ ] **Knowledge-base ingestion** — upload docs as messages/conclusions so Honcho reasons over workspace knowledge, not just chat history.

## v0.4 — Scheduled reasoning

- [ ] **Per-user dream scheduling** — run `schedule_dream` for a user's peers on a timer (nightly cron / admin button) so conclusions improve without new traffic.
- [ ] **Reasoning-level routing** — `reasoning_level=minimal` on every-turn context fetch (cheap), escalate to `high`/`max` only for explicit `/memory ask` or card regeneration.
- [ ] **Async prefetch/caching** — pre-resolve workspace/peer/session + prefetch context for the *next* message in the outlet, cutting inlet latency to near zero.

## v0.5 — Multi-agent & team

- [ ] **Multi-model assistant peers** — one Honcho peer per (model, persona) so switching models keeps a consistent assistant identity with its own conclusions.
- [ ] **Group-chat peers** — treat multi-user OWUI channels as their own Honcho peer (`channel-{id}`) with `observe_others` sessions so Honcho reasons about *conversations*, not just individuals.

## v0.6 — Admin & compliance

- [ ] **Admin dashboard / Action buttons** — OWUI Action buttons to browse, export (GDPR), and delete conclusions (`delete_conclusion`) per user.
- [ ] **Webhooks for queue status** — push Honcho queue events into a channel so reasoning backlog is visible to operators.
- [ ] **Metrics** — counters/latency stats for injections, saves, cache hits, and errors exported to logs/endpoint.

## v1.0 — Polish & stability

- [ ] **Scopes** — per-workspace/scope isolation for multi-tenant Open WebUI installs (API full parity with Honcho scopes).
- [ ] **`'what Honcho knows' Rich UI panel** — embed a Rich-UI element per user showing representation + card + top conclusions, kept in sync as Honcho learns.
- [ ] **API/behavior freeze, docs, and 1.0 release** — full test coverage, live test suite green, documented valve surface; then SemVer discipline.

Versions beyond v1.0 (e.g. Honcho-sdk version pinning, mobile-client support) will be
planned as Honcho's API stabilizes. Feedback → [GitHub issues](https://github.com/fubarpie/owui-honcho-memory/issues).