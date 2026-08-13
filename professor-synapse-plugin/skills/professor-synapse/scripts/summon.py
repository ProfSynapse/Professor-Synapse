#!/usr/bin/env python3
"""Programmatic agent summoning for Professor Synapse.

Assembles a single "boot package" that contains everything needed to *become*
an agent: its full persona/instructions, the memory recalled for it, and the
resources it can load (with how to call them). stdout IS the summon — read it,
then become whoever it hands you.

Usage:
    python3 scripts/summon.py <agent> [--query TERMS ...] [--no-reinforce] [--json]

  <agent>            agent slug (e.g. memory-agent) or a phrase to match against
                     agent names/triggers/descriptions.
  --query TERMS      task terms to recall from memory. If omitted, the agent's
                     own triggers are used so you always get relevant context.
  --no-reinforce     pass through to memory recall: don't wire/reset staleness.
  --json             emit the boot package as JSON instead of markdown.
  --root PATH        skill root (defaults to this script's parent dir's parent).

Stdlib only — no pip installs.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

# Windows safety: force UTF-8 on stdout/stderr so the emoji in the boot package
# and recalled memory don't crash with a cp1252 UnicodeEncodeError. No-op where
# stdio is already UTF-8 (WSL/Linux/macOS).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(SCRIPT_DIR)

# Writable data root (user agents + memory + summon marker). In a plugin this is
# the persistent data dir that survives updates; in-place it's just the skill root.
try:
    from _pluginpaths import resolve_data_root
except Exception:  # module missing (e.g. partial copy) -> behave in-place
    def resolve_data_root(skill_root):
        return str(skill_root)

# Resource paths an agent may cite (backtick-wrapped). Markdown categories
# (references/templates/protocols/agents) plus runnable scripts. Each resolves
# data-root-first (user override) then skill-root (shipped core) — see
# resolve_resource_path. Keep CATEGORY_DIRS in sync so the bootstrap pre-creates
# a writable home for every category.
RESOURCE_RE = re.compile(
    r"`((?:references|templates|protocols|agents)/[\w./-]+\.md"
    r"|scripts/[\w./-]+\.(?:py|sh))`"
)

# Per-category writable dirs under the data root (mirrors the shipped core layout).
CATEGORY_DIRS = ("agents", "scripts", "references", "templates", "protocols")


def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


# --- frontmatter + agent loading ------------------------------------------

def parse_frontmatter(text):
    """Return (frontmatter_dict, body) for an agent .md file."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    fm = {}
    for line in raw.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip()
    return fm, body


def load_agents(skill_root, data_root=None):
    """Load every agent file (except INDEX.md) into a list of dicts.

    Built-in agents live in <skill_root>/agents (shipped, read-only in a plugin).
    User-created agents live in <data_root>/agents and are merged on top — a user
    file with the same slug overrides the built-in. In-place mode (data_root == the
    skill root, or None) just scans the one directory."""
    dirs = [os.path.join(skill_root, "agents")]
    if data_root and os.path.abspath(data_root) != os.path.abspath(skill_root):
        dirs.append(os.path.join(data_root, "agents"))
    if not any(os.path.isdir(d) for d in dirs):
        die(f"No agents/ directory at {dirs[0]}")
    by_slug, order = {}, []
    for d in dirs:                      # built-in first, then user (overrides)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".md") or fn == "INDEX.md":
                continue
            path = os.path.join(d, fn)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            fm, body = parse_frontmatter(text)
            slug = fm.get("name") or fn[:-3]
            if slug not in by_slug:
                order.append(slug)
            by_slug[slug] = {
                "slug": slug,
                "filename": fn,
                "path": path,
                "emoji": fm.get("emoji", ""),
                "description": fm.get("description", ""),
                "triggers": fm.get("triggers", ""),
                "body": body,
            }
    return [by_slug[s] for s in order]


# --- resolution ------------------------------------------------------------

