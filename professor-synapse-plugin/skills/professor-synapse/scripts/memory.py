#!/usr/bin/env python3
"""
memory.py - Professor Synapse's shared, agent-tagged memory.

Two stores under ./memory/, one tool:
  - memory/memory.json   working memory (profile + active items). Human-readable. Hot.
  - memory/longterm.db   SQLite long-term store (records + change log). Queryable. Cold.

Every item, record, and log entry is tagged with the agent that created it, so
memory can be filtered by agent. Standard library only; no pip installs.

Top-level options:
  --agent <slug>   the active agent. Writes stamp it; reads filter by it; omit
                   on a read to see across all agents.

Verbs (run `python3 memory.py <verb> --help` for options):
  read       Show working memory (optionally one agent's).
  profile    Set shared profile fields (person-level, not agent-scoped).
  add        Add an active item, tagged with --agent.
  update     Change fields on an active item.
  resolve    Mark an active item done.
  scan       Flag stale / overdue / done / duplicate active items.
  compact    Move approved items to long-term, cap the log.
  resurface  Move an archived item back to active (same id).
  record     Write straight to long-term: a decision, note, or fact.
  recall     Find long-term items worth surfacing (by date, --query, --agent).
             --query is ranked full-text search (FTS5, with a LIKE fallback).
  brief      One-shot prefetch: profile + active items + due long-term items,
             plus --query matches. The start-of-session recall in one call.
  dream      Surface dense graph clusters to consolidate + persona material (read-only).
  consolidate Fold a cluster of atoms into a higher-altitude insight.
  agents     List every agent that has touched memory, with counts.
  validate   Check memory.json; --fix applies safe mechanical repairs.
  doctor     Check long-term db integrity.
  render     Print working memory as markdown.
  export     Dump long-term db to JSON (optionally one agent's).

Persistence: the store lives in the writable data dir (resolved automatically;
override with --root). Writes are durable on the spot.
"""

import argparse
import difflib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows safety: force UTF-8 on stdout/stderr so emoji in memory output don't
# crash with a cp1252 UnicodeEncodeError. No-op where stdio is already UTF-8
# (WSL/Linux/macOS).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

SKILL_ROOT = Path(__file__).resolve().parent.parent
MEM_DIR = SKILL_ROOT / "memory"

# The store (memory/) is USER DATA and must live in a writable dir that survives
# plugin updates, not in the read-only skill root. resolve_data_root() finds it
# (plugin data dir, or the skill root itself when running in-place). When --root is
# passed explicitly (e.g. by summon.py) that wins; otherwise main() falls back here.
try:
    from _pluginpaths import resolve_data_root
except Exception:  # module missing (e.g. partial copy) -> behave in-place
    def resolve_data_root(skill_root):
        return str(skill_root)

ITEM_STATUS = {"open", "done"}
RECORD_KINDS = {"item", "decision", "note", "fact", "lesson", "insight"}
RECORD_STATUS = {"open", "done", "deferred", "dropped"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
# Optional fusion nudge by confidence (applied multiplicatively when present).
CONFIDENCE_WEIGHT = {"high": 1.15, "medium": 1.0, "low": 0.85}
STALE_DAYS = 21
LONGTERM_STALE_DAYS = 60     # a long-term record unused this long is a chopping-block candidate
GRAPH_SPARE_AFFINITY = 1.0   # ...unless it's this wired-in (part of a live cluster) — then spared
LOG_CAP_DAYS = 90
SCHEMA_VERSION = 1
DEFAULT_AGENT = "unknown"

# Ranked-fusion tuning for keyword recall (recall --query / brief --query).
RRF_K = 60                       # Reciprocal Rank Fusion damping; larger = flatter
W_TEXT = 1.0                     # weight of the BM25 relevance ranking
W_RECENCY = 0.5                  # weight of the recency ranking (secondary signal)
# BM25 per-column weights, in FTS index column order
# (id, text, people, tags, owner, goal, outcome, constraints, source, verify, unknowns).
# people/tags/constraints outrank free text: an exact tag/person/gotcha hit is a
# stronger signal. goal/outcome are weighted above plain text but below the lists.
# source/verify/unknowns (the confidence basis) are searchable but weighted modestly;
# verify a touch higher so "how do I confirm X" finds the upgrade path.
BM25_WEIGHTS = (0.0, 1.0, 3.0, 3.0, 1.0, 2.0, 1.5, 3.0, 1.0, 1.5, 1.0)
# insight = a synthesised roll-up of a cluster (memory's "dream"); it is the highest-
# signal recall hit, edging out the atoms it consolidated.
KIND_WEIGHT = {"insight": 1.35, "fact": 1.3, "lesson": 1.25, "decision": 1.2,
               "note": 1.0, "item": 1.0}

# No-query recall fallback: when a summon/recall arrives with no query terms (or the
# query matches nothing), surface this many of the agent's most recent records,
# reranked by recency with the same kind/confidence nudges as keyword recall.
RECENT_DEFAULT = 8

# Knowledge-graph (co-recall affinity) tuning. Edges are undirected, weighted links
# between records; weight grows with co-use and decays with time ("fire together,
# wire together" + forgetting), then feeds recall via spreading activation.
EDGE_HALFLIFE_DAYS = 30.0   # a decayed edge weight halves every N days since its last bump
COUSE_INCREMENT = 1.0       # affinity added per co-use / reinforce event
MANUAL_LINK_FLOOR = 3.0     # an explicit `link` is worth at least this much
W_GRAPH = 0.6               # fusion weight of the graph-affinity ranking (cf. W_TEXT/W_RECENCY)
GRAPH_SEEDS = 5             # how many top text hits seed spreading activation
GRAPH_FANOUT = 20           # cap on neighbours pulled in per query
GRAPH_MIN_AFFINITY = 0.1    # ignore trivially weak pulled-in neighbours

# Dream (consolidation) tuning. `dream` surfaces dense graph clusters worth rolling up
# into a higher-altitude `insight`; `consolidate` folds a cluster's atoms under one,
# marking them consolidated (kept as evidence, one hop behind the summary in recall).
DREAM_EDGE_FLOOR = 0.5      # min decayed edge weight for two records to count as intra-cluster
DREAM_MIN_CLUSTER = 3       # smallest cluster worth proposing for consolidation
DREAM_MAX_CLUSTERS = 10     # cap on clusters surfaced per dream
DREAM_PERSONA_FACTS = 12    # high-confidence facts surfaced for persona (profile) re-synthesis

# Write-time similarity check ("am I about to duplicate/contradict something?").
# Surfaces existing records resembling a proposed write so the agent can decide
# whether to just save, or pause and confirm. Advisory only — never blocks a write.
SIMILAR_POOL = 12           # FTS candidates considered before scoring
SIMILAR_RETURN = 5          # max similar records surfaced
DUP_RATIO = 0.82            # difflib text-overlap ratio at/above which it's a likely duplicate
SIMILAR_FLOOR = 0.45        # below this ratio AND no shared tag/person, too unrelated to surface
CONFLICT_KINDS = ("fact", "decision")  # kinds whose high-confidence records warrant a contradiction check


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_today():
    return datetime.now(timezone.utc).date().isoformat()


def working_path(root):
    return Path(root) / "memory" / "memory.json"


def db_path(root):
    return Path(root) / "memory" / "longterm.db"


def new_id():
    return f"m-{uuid.uuid4().hex[:8]}"


def _is_iso(v):
    try:
        datetime.fromisoformat(str(v))
        return True
    except ValueError:
        return False


def _ensure_dir(root):
    (Path(root) / "memory").mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Working memory
# --------------------------------------------------------------------------

def empty_working():
    return {
        "meta": {"schema_version": SCHEMA_VERSION, "updated_at": None},
        "profile": {
            "name": None, "title": None, "notes": None,
            "focus_areas": [], "key_people": [],
        },
        "active": [],
    }


MIGRATIONS = {}  # {from_version: fn(data)->None}. See references/memory-data-model.md.


def migrate_working(data):
    meta = data.setdefault("meta", {})
    v = meta.get("schema_version", SCHEMA_VERSION)
    if v > SCHEMA_VERSION:
        sys.exit(f"memory.json is schema_version {v}, newer than this code "
                 f"({SCHEMA_VERSION}); the data dir was written by a newer version.")
    changed = False
    while v < SCHEMA_VERSION:
        fn = MIGRATIONS.get(v)
        if fn is None:
            sys.exit(f"No migration registered from schema_version {v} to {v + 1}.")
        fn(data)
        v += 1
        meta["schema_version"] = v
        changed = True
    return changed


def load_working(root):
    p = working_path(root)
    if not p.exists():
        data = empty_working()
        save_working(root, data)
        return data
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"memory.json is not valid JSON: {e}. "
                 "Restore memory.json.bak or fix the file, then retry.")
    if migrate_working(data):
        save_working(root, data)
    fatal = [m for s, m in validate_working(data) if s == "fatal"]
    if fatal:
        sys.exit("memory.json has a structural problem: " + "; ".join(fatal)
                 + ". Run `validate` for details; restore memory.json.bak to recover.")
    return data


def save_working(root, data):
    fatal = [m for s, m in validate_working(data) if s == "fatal"]
    if fatal:
        raise ValueError("Refusing to write invalid memory.json: " + "; ".join(fatal))
    _ensure_dir(root)
    data.setdefault("meta", {})["updated_at"] = utc_now()
    p = working_path(root)
    if p.exists():
        # copyfile (content only) — not copy2/copy, which call os.utime/os.chmod
        # and raise PermissionError on Google Drive / network mounts (e.g. WSL).
        shutil.copyfile(p, str(p) + ".bak")
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def validate_working(data):
    out = []
    if not isinstance(data, dict):
        return [("fatal", "top level is not an object")]
    for key in ("meta", "profile", "active"):
        if key not in data:
            out.append(("fixable", f"missing top-level key '{key}'"))
    active = data.get("active", [])
    if not isinstance(active, list):
        return out + [("fatal", "'active' is not a list")]
    seen = set()
    for i, item in enumerate(active):
        if not isinstance(item, dict):
            out.append(("fatal", f"active[{i}] is not an object"))
            continue
        iid = item.get("id")
        if not iid:
            out.append(("fixable", f"active[{i}] missing id"))
        elif iid in seen:
            out.append(("fixable", f"duplicate id '{iid}'"))
        else:
            seen.add(iid)
        if not item.get("agent"):
            out.append(("fixable", f"active[{i}] missing agent"))
        if item.get("status") not in ITEM_STATUS:
            out.append(("fixable", f"active[{i}] invalid status '{item.get('status')}'"))
        for key in ("people", "tags", "constraints"):
            if key in item and not isinstance(item[key], list):
                out.append(("fixable", f"active[{i}] field '{key}' is not a list"))
        conf = item.get("confidence")
        if conf is not None and conf not in CONFIDENCE_LEVELS:
            out.append(("fixable", f"active[{i}] invalid confidence '{conf}'"))
        for d in ("due", "created_at", "updated_at"):
            v = item.get(d)
            if v and not _is_iso(v):
                out.append(("fixable", f"active[{i}] '{d}' not ISO: {v}"))
    return out


