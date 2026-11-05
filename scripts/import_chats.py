#!/usr/bin/env python3
"""Import an Open WebUI chat export into Honcho (stdlib only).

Export chats in Open WebUI: Settings -> Data Controls -> Export Chats (JSON).

    export HONCHO_TOKEN=...            # never pass the token on the command line
    python scripts/import_chats.py chats-export.json \
        --url http://192.168.1.197:8000 --user-id <your OWUI user id> --dry-run
    # then run again without --dry-run

Peer/session naming matches the filter defaults (workspace "open-webui",
peer "user-<id>", peer "assistant-<model>", session = chat id), so imported
chats and live chats share one memory. Re-running is safe: chats already
imported are recorded in a state file and skipped (use --force to override).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

MAX_CONTENT = 25000
BATCH = 100
_MEM_RE = re.compile(r"<honcho_memory>.*?(?:</honcho_memory>|$)", re.DOTALL | re.IGNORECASE)
_REASON_RE = re.compile(r"<details\b[^>]*\btype\s*=\s*[\"']?reasoning[\"']?[^>]*>.*?</details>", re.DOTALL | re.IGNORECASE)


def sanitize_id(s, max_len: int = 100) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(s) if s is not None else "")[:max_len]
    return cleaned or "unknown"


def clean(text: str) -> str:
    return _REASON_RE.sub("", _MEM_RE.sub("", text or "")).strip()


def content_of(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, list):  # multimodal: keep text parts only
        c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return clean(c if isinstance(c, str) else "")


def iso(ts) -> str | None:
    try:
        ts = float(ts)
        if ts > 1e12:  # milliseconds
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def ordered_messages(chat: dict) -> list[dict]:
    """Follow the active branch (currentId -> parents) so edits/regens aren't duplicated."""
    inner = chat.get("chat", chat)
    hist = inner.get("history") or {}
    msgs = hist.get("messages")
    if isinstance(msgs, dict) and msgs:
        cur, out, seen = hist.get("currentId"), [], set()
        if cur not in msgs:  # fall back to the latest message
            cur = max(msgs, key=lambda k: msgs[k].get("timestamp") or 0)
        while cur and cur in msgs and cur not in seen:
            seen.add(cur)
            out.append(msgs[cur])
            cur = msgs[cur].get("parentId")
        return out[::-1]
    flat = inner.get("messages")
    return flat if isinstance(flat, list) else []


class Honcho:
    def __init__(self, url: str, token: str, workspace: str):
        self.url, self.token, self.ws = url.rstrip("/"), token, workspace

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} on {method} {path}: {e.read().decode()[:200]}") from None

    def w(self, suffix: str = "") -> str:
        return f"/v3/workspaces/{quote(self.ws, safe='')}{suffix}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path, help="Open WebUI chats export (JSON)")
    ap.add_argument("--url", default=os.environ.get("HONCHO_URL", "http://localhost:8000"))
    ap.add_argument("--workspace", default="open-webui")
    ap.add_argument("--user-id", help="OWUI user id (defaults to user_id found in the export)")
    ap.add_argument("--peer-prefix", default="user-")
    ap.add_argument("--no-per-model", action="store_true", help="use a single 'assistant' peer")
    ap.add_argument("--limit", type=int, help="only import the N most recently updated chats")
    ap.add_argument("--min-chars", type=int, default=0, help="skip chats with fewer total user characters")
    ap.add_argument("--state", type=Path, default=Path(".import_state.json"))
    ap.add_argument("--force", action="store_true", help="re-import chats already in the state file")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    token = os.environ.get("HONCHO_TOKEN", "")
    if not token and not a.dry_run:
        print("Set HONCHO_TOKEN in the environment.", file=sys.stderr)
        return 2

    data = json.loads(a.export.read_text())
    chats = data if isinstance(data, list) else data.get("chats", [data])
    chats = [c for c in chats if isinstance(c, dict)]
    chats.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
    if a.limit:
        chats = chats[: a.limit]

    done = set(json.loads(a.state.read_text())) if a.state.exists() and not a.force else set()
    hc = Honcho(a.url, token, a.workspace)
    if not a.dry_run:
        hc.call("POST", "/v3/workspaces", {"id": a.workspace})

    tot_chats = tot_msgs = skipped = failed = 0
    peers_seen: set[str] = set()
    for chat in chats:
        cid = chat.get("id")
        uid = a.user_id or chat.get("user_id")
        if not cid or not uid:
            skipped += 1
            continue
        if cid in done:
            skipped += 1
            continue
        inner = chat.get("chat", chat)
        models = inner.get("models") or []
        user_peer = sanitize_id(f"{a.peer_prefix}{uid}")
        batch, asst_peers = [], set()
        for m in ordered_messages(chat):
            role, text = m.get("role"), content_of(m)
            if role not in ("user", "assistant") or not text:
                continue
            if role == "user":
                peer = user_peer
            else:
                model = m.get("model") or (models[0] if models else None)
                peer = "assistant" if (a.no_per_model or not model) else sanitize_id(f"assistant-{model}")
                asst_peers.add(peer)
            ts = iso(m.get("timestamp")) or iso(chat.get("updated_at") or chat.get("created_at"))
            for i in range(0, len(text), MAX_CONTENT):
                item = {"peer_id": peer, "content": text[i : i + MAX_CONTENT], "metadata": {"imported": True}}
                if ts:
                    item["created_at"] = ts
                batch.append(item)
        user_chars = sum(len(b["content"]) for b in batch if b["peer_id"] == user_peer)
        if not batch or user_chars < a.min_chars:
            skipped += 1
            continue
        print(f"{'[dry] ' if a.dry_run else ''}{cid[:8]}  {len(batch):4d} msgs  {chat.get('title') or ''!s:.50}")
        tot_chats += 1
        tot_msgs += len(batch)
        if a.dry_run:
            continue
        try:
            for p in {user_peer, *asst_peers} - peers_seen:
                hc.call("POST", hc.w("/peers"), {"id": p})
                peers_seen.add(p)
            peers = {user_peer: {"observe_me": True, "observe_others": False}}
            peers.update({p: {"observe_me": False, "observe_others": False} for p in asst_peers})
            hc.call("POST", hc.w("/sessions"), {"id": sanitize_id(cid), "peers": peers,
                                               "metadata": {"imported": True, "title": chat.get("title")}})
            for i in range(0, len(batch), BATCH):
                hc.call("POST", hc.w(f"/sessions/{quote(sanitize_id(cid), safe='')}/messages"),
                        {"messages": batch[i : i + BATCH]})
            done.add(cid)
            a.state.write_text(json.dumps(sorted(done)))
            time.sleep(0.2)
        except RuntimeError as e:
            failed += 1
            print(f"  FAILED: {e}", file=sys.stderr)

    print(f"\n{'Would import' if a.dry_run else 'Imported'} {tot_chats} chats / {tot_msgs} messages "
          f"(skipped {skipped}, failed {failed}).")
    if not a.dry_run and tot_msgs:
        print("The deriver now processes these in the background; peer card/representation appear as it catches up.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