def resolve_agent(agents, term):
    """Resolve a term to one agent. Returns (agent, candidates).

    Exact slug/filename wins. Otherwise score token overlap against
    name+triggers+description; a unique top score wins, ties return candidates,
    no overlap returns (None, [])."""
    t = term.strip().lower()
    for a in agents:
        if t == a["slug"].lower() or t == a["filename"].lower() or t == a["filename"][:-3].lower():
            return a, [a]

    tokens = [tok for tok in re.split(r"[^\w]+", t) if tok]
    scored = []
    for a in agents:
        hay = " ".join([a["slug"], a["triggers"], a["description"]]).lower()
        haytokens = set(re.split(r"[^\w]+", hay))
        score = sum(1 for tok in tokens if tok in haytokens or tok in hay)
        if score:
            scored.append((score, a))
    if not scored:
        return None, []
    scored.sort(key=lambda s: s[0], reverse=True)
    top = scored[0][0]
    winners = [a for sc, a in scored if sc == top]
    if len(winners) == 1:
        return winners[0], winners
    return None, winners


# --- memory recall ---------------------------------------------------------

def recall_memory(skill_root, data_root, slug, query_terms, no_reinforce):
    """Run `memory.py brief --agent <slug> --query ...` and return parsed JSON.

    Reinforces by default: surfacing memories for an agent is co-use, so the
    edges wire and each record's staleness clock resets, stamped to the agent."""
    # The script lives under the (read-only) skill root; the STORE lives under the
    # writable data root, which we pass as --root (memory.py resolves <root>/memory/).
    cmd = [sys.executable, os.path.join(skill_root, "scripts", "memory.py"),
           "--root", data_root,
           "--agent", slug, "brief"]
    if query_terms:
        cmd += ["--query", *query_terms]
    if no_reinforce:
        cmd.append("--no-reinforce")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", timeout=30)
    except Exception as e:  # noqa: BLE001
        return {"error": f"memory recall failed: {e}"}
    if res.returncode != 0:
        return {"error": (res.stderr or res.stdout or "memory recall failed").strip()}
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return {"error": "memory recall returned non-JSON", "raw": res.stdout.strip()}


# --- resources -------------------------------------------------------------

def parse_skill_resources(root):
    """Map resource-path -> (when_to_load, what_it_contains) from the SKILL.md table."""
    path = os.path.join(root, "SKILL.md")
    table = {}
    if not os.path.isfile(path):
        return table
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            m = re.search(r"`([^`]+)`", cells[0])
            if not m:
                continue
            table[m.group(1)] = (cells[1], cells[2])
    return table


def extract_scripts_section(body):
    """Return the agent's '## Scripts' section text, if present."""
    m = re.search(r"\n## Scripts\b.*?(?=\n## |\Z)", body, re.DOTALL)
    return m.group(0).strip() if m else ""


def resolve_resource_path(rel, skill_root, data_root):
    """Absolute path for a cited resource, USER DATA overriding shipped CORE.

    Tries <data_root>/rel first (a user-created reference/template/protocol/agent/
    script), then <skill_root>/rel (the shipped core). Returns the first that
    exists; if neither does, returns the core path as a best-guess so the boot
    package still shows a runnable absolute path. Emitting absolute paths is what
    makes the package work from any cwd — essential in a plugin, where the model's
    working directory is the user's project, not the install dir."""
    if data_root and os.path.abspath(data_root) != os.path.abspath(skill_root):
        cand = os.path.join(data_root, rel)
        if os.path.exists(cand):
            return cand
    return os.path.join(skill_root, rel)


def collect_resources(agent, skill_table, skill_root, data_root, exclude_text=""):
    """Auto-extract referenced resources: paths cited in the agent body, resolved
    to absolute paths (user data overriding core) and enriched with the SKILL.md
    'when/what' descriptions where available. Paths already shown in `exclude_text`
    (e.g. the Scripts section) are skipped to avoid duplication."""
    resources = []
    seen = set()
    for m in RESOURCE_RE.finditer(agent["body"]):
        p = m.group(1)
        if p in seen or p in exclude_text:
            continue
        seen.add(p)
        when, what = skill_table.get(p, ("", ""))
        abspath = resolve_resource_path(p, skill_root, data_root)
        resources.append({"path": p, "abspath": abspath, "when": when, "what": what})
    return resources


# --- rendering -------------------------------------------------------------