def fix_working(data):
    changes = []
    base = empty_working()
    for key in ("meta", "profile"):
        if not isinstance(data.get(key), dict):
            data[key] = base[key]
            changes.append(f"added missing {key}")
    if not isinstance(data.get("active"), list):
        data["active"] = []
        changes.append("reset active list")
    seen, cleaned = set(), []
    for item in data["active"]:
        if not isinstance(item, dict):
            changes.append("dropped a non-object active entry")
            continue
        if not item.get("id"):
            item["id"] = new_id()
            changes.append(f"assigned id {item['id']}")
        if item["id"] in seen:
            changes.append(f"dropped duplicate id {item['id']}")
            continue
        seen.add(item["id"])
        if not item.get("agent"):
            item["agent"] = DEFAULT_AGENT
            changes.append(f"set agent on {item['id']}")
        for key in ("people", "tags"):
            if not isinstance(item.get(key), list):
                item[key] = []
                changes.append(f"reset {key} on {item['id']}")
        if "constraints" in item and not isinstance(item["constraints"], list):
            item["constraints"] = []
            changes.append(f"reset constraints on {item['id']}")
        if item.get("confidence") is not None and item["confidence"] not in CONFIDENCE_LEVELS:
            item["confidence"] = None
            changes.append(f"cleared invalid confidence on {item['id']}")
        if not item.get("created_at"):
            item["created_at"] = utc_today()
            changes.append(f"backfilled created_at on {item['id']}")
        if not item.get("updated_at"):
            item["updated_at"] = item["created_at"]
            changes.append(f"backfilled updated_at on {item['id']}")
        if item.get("status") not in ITEM_STATUS:
            item["status"] = "open"
            changes.append(f"reset status on {item['id']}")
        cleaned.append(item)
    data["active"] = cleaned
    return changes


# --------------------------------------------------------------------------
# Long-term store
# --------------------------------------------------------------------------

# `kind` is validated in Python (RECORD_KINDS), not by a DB CHECK, so new kinds can
# be added without rebuilding the table. The body fields goal/outcome/constraints are
# optional and conventionally filled by `lesson` records (constraints is a JSON array).
RECORD_DDL = """
CREATE TABLE IF NOT EXISTS record (
    id          TEXT PRIMARY KEY,
    agent       TEXT,
    kind        TEXT NOT NULL,
    type        TEXT,
    text        TEXT NOT NULL,
    people      TEXT,
    tags        TEXT,
    owner       TEXT,
    due         TEXT,
    status      TEXT CHECK(status IN ('open','done','deferred','dropped')),
    source      TEXT,
    created_at  TEXT,
    recorded_at TEXT,
    reason      TEXT,
    rationale   TEXT,
    goal        TEXT,
    outcome     TEXT,
    constraints TEXT,
    confidence  TEXT,
    verify      TEXT,
    unknowns    TEXT,
    last_used   TEXT,
    consolidated_into TEXT
);
"""
EDGE_DDL = """
CREATE TABLE IF NOT EXISTS edge (
    a          TEXT NOT NULL,
    b          TEXT NOT NULL,
    weight     REAL NOT NULL DEFAULT 0,
    count      INTEGER NOT NULL DEFAULT 0,
    source     TEXT,
    created_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (a, b)
);
"""
CHANGELOG_DDL = """
CREATE TABLE IF NOT EXISTS changelog (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT,
    agent   TEXT,
    action  TEXT,
    item_id TEXT,
    summary TEXT
);
"""
# Columns in RECORD_DDL order — single source of truth for full-row copies.
RECORD_COLUMNS = ("id", "agent", "kind", "type", "text", "people", "tags", "owner",
                  "due", "status", "source", "created_at", "recorded_at", "reason",
                  "rationale", "goal", "outcome", "constraints", "confidence",
                  "verify", "unknowns", "last_used", "consolidated_into")


def _migrate_record_schema(con):
    """Bring an existing `record` table up to the current schema. Additive columns
    are added in place; the old restrictive `kind` CHECK (which forbids 'lesson') is
    removed by rebuilding the table, preserving every row. No-op on a fresh db."""
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='record'").fetchone()
    if not row:
        return
    cols = {r[1] for r in con.execute("PRAGMA table_info(record)")}
    for c in ("goal", "outcome", "constraints", "confidence", "verify", "unknowns",
              "last_used", "consolidated_into"):
        if c not in cols:
            con.execute(f"ALTER TABLE record ADD COLUMN {c} TEXT")
    if "kind IN ('item','decision','note','fact')" in (row[0] or ""):
        collist = ",".join(RECORD_COLUMNS)
        con.executescript(
            "ALTER TABLE record RENAME TO _record_old;"
            + RECORD_DDL
            + f"INSERT INTO record ({collist}) SELECT {collist} FROM _record_old;"
            + "DROP TABLE _record_old;"
        )


def connect_db(root):
    _ensure_dir(root)
    con = sqlite3.connect(str(db_path(root)))
    con.executescript(RECORD_DDL + CHANGELOG_DDL + EDGE_DDL)
    _migrate_record_schema(con)
    con.commit()
    return con


# --------------------------------------------------------------------------
# Knowledge graph: undirected weighted edges with time decay
# --------------------------------------------------------------------------

def _edge_key(i, j):
    """Canonical (a, b) with a <= b so an undirected edge has one row."""
    return (i, j) if i <= j else (j, i)


def _decay(weight, updated_at, now=None):
    """Edge weight as of `now`, halving every EDGE_HALFLIFE_DAYS since last bump."""
    if not weight or not updated_at or not _is_iso(updated_at):
        return float(weight or 0.0)
    now = now or datetime.now(timezone.utc)
    elapsed = (now - datetime.fromisoformat(updated_at)).total_seconds() / 86400.0
    if elapsed <= 0:
        return float(weight)
    return float(weight) * (0.5 ** (elapsed / EDGE_HALFLIFE_DAYS))


def _record_exists(con, rid):
    return con.execute("SELECT 1 FROM record WHERE id=?", (rid,)).fetchone() is not None


def _touch_records(con, ids, now=None):
    """Reset the staleness clock on records that were just reactivated (recalled/used)."""
    now = now or utc_now()
    ids = [x for x in dict.fromkeys(ids)]
    if not ids:
        return
    qs = ",".join("?" for _ in ids)
    con.execute(f"UPDATE record SET last_used=? WHERE id IN ({qs})", (now, *ids))


def _bump_edge(con, i, j, inc, source, now=None):
    """Add `inc` affinity to the i–j edge (after decaying what's there), bump count,
    and reactivate both endpoints. Creates the edge if absent."""
    if i == j:
        return
    a, b = _edge_key(i, j)
    now = now or utc_now()
    row = con.execute("SELECT weight,count,source,updated_at FROM edge WHERE a=? AND b=?",
                      (a, b)).fetchone()
    if row:
        w = _decay(row[0], row[3]) + inc
        src = row[2] if row[2] == source else "mixed"
        con.execute("UPDATE edge SET weight=?,count=?,source=?,updated_at=? WHERE a=? AND b=?",
                    (w, row[1] + 1, src, now, a, b))
    else:
        con.execute("INSERT INTO edge(a,b,weight,count,source,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)", (a, b, float(inc), 1, source, now, now))
    _touch_records(con, [i, j], now)


def _reinforce(con, ids, inc, source, now=None):
    """Bump every unordered pair among `ids` (a co-use event). Returns pair count."""
    ids = [x for x in dict.fromkeys(ids)]
    now = now or utc_now()
    pairs = 0
    for x in range(len(ids)):
        for y in range(x + 1, len(ids)):
            _bump_edge(con, ids[x], ids[y], inc, source, now)
            pairs += 1
    return pairs


def _connectivity(con, now=None):
    """Map record id -> summed decayed edge weight (how wired-in it is)."""
    now = now or datetime.now(timezone.utc)
    agg = {}
    for a, b, w, upd in con.execute("SELECT a,b,weight,updated_at FROM edge"):
        dw = _decay(w, upd, now)
        if dw <= 0:
            continue
        agg[a] = agg.get(a, 0.0) + dw
        agg[b] = agg.get(b, 0.0) + dw
    return agg


def _consolidated_ids(con, ids):
    """Of the given ids, those folded under an insight (`consolidated_into` set). They
    are kept as evidence but held out of *primary* recall — they resurface only one hop
    behind their summary via spreading activation."""
    ids = [x for x in dict.fromkeys(ids)]
    if not ids:
        return set()
    qs = ",".join("?" for _ in ids)
    rows = con.execute(
        f"SELECT id FROM record WHERE id IN ({qs}) "
        "AND consolidated_into IS NOT NULL AND consolidated_into != ''", ids).fetchall()
    return {r[0] for r in rows}


def log_event(con, agent, action, item_id, summary):
    con.execute("INSERT INTO changelog(ts,agent,action,item_id,summary) VALUES(?,?,?,?,?)",
                (utc_now(), agent, action, item_id, summary))


def cap_log(con):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOG_CAP_DAYS)).isoformat(timespec="seconds")
    return con.execute("DELETE FROM changelog WHERE ts < ?", (cutoff,)).rowcount


