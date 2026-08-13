#!/usr/bin/env python3
"""Programmatic agent summoning for Professor Synapse.

Assembles a single "boot package" that contains everything needed to *become*
an agent: its full persona/instructions, the memory recalled for it, and the
resources it can load (with how to call them). stdout IS the summon — read it,
then become whoever it hands you.

Usage:
    python3 scripts/summon.py <agent> [--query TERMS ...] [--no-reinforce] [--json]

  <agent>            agent slug (e.g. memory-agent) or a task phrase. A phrase
                     summons an agent only when it contains one of that agent's
                     COMPLETE trigger phrases; otherwise the script abstains and
                     lists scored near-misses instead of guessing. Pass an exact
                     slug to bypass matching.
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
import math
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


# --- routing confidence tuning ---------------------------------------------
# Auto-summoning is deliberately STRICT: only an exact slug or a *complete*
# trigger phrase adopts an agent. Everything else abstains and suggests.
#
# Why not a confidence threshold? Because coverage scoring cannot separate a
# real short match from a coincidental one. Measured on labelled cases,
# "format this week's team meeting agenda" (legitimate) and "build a lesson
# tracker doc" (a fragment mis-route) both score 0.50 — any floor that rejects
# the second rejects the first. Complete-trigger matching separates them
# cleanly, so that is what gates the summon; the score only ranks suggestions.

# English function words only. Deliberately NOT domain verbs like "create" or
# "build" — those appear in real triggers ("create agent") and must stay
# matchable. Used for the suggestion score only; trigger matching runs on raw
# words so multi-word triggers containing stopwords ("forget this", "what do
# you remember") still fire.
STOPWORDS = {
    "a", "about", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "can", "could", "did", "do", "does", "for", "from", "how", "i", "in", "is",
    "it", "let", "me", "my", "need", "of", "on", "or", "our", "please",
    "should", "that", "the", "these", "this", "those", "to", "want", "was",
    "we", "what", "when", "who", "will", "with", "would", "you", "your",
}
SUGGEST_FLOOR = 0.10   # candidates scoring below this aren't worth listing
SUGGEST_MAX = 3        # cap the suggestion list


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

def _words(text):
    """Every word in text, lowercased. No stopword filtering."""
    return {w for w in re.split(r"[^\w]+", text.lower()) if w}


def _content_tokens(text):
    """Query words worth scoring: lowercased, stopwords and single chars dropped."""
    return [w for w in re.split(r"[^\w]+", text.lower())
            if w and w not in STOPWORDS and len(w) > 1]


def _agent_words(agent):
    """The agent's whole matchable vocabulary: slug + triggers + description."""
    return _words(" ".join([agent["slug"], agent["triggers"], agent["description"]]))


def fired_triggers(agent, query_words):
    """Trigger phrases fully present in the query.

    A multi-word trigger fires only when EVERY one of its words is in the
    query — "agenda tracker" does not fire on "tracker" alone. This is the
    signal that gates auto-summoning, so it is matched against raw query
    words: stripping stopwords first would stop "forget this" from ever
    firing.
    """
    out = []
    for phrase in agent["triggers"].split(","):
        phrase = phrase.strip()
        if phrase and _words(phrase) <= query_words:
            out.append(phrase)
    return out


def score_agents(agents, term):
    """Rank agents by how much of the query's *meaningful* content they cover.

    Used only to order suggestions when no agent auto-summons — it never
    adopts an agent on its own. Each matched word is weighted by IDF over the
    agent roster (the roster is the corpus), so a word appearing in every
    agent's blurb counts for little and a rare one counts for a lot. Words in
    no agent at all keep their full weight in the denominator: an unrecognised
    word is evidence the task is out of scope, not something to ignore.

    Returns [{"agent", "score", "matched"}], best first.
    """
    query = _content_tokens(term)
    if not query:
        return []
    vocab = {a["slug"]: _agent_words(a) for a in agents}
    n = len(agents)
    # log1p-style smoothing, not the textbook log(N/df): with a single-agent
    # roster every df equals N, log(N/df) is 0 for every term, and the
    # denominator collapses to zero.
    idf = {}
    for tok in set(query):
        df = sum(1 for a in agents if tok in vocab[a["slug"]])
        idf[tok] = math.log(1 + n / df) if df else math.log(1 + n)
    denom = sum(idf[tok] for tok in query)
    ranked = []
    for a in agents:
        matched = [tok for tok in query if tok in vocab[a["slug"]]]
        score = sum(idf[tok] for tok in matched) / denom if denom else 0.0
        ranked.append({"agent": a, "score": score, "matched": matched})
    ranked.sort(key=lambda r: (-r["score"], r["agent"]["slug"]))
    return ranked


def suggestions(agents, term):
    """The candidates worth showing when nothing auto-summons."""
    return [r for r in score_agents(agents, term)
            if r["score"] >= SUGGEST_FLOOR][:SUGGEST_MAX]