def render_markdown(agent, memory, resources, scripts_section, query_terms):
    L = []
    emoji = agent["emoji"] or "🧙🏾‍♂️"
    L.append(f"# Summoned: {emoji} {agent['slug']}")
    L.append("")
    L.append(f"> **{agent['description']}**" if agent["description"] else "")
    L.append("")
    L.append("You are now this agent. Adopt its emoji as your response prefix, follow its "
             "INSTRUCTIONS as your procedure and its GUIDELINES as your constraints, and use "
             "its FORMAT if present. Professor Synapse steps back until the task is done.")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Persona & Instructions")
    L.append("")
    L.append(agent["body"].strip())
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Recalled context")
    L.append("")
    if query_terms:
        L.append(f"*Recalled for `{agent['slug']}` on: {' '.join(query_terms)}*")
        L.append("")
    if memory.get("error"):
        L.append(f"_Memory unavailable: {memory['error']}_")
    else:
        L.append("Reason over this — don't just echo it. Read each hit's `why` "
                 "(`matches` = direct, `due date reached` = a reminder, `linked to a match` = "
                 "associative context from the graph, `recent (no query match)` = surfaced by "
                 "recency because nothing matched the query). Honour any `constraints` before "
                 "acting and calibrate trust by `confidence`.")
        L.append("")
        L.append("```json")
        L.append(json.dumps(memory, indent=2, ensure_ascii=False))
        L.append("```")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Resources you can load")
    L.append("")
    if scripts_section:
        L.append(scripts_section)
        L.append("")
    if resources:
        L.append("Referenced by this agent — open with the file/`view` tool, run scripts with "
                 "`python3`/`bash`. Paths are absolute (resolved user-data-first, then core) so "
                 "they work from any directory:")
        L.append("")
        L.append("| Resource | When to load | What it contains |")
        L.append("|----------|--------------|------------------|")
        for r in resources:
            L.append(f"| `{r['abspath']}` | {r['when']} | {r['what']} |")
        L.append("")
    if not scripts_section and not resources:
        L.append("_This agent cites no external resources; work from the persona above._")
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def render_no_match(term, agents, candidates):
    if candidates:
        lines = [f"# No single match for '{term}'", "",
                 "Multiple agents could fit. Re-run `summon.py` with one of these slugs:", ""]
        for a in candidates:
            lines.append(f"- `{a['slug']}` {a['emoji']} — {a['description']}")
        return "\n".join(lines) + "\n"
    lines = [f"# No agent matches '{term}'", "",
             "No existing agent fits. Either answer directly if a general response suffices, "
             "or create a reusable agent: load `references/agent-template.md` and "
             "`references/domain-expertise.md`, then save the new agent into your data "
             "agents dir (see SKILL.md); it takes effect on the next summon.", "",
             "Existing agents:", ""]
    for a in agents:
        lines.append(f"- `{a['slug']}` {a['emoji']} — {a['description']}")
    return "\n".join(lines) + "\n"


def render_list(agents):
    """Print the merged agent roster (built-in + user) as a routing table. This is
    the canonical 'what agents exist' view — always current, no static index to go
    stale. Route by re-running summon.py with a slug or a task phrase."""
    lines = ["# Professor Synapse — available agents", "",
             "Summon one with `summon.py \"<slug or task phrase>\"`.", "",
             "| Agent | Emoji | Description | Triggers |",
             "|-------|-------|-------------|----------|"]
    for a in agents:
        lines.append(f"| `{a['slug']}` | {a['emoji']} | {a['description']} | {a['triggers']} |")
    return "\n".join(lines) + "\n"


# --- summon marker (PreToolUse summon-gate integration) --------------------