def _json_list(s):
    try:
        v = json.loads(s) if s else []
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _check_confidence(val):
    """Normalize an optional confidence to high/medium/low, or exit with guidance."""
    if val is None:
        return None
    v = val.strip().lower()
    aliases = {"hi": "high", "h": "high", "med": "medium", "m": "medium",
               "lo": "low", "l": "low"}
    v = aliases.get(v, v)
    if v not in CONFIDENCE_LEVELS:
        sys.exit(f"--confidence must be one of {sorted(CONFIDENCE_LEVELS)} (got '{val}')")
    return v


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_read(root, args):
    data = load_working(root)
    if args.agent:
        data = dict(data)
        data["active"] = [i for i in data["active"] if i.get("agent") == args.agent]
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_profile(root, args):
    data = load_working(root)
    prof = data["profile"]
    for field in ("name", "title", "notes"):
        val = getattr(args, field)
        if val is not None:
            prof[field] = val
    if args.focus_areas is not None:
        prof["focus_areas"] = args.focus_areas
    if args.key_people is not None:
        try:
            people = json.loads(args.key_people)
            assert isinstance(people, list)
        except (ValueError, AssertionError):
            sys.exit("--key-people must be a JSON array of {name, role, note}")
        prof["key_people"] = people
    save_working(root, data)
    print("profile updated")


def cmd_add(root, args):
    data = load_working(root)
    item = {
        "id": new_id(),
        "agent": args.agent or DEFAULT_AGENT,
        "type": args.type,
        "text": args.text,
        "people": args.people or [],
        "tags": args.tags or [],
        "owner": args.owner,
        "due": args.due,
        "status": "open",
        "source": args.source,
        "goal": args.goal,
        "outcome": args.outcome,
        "constraints": args.constraints or [],
        "confidence": _check_confidence(args.confidence),
        "verify": args.verify,
        "unknowns": args.unknowns,
        "created_at": utc_today(),
        "updated_at": utc_today(),
    }
    data["active"].append(item)
    save_working(root, data)
    print(f"added {item['id']} [{item['agent']}]: {item['text']}")


def cmd_update(root, args):
    data = load_working(root)
    item = _find(data, args.id)
    for field in ("text", "owner", "due", "source", "status", "type", "agent",
                  "goal", "outcome", "verify", "unknowns"):
        val = getattr(args, field)
        if val is not None:
            item[field] = val
    if args.people is not None:
        item["people"] = args.people
    if args.tags is not None:
        item["tags"] = args.tags
    if args.constraints is not None:
        item["constraints"] = args.constraints
    if args.confidence is not None:
        item["confidence"] = _check_confidence(args.confidence)
    item["updated_at"] = utc_today()
    save_working(root, data)
    print(f"updated {item['id']}")


def cmd_resolve(root, args):
    data = load_working(root)
    item = _find(data, args.id)
    item["status"] = "done"
    item["updated_at"] = utc_today()
    save_working(root, data)
    print(f"resolved {item['id']} (run compact to archive it)")


def _find(data, item_id):
    for item in data["active"]:
        if item["id"] == item_id:
            return item
    sys.exit(f"no active item with id '{item_id}'")


def cmd_scan(root, args):
    data = load_working(root)
    today = datetime.now(timezone.utc).date()
    items = data["active"]
    if args.agent:
        items = [i for i in items if i.get("agent") == args.agent]
    overdue, done, stale, dupes = [], [], [], []
    by_text = {}
    for item in items:
        if item.get("status") == "done":
            done.append(item["id"])
        due = item.get("due")
        if item.get("status") == "open" and due and _is_iso(due):
            if datetime.fromisoformat(due).date() < today:
                overdue.append(item["id"])
        upd = item.get("updated_at")
        if item.get("status") == "open" and upd and _is_iso(upd):
            if datetime.fromisoformat(upd).date() < today - timedelta(days=STALE_DAYS):
                stale.append(item["id"])
        key = re.sub(r"\s+", " ", (item.get("text") or "").strip().lower())
        by_text.setdefault(key, []).append(item["id"])
    for ids in by_text.values():
        if len(ids) > 1:
            dupes.append(ids)

    # Long-term chopping block: records not used in a long time AND not well-connected.
    # Reactivation (recall --reinforce / reinforce / link) resets last_used, and a
    # strongly-wired record is spared even when dormant — use it or lose it.
    con = connect_db(root)
    now = datetime.now(timezone.utc)
    conn_map = _connectivity(con, now)
    cutoff = (now - timedelta(days=LONGTERM_STALE_DAYS)).date()
    clause = " AND agent = ?" if args.agent else ""
    a = (args.agent,) if args.agent else ()
    stale_lt = []
    for rid, last_used, recorded_at, created_at in con.execute(
        "SELECT id,last_used,recorded_at,created_at FROM record "
        f"WHERE status != 'dropped' AND COALESCE(consolidated_into,'') = ''{clause}", a):
        stamp = last_used or recorded_at or created_at
        if not stamp or not _is_iso(stamp):
            continue
        if datetime.fromisoformat(stamp).date() >= cutoff:
            continue
        affinity = round(conn_map.get(rid, 0.0), 4)
        if affinity >= GRAPH_SPARE_AFFINITY:
            continue  # spared: part of a live cluster
        stale_lt.append({"id": rid, "last_used": stamp, "affinity": affinity})

    # Relied-on but unverified: below-high records that carry a `verify` path (a known
    # way to firm them up). Sorted by reliance (most recently used / best-connected
    # first) so the records the work keeps leaning on float to the top to `reconfirm`.
    unverified = []
    for rid, conf, verify, last_used in con.execute(
        "SELECT id,confidence,verify,last_used FROM record WHERE status != 'dropped' "
        "AND COALESCE(consolidated_into,'') = '' "
        f"AND verify IS NOT NULL AND verify != '' AND confidence IN ('low','medium'){clause}", a):
        unverified.append({"id": rid, "confidence": conf, "verify": verify,
                           "last_used": last_used, "affinity": round(conn_map.get(rid, 0.0), 4)})
    unverified.sort(key=lambda u: (u["last_used"] or "", u["affinity"]), reverse=True)
    con.close()

    print(json.dumps({"overdue": overdue, "done": done, "stale_days": STALE_DAYS,
                      "stale": stale, "duplicates": dupes, "count": len(items),
                      "longterm_stale_days": LONGTERM_STALE_DAYS,
                      "stale_longterm": stale_lt, "unverified": unverified}, indent=2))


# Compact English stopword set for keyword suggestion (dream). Not exhaustive — just
# enough to keep suggested keywords substantive; the agent curates the final set.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "of", "to", "in", "on", "for",
    "with", "at", "by", "from", "as", "is", "are", "was", "were", "be", "been", "being",
    "it", "its", "this", "that", "these", "those", "i", "you", "he", "she", "they", "we",
    "them", "his", "her", "their", "our", "your", "my", "me", "us", "do", "does", "did",
    "done", "so", "not", "no", "yes", "can", "will", "would", "should", "could", "may",
    "might", "must", "have", "has", "had", "about", "into", "over", "than", "too", "very",
    "just", "also", "more", "most", "some", "any", "all", "each", "who", "what", "when",
    "where", "which", "how", "user", "users", "prefers", "prefer", "preferred",
}


def _suggest_keywords(members, limit=12):
    """Pull salient keywords from a cluster's members (their tags + text + constraints)
    to seed an `insight`'s recall tags, so the folded topics still surface on their own
    words. Existing tags are always kept (already-curated recall keys); the rest are the
    most frequent stopword-filtered words across the members. A starting point for the
    agent to curate — not an authority."""
    from collections import Counter
    counts = Counter()
    kept = []  # curated tags, kept regardless of frequency
    for m in members:
        for t in (m.get("tags") or []):
            tl = t.strip().lower()
            if tl and tl not in kept:
                kept.append(tl)
        blob = " ".join([m.get("text") or ""] + list(m.get("constraints") or []))
        for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", blob.lower()):
            if w not in _STOPWORDS:
                counts[w] += 1
    out = list(kept)
    for w, _n in counts.most_common():
        if len(out) >= limit:
            break
        if w not in out:
            out.append(w)
    return out[:limit]


def _clusters(con, agent=None, now=None):
    """Dense neighbourhoods in the co-recall graph worth rolling up into an `insight`.
    Union-find over edges whose decayed weight clears DREAM_EDGE_FLOOR, restricted to
    live records (non-dropped and not already consolidated). Returns clusters of at
    least DREAM_MIN_CLUSTER members, densest first (density = summed intra-cluster
    decayed weight)."""
    now = now or datetime.now(timezone.utc)
    clause = " AND agent = ?" if agent else ""
    a = (agent,) if agent else ()
    eligible = {}
    for rid, ag, kind, text, tags, conf, constraints in con.execute(
        "SELECT id,agent,kind,text,tags,confidence,constraints FROM record "
        f"WHERE status != 'dropped' AND COALESCE(consolidated_into,'') = ''{clause}", a):
        eligible[rid] = {"id": rid, "agent": ag, "kind": kind, "text": text,
                         "tags": _json_list(tags), "confidence": conf,
                         "constraints": _json_list(constraints)}
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    intra = []  # (a, b, decayed_weight) for edges inside the eligible set
    for ea, eb, w, upd in con.execute("SELECT a,b,weight,updated_at FROM edge"):
        if ea not in eligible or eb not in eligible:
            continue
        dw = _decay(w, upd, now)
        if dw < DREAM_EDGE_FLOOR:
            continue
        union(ea, eb)
        intra.append((ea, eb, dw))
    groups = {}
    for rid in eligible:
        groups.setdefault(find(rid), []).append(rid)
    density = {}
    for ea, _eb, dw in intra:
        root = find(ea)
        density[root] = density.get(root, 0.0) + dw
    out = []
    for root, ids in groups.items():
        if len(ids) < DREAM_MIN_CLUSTER:
            continue
        members = [eligible[i] for i in ids]
        out.append({"size": len(ids), "density": round(density.get(root, 0.0), 4),
                    "suggested_keywords": _suggest_keywords(members),
                    "members": members})
    out.sort(key=lambda c: c["density"], reverse=True)
    return out[:DREAM_MAX_CLUSTERS]


