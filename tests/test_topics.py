"""Tests for topic grouping of curated observations."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "honcho_helpers_topics", Path(__file__).resolve().parent.parent / "parts" / "02_helpers.py"
)
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)

MEMS = [
    ("Riffle accounting project: self-hosted Wave-like accounting app, deployed via Docker Compose", "projects/riffle-accounting"),
    ("Homelab: Proxmox server hosts LXCs", "homelab"),
    ("no path memory about cats", None),
]


def test_keyword_rules():
    assert h.classify_topic("Has a notification webhook on a self-hosted ntfy server") == "homelab"
    assert h.classify_topic("Uses Fedora on day-to-day machines") == "devices"
    assert h.classify_topic("Wants a filter for OpenWebUI model handoff") == "projects/open-webui"
    assert h.classify_topic("Enjoys hiking") == "general"


def test_owui_paths_take_precedence():
    t = h.classify_topic("Wants the Riffle accounting app to support invoices", MEMS)
    assert t == "projects/riffle-accounting"


def test_pathless_memories_ignored():
    assert h.classify_topic("cats cats cats", MEMS) == "general"


def test_group_preserves_order_and_membership():
    facts = ["Enjoys hiking", "Runs Proxmox LXC containers", "Uses Docker on the homelab"]
    g = h.group_by_topic(facts)
    assert g == {"general": ["Enjoys hiking"], "homelab": facts[1:]}
    assert list(g) == ["general", "homelab"]


def test_format_orders_query_relevant_group_first():
    g = {"general": ["A"], "homelab": ["B"], "projects/open-webui": ["C"]}
    out = h.format_grouped_facts(g, "my homelab is down")
    assert out.splitlines()[0] == "homelab:"
    assert "- B" in out and "- C" in out and "- A" in out


def test_lone_general_is_flat_and_empty_ok():
    assert h.format_grouped_facts({"general": ["x", "y"]}) == "- x\n- y"
    assert h.format_grouped_facts({}) == ""