def write_summon_marker(data_root, kind, label, query_terms):
    """Record that a summon (or an explicit no-agent decision) happened this
    session so the PreToolUse summon-gate hook lets task-action tools through.

    The marker lives under the writable data root (<data_root>/.summon-state/), the
    same place the summon-gate hook reads it from — so the two agree regardless of
    plugin vs in-place mode.

    Best-effort: never fail the summon if the marker cannot be written. The
    hook also has a transcript-scan fallback, so a missing marker is recoverable.
    """
    try:
        state_dir = os.path.join(data_root, ".summon-state")
        os.makedirs(state_dir, exist_ok=True)
        # Tools name the session env var differently; Codex exposes none, so the
        # gate also accepts a recent shared "nosession" marker (see summon-gate.py).
        session = (os.environ.get("CLAUDE_CODE_SESSION_ID")
                   or os.environ.get("CODEX_SESSION_ID")
                   or os.environ.get("AGENT_SESSION_ID")
                   or "nosession")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", session)
        path = os.path.join(state_dir, f"summon-{safe}.json")
        # Merge with any existing marker for this session so we keep the full set
        # of agents summoned. The summon-gate's memory-write check looks for
        # "memory-agent" in this list before allowing a memory.py write.
        agents = []
        try:
            with open(path, encoding="utf-8") as fh:
                agents = list(json.load(fh).get("agents") or [])
        except Exception:
            agents = []
        if kind == "agent" and label and label not in agents:
            agents.append(label)
        payload = {
            "session": session,
            "kind": kind,            # "agent" (summoned a specialist) or "self" (no agent fits)
            "label": label,          # agent slug, or the short reason for a self-route
            "agents": agents,        # every agent slug summoned this session (for the memory-write gate)
            "query": query_terms or [],
            "ts": datetime.datetime.now().astimezone().isoformat(),
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


# --- main ------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Summon an agent: assemble a boot package (persona + recalled memory + resources).")
    ap.add_argument("agent", nargs="?", default=None, help="agent slug or a phrase to match")
    ap.add_argument("--query", nargs="*", default=None, help="task terms to recall (defaults to the agent's triggers)")
    ap.add_argument("--no-reinforce", action="store_true", help="read-only recall: don't wire or reset staleness")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    ap.add_argument("--self", dest="self_route", action="store_true",
                    help="record an explicit 'no specialized agent fits; proceeding as Professor' decision (lifts the summon-gate)")
    ap.add_argument("--reason", default=None, help="why no agent fits, used with --self")
    ap.add_argument("--list", dest="list_agents", action="store_true",
                    help="print the merged roster (built-in + user agents) and exit")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="skill root directory (read-only core)")
    args = ap.parse_args(argv)

    # skill_root = read-only core (this script, agents/, references/, SKILL.md).
    # data_root  = writable store (user agents, memory/, the summon marker). Same as
    # skill_root when not running as a plugin (portable / dev), so behavior is identical.
    skill_root = os.path.abspath(args.root)
    data_root = os.path.abspath(resolve_data_root(skill_root))

    # Escape hatch: no agent owns the task. Record the explicit decision
    # (the "say so and proceed" rule, made into a logged action) and stop.
    if args.self_route:
        reason = args.reason or (" ".join(args.query) if args.query else
                                 "no matching agent; proceeding as Professor Synapse")
        write_summon_marker(data_root, "self", reason[:200], args.query)
        print("Recorded: proceeding without a specialized agent.")
        print(f"Reason: {reason}")
        print("The summon-gate is satisfied for this session; task-action tools are unblocked.")
        return

    if args.list_agents:
        sys.stdout.write(render_list(load_agents(skill_root, data_root)))
        return

    if not args.agent:
        die("usage: summon.py <agent> [--query ...]  |  summon.py --list  |  summon.py --self --reason \"why none fits\"")

    agents = load_agents(skill_root, data_root)
    if not agents:
        die("No agents found.")

    agent, candidates = resolve_agent(agents, args.agent)
    if agent is None:
        out = render_no_match(args.agent, agents, candidates)
        if args.json:
            print(json.dumps({"matched": False, "term": args.agent,
                              "candidates": [a["slug"] for a in candidates],
                              "agents": [a["slug"] for a in agents]}, indent=2))
        else:
            sys.stdout.write(out)
        sys.exit(0 if candidates else 3)

    # Query defaults to the agent's triggers so a bare summon still recalls context.
    if args.query is not None:
        query_terms = args.query
    else:
        query_terms = [t.strip() for t in agent["triggers"].split(",") if t.strip()]

    # Record the summon so the PreToolUse summon-gate unblocks task-action tools.
    write_summon_marker(data_root, "agent", agent["slug"], query_terms)

    memory = recall_memory(skill_root, data_root, agent["slug"], query_terms, args.no_reinforce)
    skill_table = parse_skill_resources(skill_root)
    scripts_section = extract_scripts_section(agent["body"])
    resources = collect_resources(agent, skill_table, skill_root, data_root, exclude_text=scripts_section)

    if args.json:
        print(json.dumps({
            "matched": True,
            "agent": {k: agent[k] for k in ("slug", "filename", "emoji", "description", "triggers")},
            "persona": agent["body"].strip(),
            "query": query_terms,
            "memory": memory,
            "resources": resources,
            "scripts_section": scripts_section,
        }, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render_markdown(agent, memory, resources, scripts_section, query_terms))


if __name__ == "__main__":
    main()