def _persona_material(con, agent=None):
    """High-confidence facts that feed a persona (profile) re-synthesis, newest first."""
    clause = " AND agent = ?" if agent else ""
    a = (agent,) if agent else ()
    rows = con.execute(
        "SELECT id,agent,text,tags,people FROM record "
        "WHERE kind='fact' AND confidence='high' AND status != 'dropped' "
        f"AND COALESCE(consolidated_into,'') = ''{clause} "
        "ORDER BY recorded_at DESC LIMIT ?", (*a, DREAM_PERSONA_FACTS)).fetchall()
    return [{"id": r[0], "agent": r[1], "text": r[2],
             "tags": _json_list(r[3]), "people": _json_list(r[4])} for r in rows]


def cmd_dream(root, args):
    """Read-only consolidation pass — memory's 'sleep'. Surfaces dense graph clusters
    to roll up into an `insight`, plus the high-confidence facts and current profile to
    re-synthesise the persona. The agent reasons over what comes back, then calls
    `consolidate` (to fold a cluster) and/or `profile` (to refresh the persona).
    Writes nothing itself."""
    con = connect_db(root)
    clusters = _clusters(con, args.agent)
    persona_facts = _persona_material(con, args.agent)
    con.close()
    data = load_working(root)
    print(json.dumps({
        "clusters": clusters,
        "persona": {"profile": data["profile"],
                    "high_confidence_facts": persona_facts,
                    "fact_count": len(persona_facts)},
    }, indent=2, ensure_ascii=False))


def cmd_consolidate(root, args):
    """Fold a cluster of atoms into a new higher-altitude `insight` (memory's L2/L3
    roll-up). Creates the insight, links it to each source atom so they stay reachable
    one hop behind it, and marks the atoms consolidated — held out of primary recall
    but kept as evidence. The summary becomes the hot recall hit; the atoms are its
    trail back to the ground truth. The insight inherits the union of the atoms' tags
    and people (plus any keywords passed via --tags) so the folded topics still surface
    on their own words."""
    kind = args.kind or "insight"
    if kind not in RECORD_KINDS:
        sys.exit(f"--kind must be one of {sorted(RECORD_KINDS)}")
    ids = [x for x in dict.fromkeys(args.from_ids or [])]
    if len(ids) < 2:
        sys.exit("--from needs at least two record ids to consolidate")
    con = connect_db(root)
    # Inherit the atoms' curated recall keys so the folded topics still surface: the
    # insight's tags/people are the agent-supplied ones (major keywords pulled from the
    # originals — dream suggests them) plus the union of every source atom's tags/people.
    inherited_tags, inherited_people = [], []
    for sid in ids:
        row = con.execute(
            "SELECT status, consolidated_into, tags, people FROM record WHERE id=?",
            (sid,)).fetchone()
        if row is None:
            con.close()
            sys.exit(f"unknown record id: {sid}")
        if row[0] == "dropped":
            con.close()
            sys.exit(f"record {sid} is dropped; resurface it or leave it out")
        if row[1]:
            con.close()
            sys.exit(f"record {sid} is already consolidated (into {row[1]})")
        for t in _json_list(row[2]):
            if t not in inherited_tags:
                inherited_tags.append(t)
        for p in _json_list(row[3]):
            if p not in inherited_people:
                inherited_people.append(p)
    final_tags = list(dict.fromkeys((args.tags or []) + inherited_tags))
    final_people = list(dict.fromkeys((args.people or []) + inherited_people))
    rid = new_id()
    now_ts = utc_now()
    con.execute(
        "INSERT INTO record"
        "(id,agent,kind,type,text,people,tags,owner,due,status,source,created_at,"
        "recorded_at,reason,rationale,goal,outcome,constraints,confidence,verify,unknowns,last_used)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, args.agent or DEFAULT_AGENT, kind, args.type, args.text,
         json.dumps(final_people), json.dumps(final_tags),
         None, None, "done", args.source, utc_today(), now_ts, None, None,
         args.goal, args.outcome, json.dumps(args.constraints or []),
         _check_confidence(args.confidence), args.verify, args.unknowns, now_ts))
    for sid in ids:
        con.execute("UPDATE record SET consolidated_into=? WHERE id=?", (rid, sid))
        _bump_edge(con, rid, sid, MANUAL_LINK_FLOOR, "manual", now_ts)
    log_event(con, args.agent or DEFAULT_AGENT, "consolidate", rid,
              f"{args.text} (folded {len(ids)})")
    con.commit()
    con.close()
    print(f"consolidated {len(ids)} record(s) into {kind} {rid} "
          f"[{args.agent or DEFAULT_AGENT}]: {args.text}")


def cmd_compact(root, args):
    data = load_working(root)
    con = connect_db(root)
    moved = []
    for iid in (args.archive or []):
        moved.append(_to_record(data, con, iid, status=None, reason=args.reason))
    for iid in (args.drop or []):
        moved.append(_to_record(data, con, iid, status="dropped",
                                 reason=args.reason or "dropped during compaction"))
    data["active"] = [i for i in data["active"] if i["id"] not in moved]
    pruned = cap_log(con)
    con.commit()
    con.close()
    save_working(root, data)
    print(f"moved {len(moved)} item(s) to long-term; pruned {pruned} old log row(s)")


def _to_record(data, con, item_id, status, reason):
    item = next((i for i in data["active"] if i["id"] == item_id), None)
    if item is None:
        sys.exit(f"no active item with id '{item_id}' to archive")
    final_status = status or ("done" if item.get("status") == "done" else "open")
    now_ts = utc_now()
    con.execute(
        "INSERT OR REPLACE INTO record"
        "(id,agent,kind,type,text,people,tags,owner,due,status,source,created_at,"
        "recorded_at,reason,rationale,goal,outcome,constraints,confidence,verify,unknowns,last_used)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (item["id"], item.get("agent"), "item", item.get("type"), item.get("text"),
         json.dumps(item.get("people") or []), json.dumps(item.get("tags") or []),
         item.get("owner"), item.get("due"), final_status, item.get("source"),
         item.get("created_at"), now_ts, reason, None,
         item.get("goal"), item.get("outcome"),
         json.dumps(item.get("constraints") or []), item.get("confidence"),
         item.get("verify"), item.get("unknowns"), now_ts),
    )
    log_event(con, item.get("agent"), "drop" if status == "dropped" else "archive",
              item["id"], f"{item.get('type')}: {item.get('text')}")
    return item["id"]


def cmd_resurface(root, args):
    data = load_working(root)
    con = connect_db(root)
    row = con.execute(
        "SELECT id,agent,type,text,people,tags,owner,due,source,created_at,"
        "goal,outcome,constraints,confidence,verify,unknowns FROM record "
        "WHERE id=? AND kind='item'", (args.id,)).fetchone()
    if row is None:
        con.close()
        sys.exit(f"no archived item with id '{args.id}'")
    item = {
        "id": row[0], "agent": row[1], "type": row[2], "text": row[3],
        "people": _json_list(row[4]), "tags": _json_list(row[5]),
        "owner": row[6], "due": row[7], "status": "open", "source": row[8],
        "goal": row[10], "outcome": row[11],
        "constraints": _json_list(row[12]), "confidence": row[13],
        "verify": row[14], "unknowns": row[15],
        "created_at": row[9] or utc_today(), "updated_at": utc_today(),
    }
    con.execute("DELETE FROM record WHERE id=?", (args.id,))
    con.execute("DELETE FROM edge WHERE a=? OR b=?", (args.id, args.id))
    log_event(con, item["agent"], "resurface", args.id, item["text"])
    con.commit()
    con.close()
    data["active"].append(item)
    save_working(root, data)
    print(f"resurfaced {item['id']} [{item['agent']}]: {item['text']}")


def cmd_record(root, args):
    if args.kind not in RECORD_KINDS:
        sys.exit(f"--kind must be one of {sorted(RECORD_KINDS)}")
    confidence = _check_confidence(args.confidence)
    con = connect_db(root)
    rid = new_id()
    now_ts = utc_now()
    con.execute(
        "INSERT INTO record"
        "(id,agent,kind,type,text,people,tags,owner,due,status,source,created_at,"
        "recorded_at,reason,rationale,goal,outcome,constraints,confidence,verify,unknowns,last_used)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, args.agent or DEFAULT_AGENT, args.kind, args.type, args.text,
         json.dumps(args.people or []), json.dumps(args.tags or []),
         None, None, "done", args.source, utc_today(), now_ts, None, args.rationale,
         args.goal, args.outcome, json.dumps(args.constraints or []),
         confidence, args.verify, args.unknowns, now_ts),
    )
    log_event(con, args.agent or DEFAULT_AGENT, args.kind, rid, args.text)
    # Write-time advisory: flag any near-duplicate or high-confidence conflict so the
    # agent can consolidate or confirm. Non-blocking — the record is already written.
    flagged = [s for s in _find_similar(con, args.text, args.kind, args.tags,
                                        args.people, exclude_id=rid)
               if s["duplicate"] or s["conflict"]]
    con.commit()
    con.close()
    print(f"recorded {args.kind} {rid} [{args.agent or DEFAULT_AGENT}]: {args.text}")
    for s in flagged:
        label = ("possible duplicate" if s["duplicate"]
                 else f"related high-confidence {s['kind']}")
        print(f"  ⚠ {label}: {s['id']} ({s['kind']}, {s['confidence'] or 'n/a'})"
              f" \"{s['text']}\"")
    if flagged:
        print("  → before persisting, consolidate a duplicate (link/forget) or"
              " confirm a conflicting update with the user.")