def resolve_agent(agents, term):
    """Resolve a term to one agent. Returns (agent, candidates, why).

    Strict by design — an agent is adopted only on a definite signal:

      1. the term IS an agent slug/filename, or
      2. exactly one agent has a COMPLETE trigger phrase in the term.

    Anything else abstains, because a partial keyword overlap is
    indistinguishable from a coincidence and a wrongly-adopted agent runs the
    wrong procedure under a header that looks entirely correct.

    `candidates` are scored dicts (see score_agents); `why` explains the
    outcome and is surfaced to the caller.
    """
    t = term.strip().lower()
    for a in agents:
        if t == a["slug"].lower() or t == a["filename"].lower() or t == a["filename"][:-3].lower():
            return a, [{"agent": a, "score": 1.0, "matched": []}], "exact slug"

    query_words = _words(term)
    fired = [(a, fired_triggers(a, query_words)) for a in agents]
    fired = [(a, f) for a, f in fired if f]
    if len(fired) == 1:
        a, phrases = fired[0]
        why = "trigger fired: " + ", ".join(repr(p) for p in phrases)
        return a, [{"agent": a, "score": 1.0, "matched": phrases}], why
    if len(fired) > 1:
        cands = [{"agent": a, "score": 1.0, "matched": f} for a, f in fired]
        return None, cands, "multiple agents matched a full trigger"

    cands = suggestions(agents, term)
    if not cands:
        return None, [], "nothing in the task matches any agent"
    return None, cands, "no agent matched a full trigger — keyword overlap only"


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

def render_markdown(agent, memory, resources, scripts_section, query_terms, why=""):
    L = []
    emoji = agent["emoji"] or "🧙🏾‍♂️"
    L.append(f"# Summoned: {emoji} {agent['slug']}")
    L.append("")
    # Say WHY this agent was picked. A summon header that looks identical
    # whether the match was exact or coincidental is how a mis-route goes
    # unnoticed; "exact slug" needs no explanation, anything else does.
    if why and why != "exact slug":
        L.append(f"*Matched by: {why}.*")
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


def render_no_match(term, agents, candidates, why=""):
    """Explain the abstention and show the near-misses with their scores.

    No agent has been adopted here. The point is to make a weak match VISIBLE
    rather than silently act on it, so the scores are shown and the ways
    forward are spelled out.
    """
    short = term if len(term) <= 60 else term[:57] + "..."
    if candidates:
        lines = [f"# No confident match for '{short}'", ""]
        if why:
            lines += [f"{why[0].upper()}{why[1:]}.", ""]
        lines += ["These agents came closest. **None was summoned** — a partial keyword "
                  "overlap is not evidence of a fit.", "",
                  "| Agent | Score | Matched on |",
                  "|-------|-------|------------|"]
        for c in candidates:
            a = c["agent"]
            hit = ", ".join(f"`{m}`" for m in c["matched"]) or "—"
            lines.append(f"| `{a['slug']}` {a['emoji']} | {c['score']:.2f} | {hit} |")
        lines += ["", "Pick a way forward:", "",
                  "- **One of these is genuinely right** — re-run `summon.py` with its exact "
                  "slug, which bypasses matching entirely.",
                  "- **It should have matched** — add the phrase to that agent's `triggers:` "
                  "frontmatter so it fires next time.",
                  "- **No agent owns this** — answer directly, or hand off to the skill that "
                  "does own it.", ""]
        return "\n".join(lines).rstrip() + "\n"
    lines = [f"# No agent matches '{short}'", ""]
    if why:
        lines += [f"{why[0].upper()}{why[1:]}.", ""]
    lines += ["No existing agent fits. Either answer directly if a general response suffices, "
              "or create a reusable agent: load `references/agent-template.md` and "
              "`references/domain-expertise.md`, then follow the packaging workflow.", "",
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

    agent, candidates, why = resolve_agent(agents, args.agent)
    if agent is None:
        out = render_no_match(args.agent, agents, candidates, why)
        if args.json:
            print(json.dumps({
                "matched": False, "term": args.agent, "reason": why,
                "candidates": [{"slug": c["agent"]["slug"],
                                "score": round(c["score"], 3),
                                "matched": c["matched"]} for c in candidates],
                "agents": [a["slug"] for a in agents],
            }, indent=2, ensure_ascii=False))
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
            "reason": why,
            "agent": {k: agent[k] for k in ("slug", "filename", "emoji", "description", "triggers")},
            "persona": agent["body"].strip(),
            "query": query_terms,
            "memory": memory,
            "resources": resources,
            "scripts_section": scripts_section,
        }, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render_markdown(agent, memory, resources, scripts_section,
                                         query_terms, why))


if __name__ == "__main__":
    main()
