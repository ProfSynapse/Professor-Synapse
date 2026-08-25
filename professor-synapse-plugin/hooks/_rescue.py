#!/usr/bin/env python3
"""Recover memory stores this plugin wrote to the wrong directory.

Before the data-root resolution fix, the two halves of this plugin could
disagree about where state lives. Hooks get $CLAUDE_PLUGIN_DATA injected and
used the real data dir; model-run scripts (summon.py, memory.py) derived the
path from their own location and could land somewhere else entirely — the
in-place skill root, or a stale data dir from an earlier install flavor. The
result is a second memory store, diverging quietly from the live one.

That orphan is on a clock. When it sits in the skill root, a plugin update
replaces that directory wholesale (see _pluginpaths' header) and the store goes
with it — so the fix that stops the fork can also delete the evidence of it.

Hence this: on the first session after the fix, find the orphan, copy it beside
the live store as rescued-store-<source>/, and say so. COPY, never merge —
reconciling two diverged stores means judging which of a pair of contradictory
records is current, and a SessionStart hook has no business deciding that
silently. The memory.py CLI is where that belongs.

Scope note: for a data-dir orphan, agents/ holds only the user's own agents and
is rescued too. For an in-place orphan, agents/ is the SHIPPED roster with any
user agents mixed in, indistinguishable without guessing, so only memory/ is
taken.

Fails soft throughout — a rescue that cannot happen must never break a session.
Stdlib only.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

PLUGIN_NAME = "professor-synapse"
RESCUE_PREFIX = "rescued-store-"


def _longterm_records(db: Path) -> int:
    """Row count in a long-term store, read-only so we never create or lock it."""
    if not db.is_file():
        return 0
    con = None
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
        return int(con.execute("SELECT COUNT(*) FROM record").fetchone()[0])
    except Exception:
        return 0
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _working(js: Path):
    """(active items, has ever been written) for a memory.json."""
    try:
        data = json.loads(js.read_text(encoding="utf-8"))
    except Exception:
        return 0, False
    if not isinstance(data, dict):
        return 0, False
    active = data.get("active")
    meta = data.get("meta")
    touched = bool(isinstance(meta, dict) and meta.get("updated_at"))
    return (len(active) if isinstance(active, list) else 0), touched


def store_summary(root: Path):
    """(items, records) for the store under `root`, or None if there isn't one.

    The shipped seed — updated_at null, no active items, an empty database —
    reads as "no store", which is how a pristine install stays quiet.
    """
    mem = root / "memory"
    if not mem.is_dir():
        return None
    items, touched = _working(mem / "memory.json")
    records = _longterm_records(mem / "longterm.db")
    if not items and not records and not touched:
        return None
    return items, records


def _sibling_data_dirs(live: Path):
    """Other <plugins>/data/<plugin>-* dirs beside the live one."""
    parent = live.parent
    if parent.name != "data" or not parent.is_dir():
        return []
    return sorted(d for d in parent.glob(f"{PLUGIN_NAME}-*") if d.is_dir())


def find_orphans(live: Path, skill_root: Path):
    """Directories holding a store that is not the live one."""
    orphans = []
    try:
        seen = {live.resolve()}
    except Exception:
        seen = {live}
    for cand in (skill_root, *_sibling_data_dirs(live)):
        try:
            resolved = cand.resolve()
        except Exception:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if store_summary(cand) is not None:
            orphans.append(cand)
    return orphans


def _key_for(orphan: Path, skill_root: Path) -> str:
    """A stable, readable name for the rescue dir — stable so a rescue that
    already happened is recognised instead of repeated every session."""
    try:
        if orphan.resolve() == skill_root.resolve():
            return "in-place"
    except Exception:
        pass
    return re.sub(r"[^A-Za-z0-9._-]", "-", orphan.name) or "unknown"


def rescue(live: Path, skill_root: Path):
    """Copy every orphan store beside the live one. Returns lines for the user.

    Idempotent: an orphan whose rescue dir already exists is not copied again,
    but is still reported — an unmerged store is an open question, and one line
    a session is a fair price for not letting it be forgotten.
    """
    lines = []
    for orphan in find_orphans(live, skill_root):
        key = _key_for(orphan, skill_root)
        dest = live / (RESCUE_PREFIX + key)
        summary = store_summary(orphan)
        if summary is None:
            continue
        items, records = summary
        held = f"{items} working item(s), {records} long-term record(s)"
        if dest.exists():
            lines.append(f"  - {dest} — {held}, not yet merged.")
            continue
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(orphan / "memory", dest / "memory")
            # Only a data dir's agents/ is purely the user's; a skill root's is
            # the shipped roster, so copying it would duplicate built-ins.
            if key != "in-place" and (orphan / "agents").is_dir():
                shutil.copytree(orphan / "agents", dest / "agents")
        except Exception:
            continue
        lines.append(f"  - {dest} — {held}, copied from {orphan}.")
    return lines


def notice(lines) -> str:
    """The SessionStart paragraph, or "" when there is nothing to say."""
    if not lines:
        return ""
    return (
        "⚠️ A second Professor Synapse memory store was found outside the live "
        "one. Earlier versions could resolve the data dir differently for hooks "
        "and for model-run scripts, so memory written in some sessions landed "
        "here instead. It has been COPIED (never moved, never merged) to:\n"
        + "\n".join(lines)
        + "\nNothing is lost, and the live store is untouched. To fold it in, "
        "summon the memory-agent and have it read the rescued store and re-save "
        "what is still true through `scripts/memory.py` — two diverged stores "
        "can hold contradictory records, so this needs a judgement call, not a "
        "bulk import. Delete the rescued directory when you are done and this "
        "notice stops."
    )