def cmd_amend(root, args):
    """Update selected fields on a non-dropped long-term record in place.

    This preserves the lifelong record id and graph edges, refreshes last_used,
    and appends an audit-log entry. Omitted fields stay unchanged; list options
    may be passed with no values to clear them.
    """
    fields = (
        "type", "text", "people", "tags", "owner", "due", "source",
        "rationale", "goal", "outcome", "constraints", "confidence",
        "verify", "unknowns",
    )
    changes = {}
    for field in fields:
        value = getattr(args, field, None)
        if value is None:
            continue
        if field == "text":
            value = value.strip()
            if not value:
                sys.exit("--text cannot be empty")
        elif field in ("people", "tags", "constraints"):
            value = json.dumps(value)
        elif field == "confidence":
            value = _check_confidence(value)
        elif field == "due":
            value = value or None
            if value and not _is_iso(value):
                sys.exit("--due must be YYYY-MM-DD or an empty string to clear")
        elif field in ("type", "owner", "source", "rationale", "goal",
                       "outcome", "verify", "unknowns"):
            value = value or None
        changes[field] = value
    if not changes:
        sys.exit("nothing to amend: pass at least one editable field")

    con = connect_db(root)
    row = con.execute(
        "SELECT agent,status FROM record WHERE id=?", (args.id,)).fetchone()
    if row is None or row[1] == "dropped":
        con.close()
        sys.exit(f"unknown (or dropped) record id: {args.id}")
    record_agent = row[0]
    if args.agent and args.agent != record_agent:
        con.close()
        sys.exit(f"record {args.id} belongs to agent '{record_agent}', not '{args.agent}'")

    changes["last_used"] = utc_now()
    columns = list(changes)
    assignments = ", ".join(f"{field}=?" for field in columns)
    con.execute(
        f"UPDATE record SET {assignments} WHERE id=?",
        (*[changes[field] for field in columns], args.id),
    )
    changed = [field for field in columns if field != "last_used"]
    summary = f"amended fields: {', '.join(changed)}"
    log_event(con, record_agent, "amend", args.id, summary)
    con.commit()
    con.close()
    print(f"amended {args.id} [{record_agent}]: {', '.join(changed)}")

def cmd_check(root, args):
    """Probe before writing: would this proposed record duplicate or contradict
    something already stored? Read-only — surfaces similar records and the
    duplicate/conflict signal so the agent can decide whether to ask first."""
    con = connect_db(root)
    sim = _find_similar(con, args.text, args.kind, args.tags, args.people)
    con.close()
    print(json.dumps({
        "proposed": {"kind": args.kind, "text": args.text,
                     "tags": args.tags or [], "people": args.people or [],
                     "confidence": args.confidence},
        "similar": sim,
        "has_duplicate": any(s["duplicate"] for s in sim),
        "has_conflict": any(s["conflict"] for s in sim),
    }, indent=2, ensure_ascii=False))


RECALL_COLS = ("id,agent,kind,type,text,people,tags,due,status,"
               "goal,outcome,constraints,confidence,source,verify,unknowns")


def _row_to_hit(r, why):
    hit = {"id": r[0], "agent": r[1], "kind": r[2], "type": r[3], "text": r[4],
           "people": _json_list(r[5]), "tags": _json_list(r[6]),
           "due": r[7], "status": r[8], "why": why}
    goal, outcome, constraints, confidence = r[9], r[10], r[11], r[12]
    if goal:
        hit["goal"] = goal
    if outcome:
        hit["outcome"] = outcome
    constraints = _json_list(constraints)
    if constraints:
        hit["constraints"] = constraints
    if confidence:
        hit["confidence"] = confidence
    # Confidence basis (the why behind the level): evidence held = source,
    # the upgrade path = verify, the gaps = unknowns. Surface when present.
    source, verify, unknowns = r[13], r[14], r[15]
    if source:
        hit["source"] = source
    if verify:
        hit["verify"] = verify
    if unknowns:
        hit["unknowns"] = unknowns
    return hit


def _due_hits(con, agent):
    """Open long-term items whose due date has arrived."""
    clause = " AND agent = ?" if agent else ""
    a = (agent,) if agent else ()
    rows = con.execute(
        f"SELECT {RECALL_COLS} FROM record WHERE kind='item' AND status NOT IN ('done','dropped') "
        f"AND due IS NOT NULL AND due <= ?{clause} ORDER BY due",
        (utc_today(), *a)).fetchall()
    return [_row_to_hit(r, "due date reached") for r in rows]


def _fts_expr(terms):
    """Build a forgiving FTS5 MATCH expression: each word becomes a prefix token,
    OR-ed together. Stripping to \\w tokens drops FTS operators so user input can
    never produce a syntax error."""
    toks = []
    for term in terms or []:
        for w in re.findall(r"\w+", term.lower()):
            toks.append(w + "*")
    return " OR ".join(dict.fromkeys(toks))  # dedupe, keep order


def _fts_ranked(con, terms, agent, pool):
    """Retrieve candidates for `terms` from a transient FTS5 index, scored by
    column-weighted BM25 (smaller = better). Returns up to `pool` (id, score) rows
    best-first, or None if FTS5 is unavailable."""
    expr = _fts_expr(terms)
    if not expr:
        return []
    try:
        con.execute("DROP TABLE IF EXISTS recall_fts")
        con.execute("CREATE VIRTUAL TABLE temp.recall_fts USING fts5("
                    "id UNINDEXED, text, people, tags, owner, goal, outcome, constraints, "
                    "source, verify, unknowns, "
                    "tokenize='porter unicode61')")
    except sqlite3.OperationalError:
        return None  # FTS5 not compiled in
    clause = " WHERE agent = ?" if agent else ""
    a = (agent,) if agent else ()
    rows = con.execute(
        "SELECT id,text,people,tags,owner,goal,outcome,constraints,source,verify,unknowns "
        f"FROM record{clause}", a).fetchall()
    con.executemany(
        "INSERT INTO recall_fts"
        "(id,text,people,tags,owner,goal,outcome,constraints,source,verify,unknowns) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(r[0], r[1] or "", " ".join(_json_list(r[2])), " ".join(_json_list(r[3])),
          r[4] or "", r[5] or "", r[6] or "", " ".join(_json_list(r[7])),
          r[8] or "", r[9] or "", r[10] or "")
         for r in rows])
    weights = ", ".join(str(w) for w in BM25_WEIGHTS)
    try:
        matched = con.execute(
            f"SELECT id, bm25(recall_fts, {weights}) AS s FROM recall_fts "
            "WHERE recall_fts MATCH ? ORDER BY s LIMIT ?",
            (expr, pool)).fetchall()
    finally:
        con.execute("DROP TABLE IF EXISTS recall_fts")
    return [(m[0], m[1]) for m in matched]


def _competition_rank(items, key, reverse=False):
    """Map each item to a 0-based rank; equal keys share a rank (so a secondary
    signal can break genuine ties rather than arbitrary retrieval order)."""
    pos, prev, rank = {}, object(), 0
    for idx, it in enumerate(sorted(items, key=key, reverse=reverse)):
        k = key(it)
        if k != prev:
            rank, prev = idx, k
        pos[it] = rank
    return pos


def _like_hits(con, terms, agent, limit):
    """Substring fallback used only when FTS5 is unavailable."""
    clause = " AND agent = ?" if agent else ""
    a = (agent,) if agent else ()
    out, seen = [], set()
    for term in terms:
        like = f"%{term}%"
        for r in con.execute(
            f"SELECT {RECALL_COLS} FROM record "
            f"WHERE (text LIKE ? OR people LIKE ? OR tags LIKE ? OR owner LIKE ?){clause} "
            "ORDER BY recorded_at DESC LIMIT ?",
            (like, like, like, like, *a, limit)).fetchall():
            if r[0] not in seen:
                seen.add(r[0])
                out.append(_row_to_hit(r, f"matches '{term}'"))
    return out


def _fetch_records(con, ids):
    """Map id -> RECALL row (+recorded_at) for the given ids, in one query."""
    ids = [x for x in dict.fromkeys(ids)]
    if not ids:
        return {}
    qs = ",".join("?" for _ in ids)
    rows = con.execute(
        f"SELECT {RECALL_COLS},recorded_at FROM record WHERE id IN ({qs})", ids).fetchall()
    return {r[0]: r for r in rows}


def _norm_text(s):
    return " ".join((s or "").lower().split())


def _find_similar(con, text, kind=None, tags=None, people=None,
                  exclude_id=None, limit=SIMILAR_RETURN):
    """Read-only: surface existing long-term records resembling a proposed write,
    so the agent can decide whether to just save or pause and confirm. Retrieves
    candidates by FTS over text+tags+people (across all agents — memory is shared),
    then scores text overlap with difflib. Flags each as a likely `duplicate` (high
    text overlap) and/or a `conflict` (a high-confidence fact/decision on a related
    subject that a new write might be superseding). Advisory only; never writes."""
    tags = [t for t in (tags or [])]
    people = [p for p in (people or [])]
    terms = [text] + tags + people
    ranked = _fts_ranked(con, terms, None, SIMILAR_POOL)
    if ranked is None:  # FTS5 unavailable -> best-effort substring match on the text
        cand_ids = [h["id"] for h in _like_hits(con, [text], None, SIMILAR_POOL)]
    else:
        cand_ids = [i for i, _ in ranked]
    by_id = _fetch_records(con, cand_ids)
    nt = _norm_text(text)
    ptags = {t.lower() for t in tags}
    ppeople = {p.lower() for p in people}
    out = []
    for i in cand_ids:
        if i == exclude_id or i not in by_id:
            continue
        r = by_id[i]
        if r[8] == "dropped":            # status — retired on purpose, ignore
            continue
        ratio = difflib.SequenceMatcher(None, nt, _norm_text(r[4])).ratio()  # r[4]=text
        shared_tags = sorted(ptags & {t.lower() for t in _json_list(r[6])})
        shared_people = sorted(ppeople & {p.lower() for p in _json_list(r[5])})
        if ratio < SIMILAR_FLOOR and not shared_tags and not shared_people:
            continue                     # too unrelated to be worth surfacing
        ckind, cconf = r[2], r[12]
        duplicate = ratio >= DUP_RATIO
        conflict = (not duplicate and cconf == "high" and ckind in CONFLICT_KINDS
                    and (bool(shared_tags) or bool(shared_people) or ratio >= 0.5))
        out.append({"id": i, "agent": r[1], "kind": ckind, "text": r[4],
                    "confidence": cconf, "shared_tags": shared_tags,
                    "shared_people": shared_people, "ratio": round(ratio, 2),
                    "duplicate": duplicate, "conflict": conflict})
    out.sort(key=lambda h: (h["duplicate"], h["conflict"], h["ratio"]), reverse=True)
    return out[:limit]


