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
import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(SCRIPT_DIR)

RESOURCE_RE = re.compile(r"`(references/[\w./-]+\.md|scripts/[\w./-]+\.(?:py|sh)|agents/[\w./-]+\.md)`")


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


def load_agents(root):
    """Load every agent file (except INDEX.md) into a list of dicts."""
    agents_dir = os.path.join(root, "agents")
    if not os.path.isdir(agents_dir):
        die(f"No agents/ directory at {agents_dir}")
    out = []
    for fn in sorted(os.listdir(agents_dir)):
        if not fn.endswith(".md") or fn == "INDEX.md":
            continue
        path = os.path.join(agents_dir, fn)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        fm, body = parse_frontmatter(text)
        out.append({
            "slug": fm.get("name") or fn[:-3],
            "filename": fn,
            "path": path,
            "emoji": fm.get("emoji", ""),
            "description": fm.get("description", ""),
            "triggers": fm.get("triggers", ""),
            "body": body,
        })
    return out


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

def recall_memory(root, slug, query_terms, no_reinforce):
    """Run `memory.py brief --agent <slug> --query ...` and return parsed JSON.

    Reinforces by default: surfacing memories for an agent is co-use, so the
    edges wire and each record's staleness clock resets, stamped to the agent."""
    # memory.py treats --root as the skill root and resolves <root>/memory/ itself.
    cmd = [sys.executable, os.path.join(root, "scripts", "memory.py"),
           "--root", root,
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


def collect_resources(agent, skill_table, exclude_text=""):
    """Auto-extract referenced resources: paths cited in the agent body, enriched
    with the SKILL.md 'when/what' descriptions where available. Paths already shown
    in `exclude_text` (e.g. the Scripts section) are skipped to avoid duplication."""
    resources = []
    seen = set()
    for m in RESOURCE_RE.finditer(agent["body"]):
        p = m.group(1)
        if p in seen or p in exclude_text:
            continue
        seen.add(p)
        when, what = skill_table.get(p, ("", ""))
        resources.append({"path": p, "when": when, "what": what})
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
        L.append("Referenced by this agent — load with the `view` tool, run scripts with `python3`/`bash`:")
        L.append("")
        L.append("| Resource | When to load | What it contains |")
        L.append("|----------|--------------|------------------|")
        for r in resources:
            L.append(f"| `{r['path']}` | {r['when']} | {r['what']} |")
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
             "`references/domain-expertise.md`, then save it under agents/ and rebuild the index.", "",
             "Existing agents:", ""]
    for a in agents:
        lines.append(f"- `{a['slug']}` {a['emoji']} — {a['description']}")
    return "\n".join(lines) + "\n"


# --- main ------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Summon an agent: assemble a boot package (persona + recalled memory + resources).")
    ap.add_argument("agent", help="agent slug or a phrase to match")
    ap.add_argument("--query", nargs="*", default=None, help="task terms to recall (defaults to the agent's triggers)")
    ap.add_argument("--no-reinforce", action="store_true", help="read-only recall: don't wire or reset staleness")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="skill root directory")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    agents = load_agents(root)
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

    memory = recall_memory(root, agent["slug"], query_terms, args.no_reinforce)
    skill_table = parse_skill_resources(root)
    scripts_section = extract_scripts_section(agent["body"])
    resources = collect_resources(agent, skill_table, exclude_text=scripts_section)

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
