#!/usr/bin/env python3
"""Resolve the writable DATA root for Professor Synapse across runtimes.

The core skill files (this script, agents/, references/, SKILL.md) live in a
read-only install directory — the "skill root". When installed as a Claude Code
plugin that directory is REPLACED wholesale on every update, so anything the user
creates (their own agents, the memory/ store, the summon-gate marker) must live in
a separate WRITABLE directory that survives updates: the plugin's data dir.

Critical fact this module exists to handle: the $CLAUDE_PLUGIN_DATA / $CLAUDE_PLUGIN_ROOT
environment variables are injected ONLY into plugin hook/command execution — NOT into
Bash commands the model runs. So when the model runs summon.py / memory.py, those vars
are empty and the data dir must be DERIVED from this file's own install path instead.

Resolution order:
  1. $CLAUDE_PLUGIN_DATA — present in hook/command context (and honored if ever set).
  2. Derived from the plugin cache layout this file sits in:
       ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/...
         ->  ~/.claude/plugins/data/<plugin>-<marketplace>/
     (matches how Claude Code names the data dir; no env var needed.)
  3. Glob <plugins>/data/<plugin>-*  — marketplace-name agnostic; used if (2)'s exact
     layout assumption ever shifts. On multiple matches (e.g. a stale dir from an
     earlier install flavor) the most recently modified dir wins — see _glob_data.
  4. In-place fallback: the skill root itself — so the very same files keep working as
     a plain portable skill (or in a dev checkout) with no plugin involved.

Stdlib only.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# The plugin's name (the <plugin> segment of the cache/data paths). Keep in sync
# with .claude-plugin/plugin.json "name".
PLUGIN_NAME = "professor-synapse"


def _sanitize(segment: str) -> str:
    """Claude Code replaces every char outside [A-Za-z0-9_-] with '-' when it
    names the data dir <plugin>-<marketplace>. Mirror that exactly."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", segment)


def _plugins_root_from(parts) -> Path | None:
    """Return the '.../plugins' dir if this path runs out of a plugin cache."""
    try:
        i = len(parts) - 1 - parts[::-1].index("cache")
    except ValueError:
        return None
    return Path(*parts[:i]) if i > 0 else None


def _derive_from_cache(here: Path):
    """cache/<mp>/<plugin>/<ver>/...  ->  data/<plugin>-<mp>  (or None)."""
    parts = here.parts
    try:
        i = len(parts) - 1 - parts[::-1].index("cache")
    except ValueError:
        return None
    if i + 2 >= len(parts):           # need <mp> and <plugin> after 'cache'
        return None
    marketplace, plugin = parts[i + 1], parts[i + 2]
    plugins_root = Path(*parts[:i])
    return plugins_root / "data" / _sanitize(f"{plugin}-{marketplace}")


def _glob_data(here: Path):
    """Find the <plugins>/data/<plugin>-* dir; on ambiguity prefer the live one.

    Ambiguity happens in the wild: a stale leftover data dir from an earlier
    install flavor (e.g. <plugin>-inline) can sit beside the live
    <plugin>-<marketplace> dir. Returning None in that case silently sends
    model-side writes (the summon marker, the memory store) to the in-place
    fallback while hooks — which get $CLAUDE_PLUGIN_DATA injected — keep
    reading the real data dir: the summon-gate never opens and the memory
    store forks. Prefer the most recently modified match instead — the live
    dir keeps receiving hook writes, so recency identifies it."""
    plugins_root = _plugins_root_from(here.parts) or (Path.home() / ".claude" / "plugins")
    data_root = plugins_root / "data"
    if not data_root.is_dir():
        return None
    matches = sorted(p for p in data_root.glob(f"{PLUGIN_NAME}-*") if p.is_dir())
    if not matches:
        return None
    if len(matches) > 1:
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def resolve_data_root(skill_root) -> str:
    """Return the writable data root as a string.

    `skill_root` is the in-place fallback (the installed skill dir). The returned
    path is NOT guaranteed to exist yet — callers create subdirs (memory/, agents/,
    .summon-state/) on demand; the SessionStart bootstrap hook also pre-creates them.
    """
    env = os.environ.get("CLAUDE_PLUGIN_DATA")
    if env:
        return str(env)
    here = Path(__file__).resolve()
    derived = _derive_from_cache(here)
    if derived is not None:
        return str(derived)
    globbed = _glob_data(here)
    if globbed is not None:
        return str(globbed)
    return str(skill_root)


if __name__ == "__main__":
    # Tiny CLI so hooks/tests can ask "where is the data root?" without importing.
    import sys
    fallback = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent)
    print(resolve_data_root(fallback))