def _spreading_affinity(con, seeds, now=None):
    """One-hop affinity over edges touching the seed set. Both endpoints of a
    seed-incident edge earn the decayed weight: a neighbour earns it for being wired
    to a match, and a seed earns it as a hub for its own connections — so a well-
    connected text match isn't out-ranked by its own neighbours. Returns
    {id: affinity}; non-seed ids in here are candidates to pull in."""
    seeds = set(seeds)
    if not seeds:
        return {}
    qs = ",".join("?" for _ in seeds)
    aff = {}
    for a, b, w, upd in con.execute(
        f"SELECT a,b,weight,updated_at FROM edge WHERE a IN ({qs}) OR b IN ({qs})",
            (*seeds, *seeds)):
        dw = _decay(w, upd, now)
        if dw <= 0:
            continue
        aff[a] = aff.get(a, 0.0) + dw
        aff[b] = aff.get(b, 0.0) + dw
    return aff


def _query_hits(con, terms, agent, limit=10):
    """Ranked keyword search over long-term records. Retrieves with FTS5, then
    re-ranks by fusing relevance + recency + graph affinity, with kind/confidence
    nudges (see fusion constants). Spreading activation pulls in records strongly
    linked to the top text hits. Falls back to substring LIKE if FTS5 is missing."""
    if not terms:
        return []
    ranked = _fts_ranked(con, terms, agent, max(limit * 3, 30))
    if ranked is None:
        return _like_hits(con, terms, agent, limit)
    if not ranked:
        return []
    bm25 = dict(ranked)
    by_id = _fetch_records(con, list(bm25))
    # dropped records were retired on purpose; consolidated atoms were folded under an
    # insight — both are held out of the *primary* hits. A consolidated atom can still
    # surface one hop behind its summary via spreading activation (the `extra` set).
    folded = _consolidated_ids(con, list(bm25))
    cand = [i for i in bm25 if i in by_id and by_id[i][8] != "dropped" and i not in folded]
    if not cand:
        return []
    text_ids = set(cand)

    # Spreading activation: seed from the strongest text hits, pull in their neighbours.
    seeds = sorted(cand, key=lambda i: bm25[i])[:GRAPH_SEEDS]
    affinity = _spreading_affinity(con, seeds)
    extra = sorted((i for i in affinity if i not in text_ids
                    and affinity[i] >= GRAPH_MIN_AFFINITY),
                   key=lambda i: affinity[i], reverse=True)[:GRAPH_FANOUT]
    if extra:
        by_id.update(_fetch_records(con, extra))
        cand += [i for i in extra if i in by_id and by_id[i][8] != "dropped"]

    # Three ranked lists fused with RRF. Equal keys share a rank, so weaker signals
    # genuinely break ties. Graph-only candidates get the worst text rank.
    worst_bm = max(bm25.values()) + 1.0
    text_pos = _competition_rank(cand, key=lambda i: bm25.get(i, worst_bm))
    rec_pos = _competition_rank(cand, key=lambda i: by_id[i][16] or "", reverse=True)  # [16] = recorded_at
    graph_pos = _competition_rank(cand, key=lambda i: affinity.get(i, 0.0), reverse=True)

    def score(i):
        rrf = (W_TEXT / (RRF_K + text_pos[i])
               + W_RECENCY / (RRF_K + rec_pos[i])
               + W_GRAPH / (RRF_K + graph_pos[i]))
        kind_w = KIND_WEIGHT.get(by_id[i][2], 1.0)          # by_id[i][2] = kind
        conf_w = CONFIDENCE_WEIGHT.get(by_id[i][12], 1.0)   # by_id[i][12] = confidence
        return rrf * kind_w * conf_w

    matched_why = "matches " + " ".join(terms)
    ranked_ids = sorted(cand, key=score, reverse=True)[:limit]
    return [_row_to_hit(by_id[i],
                        matched_why if i in text_ids else "linked to a match")
            for i in ranked_ids]


def _recent_hits(con, agent, limit=RECENT_DEFAULT):
    """No-query fallback: surface an agent's most relevant recent long-term records.

    Keyword recall needs query terms; a bare summon (or a query that matches nothing)
    would otherwise hand back an empty set even when the store holds plenty for this
    agent. So pull a recency-ordered pool, then rerank inside it by recency fused with
    the same kind/confidence nudges keyword recall uses — a high-value fact/lesson can
    edge out a slightly newer note, but only recent records are ever in play. Excludes
    'dropped' (retired) records, matching query recall; NULL status (the norm for
    facts/decisions/lessons/notes) is kept."""
    clause = " AND agent = ?" if agent else ""
    a = (agent,) if agent else ()
    pool = max(limit * 3, 30)
    rows = con.execute(
        f"SELECT {RECALL_COLS},recorded_at FROM record "
        f"WHERE COALESCE(status,'') != 'dropped' AND COALESCE(consolidated_into,'') = ''{clause} "
        "ORDER BY recorded_at DESC LIMIT ?",
        (*a, pool)).fetchall()
    if not rows:
        return []
    by_id = {r[0]: r for r in rows}
    cand = list(by_id)
    rec_pos = _competition_rank(cand, key=lambda i: by_id[i][16] or "", reverse=True)  # [16] = recorded_at

    def score(i):
        rrf = W_RECENCY / (RRF_K + rec_pos[i])
        kind_w = KIND_WEIGHT.get(by_id[i][2], 1.0)          # by_id[i][2] = kind
        conf_w = CONFIDENCE_WEIGHT.get(by_id[i][12], 1.0)   # by_id[i][12] = confidence
        return rrf * kind_w * conf_w

    ranked_ids = sorted(cand, key=score, reverse=True)[:limit]
    return [_row_to_hit(by_id[i], "recent (no query match)") for i in ranked_ids]


def _merge_hits(*groups):
    """Concatenate hit groups, keeping the first occurrence of each id."""
    out, seen = [], set()
    for group in groups:
        for h in group:
            if h["id"] not in seen:
                seen.add(h["id"])
                out.append(h)
    return out


def _maybe_reinforce(con, args, edge_hits, touch_hits):
    """Recall reinforces by default (suppress with --no-reinforce): every surfaced
    record is 'used' so its staleness clock resets, and the topical query matches
    are co-used so affinity is bumped across every pair. `edge_hits` are wired
    together; `touch_hits` (usually edge_hits + due items) just get last_used reset."""
    if getattr(args, "no_reinforce", False):
        return 0
    touch = [h["id"] for h in touch_hits]
    if touch:
        _touch_records(con, touch)
    edge_ids = [h["id"] for h in edge_hits]
    pairs = _reinforce(con, edge_ids, COUSE_INCREMENT, "corecall") if len(edge_ids) >= 2 else 0
    if touch or pairs:
        con.commit()
    return pairs


def cmd_recall(root, args):
    con = connect_db(root)
    due = _due_hits(con, args.agent)
    qhits = _query_hits(con, args.query or [], args.agent)
    hits = _merge_hits(due, qhits)
    if not args.query:  # no query terms supplied -> recency fallback so a bare recall isn't empty
        # (an explicit query that just misses still returns empty — that's an honest "no match")
        seen = {h["id"] for h in hits}
        hits = _merge_hits(hits, [h for h in _recent_hits(con, args.agent) if h["id"] not in seen])
    # reinforce only the topical query matches; reset last_used on everything surfaced
    _maybe_reinforce(con, args, edge_hits=qhits, touch_hits=hits)
    con.close()
    print(json.dumps({"candidates": hits}, indent=2, ensure_ascii=False))


def cmd_brief(root, args):
    data = load_working(root)
    active = data["active"]
    if args.agent:
        active = [i for i in active if i.get("agent") == args.agent]
    con = connect_db(root)
    due = _due_hits(con, args.agent)
    seen = {h["id"] for h in due}
    matches = []
    if args.query:
        matches = [m for m in _query_hits(con, args.query, args.agent) if m["id"] not in seen]
        seen.update(h["id"] for h in matches)
    # No query, or it matched nothing: fall back to the agent's most relevant recent
    # records so a bare summon still hands back substantive memory instead of nothing.
    recent = [] if matches else [h for h in _recent_hits(con, args.agent) if h["id"] not in seen]
    # wire the topical query matches; reset last_used on everything surfaced
    _maybe_reinforce(con, args, edge_hits=matches, touch_hits=due + matches + recent)
    con.close()
    out = {"agent": args.agent, "profile": data["profile"], "active": active, "due": due}
    if args.query:
        out["matches"] = matches
    if recent:
        out["recent"] = recent
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_agents(root, args):
    data = load_working(root)
    counts = {}
    for item in data["active"]:
        a = item.get("agent") or DEFAULT_AGENT
        counts.setdefault(a, {"active": 0, "longterm": 0})
        counts[a]["active"] += 1
    con = connect_db(root)
    for agent, n in con.execute("SELECT agent, COUNT(*) FROM record GROUP BY agent").fetchall():
        a = agent or DEFAULT_AGENT
        counts.setdefault(a, {"active": 0, "longterm": 0})
        counts[a]["longterm"] += n
    con.close()
    print(json.dumps(counts, indent=2, ensure_ascii=False))


def cmd_link(root, args):
    con = connect_db(root)
    if not _record_exists(con, args.a) or not _record_exists(con, args.b):
        con.close()
        sys.exit("both --a and --b must be existing record ids")
    a, b = _edge_key(args.a, args.b)
    if args.weight is not None:
        now = utc_now()
        con.execute(
            "INSERT INTO edge(a,b,weight,count,source,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(a,b) DO UPDATE SET "
            "weight=excluded.weight, source='manual', updated_at=excluded.updated_at",
            (a, b, float(args.weight), 0, "manual", now, now))
        _touch_records(con, [a, b], now)
    else:
        _bump_edge(con, a, b, MANUAL_LINK_FLOOR, "manual")
    con.commit()
    con.close()
    print(f"linked {a} <-> {b}")


