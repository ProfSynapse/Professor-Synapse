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
import json
import math
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(SCRIPT_DIR)

RESOURCE_RE = re.compile(r"`(references/[\w./-]+\.md|scripts/[\w./-]+\.(?:py|sh)|agents/[\w./-]+\.md)`")

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
        # Several agents fired. Prefer the most SPECIFIC match: a longer
        # complete trigger phrase is stronger evidence of intent than a
        # shorter one. "blog image" beats a bare "blog", so an agent whose
        # broad trigger is a subset of another's precise one doesn't force a
        # disambiguation prompt on every phrase.
        def specificity(pair):
            return max(len(_words(p)) for p in pair[1])
        top = max(specificity(p) for p in fired)
        winners = [p for p in fired if specificity(p) == top]
        if len(winners) == 1:
            a, phrases = winners[0]
            best = max(phrases, key=lambda p: len(_words(p)))
            why = f"trigger fired: {best!r} (most specific of {len(fired)} agents)"
            return a, [{"agent": a, "score": 1.0, "matched": phrases}], why
        # A real tie — two agents declare an equally specific trigger. No
        # ranking can separate them; that's a duplicate-trigger problem in the
        # agent frontmatter, so surface it rather than guessing.
        cands = [{"agent": a, "score": 1.0, "matched": f} for a, f in winners]
        return None, cands, "multiple agents declare an equally specific trigger"

    cands = suggestions(agents, term)
    if not cands:
        return None, [], "nothing in the task matches any agent"
    return None, cands, "no agent matched a full trigger — keyword overlap only"


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

    memory = recall_memory(root, agent["slug"], query_terms, args.no_reinforce)
    skill_table = parse_skill_resources(root)
    scripts_section = extract_scripts_section(agent["body"])
    resources = collect_resources(agent, skill_table, exclude_text=scripts_section)

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