def cmd_unlink(root, args):
    con = connect_db(root)
    a, b = _edge_key(args.a, args.b)
    n = con.execute("DELETE FROM edge WHERE a=? AND b=?", (a, b)).rowcount
    con.commit()
    con.close()
    print(f"removed {n} edge(s)")


def cmd_links(root, args):
    con = connect_db(root)
    rows = con.execute(
        "SELECT a,b,weight,count,source,updated_at FROM edge WHERE a=? OR b=?",
        (args.id, args.id)).fetchall()
    out = []
    for a, b, w, cnt, src, upd in rows:
        out.append({"id": b if a == args.id else a,
                    "weight": round(_decay(w, upd), 4), "count": cnt, "source": src})
    out.sort(key=lambda x: x["weight"], reverse=True)
    con.close()
    print(json.dumps({"id": args.id, "links": out}, indent=2, ensure_ascii=False))


def cmd_reinforce(root, args):
    ids = [x for x in dict.fromkeys(args.ids or [])]
    if len(ids) < 2:
        sys.exit("--ids needs at least two distinct record ids")
    con = connect_db(root)
    missing = [x for x in ids if not _record_exists(con, x)]
    if missing:
        con.close()
        sys.exit(f"unknown record ids: {', '.join(missing)}")
    inc = args.weight if args.weight is not None else COUSE_INCREMENT
    pairs = _reinforce(con, ids, inc, "corecall")
    con.commit()
    con.close()
    print(f"reinforced {pairs} edge(s) across {len(ids)} memories")


def cmd_forget(root, args):
    """Retire long-term records that hit the chopping block: mark them dropped
    (kept for audit, excluded from recall) and drop their edges."""
    ids = [x for x in dict.fromkeys(args.ids or [])]
    if not ids:
        sys.exit("--ids needs at least one record id")
    con = connect_db(root)
    missing = [x for x in ids if not _record_exists(con, x)]
    if missing:
        con.close()
        sys.exit(f"unknown record ids: {', '.join(missing)}")
    reason = args.reason or "forgotten (stale, unused)"
    forgotten = 0
    for rid in ids:
        con.execute("UPDATE record SET status='dropped', reason=? WHERE id=?", (reason, rid))
        con.execute("DELETE FROM edge WHERE a=? OR b=?", (rid, rid))
        agent = con.execute("SELECT agent FROM record WHERE id=?", (rid,)).fetchone()
        log_event(con, agent[0] if agent else None, "drop", rid, reason)
        forgotten += 1
    con.commit()
    con.close()
    print(f"forgot {forgotten} record(s)")


def cmd_reconfirm(root, args):
    """Close the verification loop on a record: fold in the evidence you found
    (`source`), adjust `confidence` (up or down — disconfirming evidence is valid),
    optionally rewrite or clear the remaining `verify` path, reset the staleness
    clock (it's a reactivation), and log the change. Acts on long-term records."""
    if args.confidence is None and args.source is None and args.verify is None:
        sys.exit("nothing to reconfirm: pass at least one of --confidence/--source/--verify")
    con = connect_db(root)
    row = con.execute(
        "SELECT agent,confidence,source,verify FROM record WHERE id=? AND status != 'dropped'",
        (args.id,)).fetchone()
    if row is None:
        con.close()
        sys.exit(f"unknown (or dropped) record id: {args.id}")
    agent, old_conf, old_source, old_verify = row
    new_conf = _check_confidence(args.confidence) if args.confidence else old_conf
    # Evidence held grows a trail: append by default so prior provenance isn't lost.
    new_source = old_source
    if args.source is not None:
        new_source = args.source if (args.replace_source or not old_source) \
            else f"{old_source}; {args.source}"
    # Pass --verify "" to clear the path (walked); pass new text to rewrite; omit to keep.
    new_verify = (args.verify or None) if args.verify is not None else old_verify
    con.execute("UPDATE record SET confidence=?, source=?, verify=?, last_used=? WHERE id=?",
                (new_conf, new_source, new_verify, utc_now(), args.id))
    summary = f"reconfirm {old_conf or 'n/a'}→{new_conf or 'n/a'}"
    if args.source:
        summary += f" ({args.source})"
    log_event(con, agent, "reconfirm", args.id, summary)
    con.commit()
    con.close()
    print(f"reconfirmed {args.id}: confidence {old_conf or 'n/a'} → {new_conf or 'n/a'}")


def cmd_validate(root, args):
    p = working_path(root)
    if not p.exists():
        print("memory.json does not exist yet (nothing to validate).")
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(json.dumps([{"severity": "fatal", "issue": f"not valid JSON: {e}"}], indent=2))
        if args.fix:
            print("\nCannot auto-fix unparseable JSON. Restore memory.json.bak.")
        return
    problems = validate_working(data)
    if not problems:
        print("memory.json is valid.")
        return
    print(json.dumps([{"severity": s, "issue": m} for s, m in problems], indent=2))
    if args.fix:
        fatal = [m for s, m in problems if s == "fatal"]
        if fatal:
            print("\nNot auto-fixing: fatal issues need human review:")
            for m in fatal:
                print(f"  - {m}")
            return
        changes = fix_working(data)
        save_working(root, data)
        print("\nApplied safe fixes (backup at memory.json.bak):")
        for c in changes:
            print(f"  - {c}")


def cmd_doctor(root, args):
    con = connect_db(root)
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    issues = []
    for r in con.execute("SELECT id,kind,status,due,created_at FROM record").fetchall():
        if r[1] not in RECORD_KINDS:
            issues.append(f"record {r[0]}: invalid kind {r[1]}")
        if r[2] and r[2] not in RECORD_STATUS:
            issues.append(f"record {r[0]}: invalid status {r[2]}")
        for label, v in (("due", r[3]), ("created_at", r[4])):
            if v and not _is_iso(v):
                issues.append(f"record {r[0]}: {label} not ISO: {v}")
    dangling = con.execute(
        "SELECT COUNT(*) FROM edge e WHERE NOT EXISTS(SELECT 1 FROM record r WHERE r.id=e.a) "
        "OR NOT EXISTS(SELECT 1 FROM record r WHERE r.id=e.b)").fetchone()[0]
    if dangling:
        issues.append(f"{dangling} edge(s) reference a missing record")
    n_rec = con.execute("SELECT COUNT(*) FROM record").fetchone()[0]
    n_log = con.execute("SELECT COUNT(*) FROM changelog").fetchone()[0]
    n_edge = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
    con.close()
    print(json.dumps({"integrity_check": integrity, "invariant_issues": issues,
                      "record_rows": n_rec, "changelog_rows": n_log,
                      "edge_rows": n_edge}, indent=2))


def cmd_render(root, args):
    data = load_working(root)
    p = data["profile"]
    people = []
    for kp in (p.get("key_people") or []):
        if isinstance(kp, dict):
            bit = kp.get("name", "")
            if kp.get("role"):
                bit += f" ({kp['role']})"
            people.append(bit)
        else:
            people.append(str(kp))
    lines = [f"# Memory for {p.get('name') or '(unnamed)'}", "",
             f"**Title:** {p.get('title') or '-'}",
             f"**Notes:** {p.get('notes') or '-'}",
             f"**Focus areas:** {', '.join(p.get('focus_areas') or []) or '-'}",
             f"**Key people:** {', '.join(people) or '-'}", "", "## Active items"]
    items = data["active"]
    if args.agent:
        items = [i for i in items if i.get("agent") == args.agent]
    if not items:
        lines.append("- (none)")
    for i in items:
        flag = " [done]" if i.get("status") == "done" else ""
        due = f" (due {i['due']})" if i.get("due") else ""
        lines.append(f"- [{i.get('agent')}] {i.get('text')}{due}{flag}  `{i['id']}`")
    print("\n".join(lines))


def cmd_export(root, args):
    con = connect_db(root)
    where = " WHERE agent = ?" if args.agent else ""
    a = (args.agent,) if args.agent else ()

    def dump(table, filt):
        cur = con.execute(f"SELECT * FROM {table}{filt}", a if filt else ())
        names = [c[0] for c in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]
    # edge has no agent column, so it is dumped whole (relationships are cross-agent).
    out = {"record": dump("record", where), "changelog": dump("changelog", where),
           "edge": dump("edge", "")}
    con.close()
    print(json.dumps(out, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(description="Professor Synapse shared memory CLI.")
    # Default None -> main() resolves the writable data root. An explicit --root wins.
    p.add_argument("--root", default=None)
    p.add_argument("--agent", help="active agent slug; writes stamp it, reads filter by it")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("read", help="show working memory (optionally one agent's)")

    pr = sub.add_parser("profile", help="set shared profile fields")
    pr.add_argument("--name")
    pr.add_argument("--title")
    pr.add_argument("--notes")
    pr.add_argument("--focus-areas", dest="focus_areas", nargs="*")
    pr.add_argument("--key-people", dest="key_people", help="JSON array of {name, role, note}")

    a = sub.add_parser("add", help="add an active item")
    a.add_argument("--type", help="free-form sub-type the agent defines")
    a.add_argument("--text", required=True)
    a.add_argument("--people", nargs="*")
    a.add_argument("--tags", nargs="*")
    a.add_argument("--owner")
    a.add_argument("--due", help="ISO date YYYY-MM-DD")
    a.add_argument("--source")
    a.add_argument("--goal", help="what this item is in service of")
    a.add_argument("--outcome", help="what resulted / current state")
    a.add_argument("--constraints", nargs="*", help="gotchas/limits (each one a quoted phrase)")
    a.add_argument("--confidence", help="high | medium | low")
    a.add_argument("--verify", help="confidence basis: what evidence is available and how to get it (the upgrade path)")
    a.add_argument("--unknowns", help="confidence basis: what remains unknown / unknown-unknown")

    u = sub.add_parser("update", help="update fields on an active item")
    u.add_argument("--id", required=True)
    u.add_argument("--text")
    u.add_argument("--people", nargs="*")
    u.add_argument("--tags", nargs="*")
    u.add_argument("--owner")
    u.add_argument("--due")
    u.add_argument("--source")
    u.add_argument("--status", choices=sorted(ITEM_STATUS))
    u.add_argument("--type")
    u.add_argument("--agent", dest="agent", default=argparse.SUPPRESS)
    u.add_argument("--goal")
    u.add_argument("--outcome")
    u.add_argument("--constraints", nargs="*", help="replaces the gotcha list")
    u.add_argument("--confidence", help="high | medium | low")
    u.add_argument("--verify", help="confidence basis: available evidence + how to get it")
    u.add_argument("--unknowns", help="confidence basis: what remains unknown")

    r = sub.add_parser("resolve", help="mark an active item done")
    r.add_argument("--id", required=True)

    sub.add_parser("scan", help="flag stale/overdue/done/duplicate active items")

    c = sub.add_parser("compact", help="move approved items to long-term, cap the log")
    c.add_argument("--archive", nargs="*", help="ids to archive")
    c.add_argument("--drop", nargs="*", help="ids to retire (kept in long-term)")
    c.add_argument("--reason")

    rs = sub.add_parser("resurface", help="move an archived item back to active")
    rs.add_argument("--id", required=True)

    rec = sub.add_parser("record", help="write straight to long-term")
    rec.add_argument("--kind", required=True, choices=sorted(RECORD_KINDS),
                     help="lesson = a reusable how-to (fills goal/outcome/constraints)")
    rec.add_argument("--text", required=True)
    rec.add_argument("--rationale")
    rec.add_argument("--type")
    rec.add_argument("--people", nargs="*")
    rec.add_argument("--tags", nargs="*")
    rec.add_argument("--source", help="confidence basis: evidence held / where the claim comes from")
    rec.add_argument("--goal", help="what was being attempted (esp. for lessons)")
    rec.add_argument("--outcome", help="what resulted (esp. for lessons)")
    rec.add_argument("--constraints", nargs="*", help="gotchas/limits (each a quoted phrase)")
    rec.add_argument("--confidence", help="high | medium | low (esp. for facts)")
    rec.add_argument("--verify", help="confidence basis: available evidence + how to get it (the upgrade path)")
    rec.add_argument("--unknowns", help="confidence basis: what remains unknown / unknown-unknown")

    am = sub.add_parser("amend", help="correct selected fields on a live long-term record in place")
    am.add_argument("--id", required=True)
    am.add_argument("--type")
    am.add_argument("--text")
    am.add_argument("--people", nargs="*")
    am.add_argument("--tags", nargs="*")
    am.add_argument("--owner")
    am.add_argument("--due", help="ISO date YYYY-MM-DD; pass an empty string to clear")
    am.add_argument("--source")
    am.add_argument("--rationale")
    am.add_argument("--goal")
    am.add_argument("--outcome")
    am.add_argument("--constraints", nargs="*", help="replaces the gotcha list")
    am.add_argument("--confidence", help="high | medium | low")
    am.add_argument("--verify")
    am.add_argument("--unknowns")

    ck = sub.add_parser("check", help="probe whether a proposed record duplicates/contradicts an existing one")
    ck.add_argument("--kind", choices=sorted(RECORD_KINDS), help="kind of the proposed record (sharpens conflict detection)")
    ck.add_argument("--text", required=True)
    ck.add_argument("--people", nargs="*")
    ck.add_argument("--tags", nargs="*")
    ck.add_argument("--confidence", help="proposed confidence (echoed back; not stored)")

    rc = sub.add_parser("recall", help="find long-term items worth surfacing")
    rc.add_argument("--query", nargs="*", help="people or topics to search (ranked FTS5)")
    rc.add_argument("query_pos", nargs="*", help="same as --query, positional: `recall summon gate`")
    rc.add_argument("--no-reinforce", dest="no_reinforce", action="store_true",
                    help="don't wire the returned set or reset its staleness (read-only)")

    br = sub.add_parser("brief", help="one-shot prefetch: profile + active + due (+ --query)")
    br.add_argument("--query", nargs="*", help="optional people/topics to also surface from long-term")
    br.add_argument("query_pos", nargs="*", help="same as --query, positional: `brief summon gate`")
    br.add_argument("--no-reinforce", dest="no_reinforce", action="store_true",
                    help="don't wire query matches or reset staleness (read-only)")

    lk = sub.add_parser("link", help="assert an explicit edge between two records")
    lk.add_argument("--a", required=True)
    lk.add_argument("--b", required=True)
    lk.add_argument("--weight", type=float, help="set an absolute weight (default: add the link floor)")

    ul = sub.add_parser("unlink", help="remove the edge between two records")
    ul.add_argument("--a", required=True)
    ul.add_argument("--b", required=True)

    ls = sub.add_parser("links", help="list a record's neighbours by decayed affinity")
    ls.add_argument("--id", required=True)

    rf = sub.add_parser("reinforce", help="bump co-use affinity across a set of records")
    rf.add_argument("--ids", nargs="+", required=True, help="two or more record ids used together")
    rf.add_argument("--weight", type=float, help="affinity increment (default: COUSE_INCREMENT)")

    fg = sub.add_parser("forget", help="retire dormant long-term records (mark dropped)")
    fg.add_argument("--ids", nargs="+", required=True)
    fg.add_argument("--reason")

    rcf = sub.add_parser("reconfirm", help="close the verify loop: fold in evidence, adjust confidence, log it")
    rcf.add_argument("--id", required=True)
    rcf.add_argument("--confidence", help="new level after checking (high|medium|low); may go up or down")
    rcf.add_argument("--source", help="evidence you confirmed it with (appended to existing unless --replace-source)")
    rcf.add_argument("--replace-source", dest="replace_source", action="store_true",
                     help="overwrite source instead of appending to the existing trail")
    rcf.add_argument("--verify", help="rewrite the remaining verify path; pass an empty string to clear it")

    sub.add_parser("dream", help="surface dense clusters to consolidate + persona material (read-only)")

    co = sub.add_parser("consolidate", help="fold a cluster of atoms into a higher-altitude insight")
    co.add_argument("--from", dest="from_ids", nargs="+", required=True,
                    help="two or more record ids to fold into the insight")
    co.add_argument("--text", required=True, help="the synthesised summary")
    co.add_argument("--kind", default="insight", choices=sorted(RECORD_KINDS),
                    help="kind of the roll-up (default: insight)")
    co.add_argument("--type")
    co.add_argument("--people", nargs="*", help="unioned with the atoms' people")
    co.add_argument("--tags", nargs="*",
                    help="major keywords pulled from the originals (see dream's "
                         "suggested_keywords) so the insight still surfaces on them; "
                         "unioned with every source atom's own tags")
    co.add_argument("--source", help="confidence basis: evidence held")
    co.add_argument("--goal")
    co.add_argument("--outcome")
    co.add_argument("--constraints", nargs="*", help="gotchas/limits (each a quoted phrase)")
    co.add_argument("--confidence", help="high | medium | low")
    co.add_argument("--verify", help="confidence basis: available evidence + how to get it")
    co.add_argument("--unknowns", help="confidence basis: what remains unknown")

    sub.add_parser("agents", help="list agents that have touched memory, with counts")

    v = sub.add_parser("validate", help="validate memory.json")
    v.add_argument("--fix", action="store_true")

    sub.add_parser("doctor", help="check long-term db integrity")
    sub.add_parser("render", help="print working memory as markdown")
    sub.add_parser("export", help="dump long-term db to JSON")

    # Make the global flags position-agnostic: accept --agent / --root AFTER the
    # subcommand too, not only before it. They are parsed at the top level (with
    # real defaults); on each subparser they default to SUPPRESS so that, when a
    # subcommand-level flag is NOT passed, it leaves the top-level value intact
    # instead of clobbering it back to None. Either order now works:
    #   memory.py --agent X record ...   and   memory.py record --agent X ...
    for _subp in sub.choices.values():
        _opts = {s for act in _subp._actions for s in act.option_strings}
        if "--agent" not in _opts:
            _subp.add_argument("--agent", dest="agent", default=argparse.SUPPRESS,
                               help="active agent slug (accepted before or after the subcommand)")
        if "--root" not in _opts:
            _subp.add_argument("--root", dest="root", default=argparse.SUPPRESS,
                               help="skill root (accepted before or after the subcommand)")
    return p


DISPATCH = {
    "read": cmd_read, "profile": cmd_profile, "add": cmd_add, "update": cmd_update,
    "resolve": cmd_resolve, "scan": cmd_scan, "compact": cmd_compact,
    "resurface": cmd_resurface, "record": cmd_record, "amend": cmd_amend, "check": cmd_check, "recall": cmd_recall,
    "brief": cmd_brief, "agents": cmd_agents, "validate": cmd_validate, "doctor": cmd_doctor,
    "render": cmd_render, "export": cmd_export,
    "link": cmd_link, "unlink": cmd_unlink, "links": cmd_links,
    "reinforce": cmd_reinforce, "forget": cmd_forget, "reconfirm": cmd_reconfirm,
    "dream": cmd_dream, "consolidate": cmd_consolidate,
}


# List-valued options use nargs="*", so they accept space-separated values
# (--tags a b). Agents and users also naturally type comma-separated values
# (--tags a,b) — normalize both forms to a clean list of distinct tokens.
# Short-token fields where comma-splitting is helpful. constraints is intentionally
# excluded: each gotcha is a phrase that may itself contain a comma.
_MULTI_FIELDS = ("people", "tags", "query", "focus_areas", "archive", "drop", "from_ids")


def _split_multi(values):
    out = []
    for v in values:
        for part in str(v).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def normalize_multi(args):
    for field in _MULTI_FIELDS:
        val = getattr(args, field, None)
        if val is not None:
            setattr(args, field, _split_multi(val))
    return args


def _merge_positional_query(args):
    """recall/brief accept query terms positionally too; fold them into --query
    when --query wasn't given so downstream code only ever reads args.query."""
    pos = getattr(args, "query_pos", None)
    if pos and not getattr(args, "query", None):
        args.query = pos
    return args


def main(argv=None):
    args = normalize_multi(_merge_positional_query(build_parser().parse_args(argv)))
    root = args.root if getattr(args, "root", None) else resolve_data_root(SKILL_ROOT)
    DISPATCH[args.cmd](root, args)


if __name__ == "__main__":
    main()
