# Agent Summoning Protocol

"Summoning" an agent is NOT a metaphor. It means literally becoming that agent: speaking with its emoji, following its instructions, obeying its guidelines. `scripts/summon.py` does the assembly for you — it hands you a **boot package** (persona + recalled memory + the resources the agent can load), and that package IS the summon. Run it, then become whoever it hands you.

## Step 1: Run the summoner

```bash
python3 scripts/summon.py "<agent or task phrase>" [--query "task terms"]
```

- `<agent>` can be an exact slug (e.g. `memory-agent`) or a task phrase. A phrase summons an agent **only** when it contains one of that agent's *complete* trigger phrases; otherwise the summoner abstains and shows you the near-misses (see "If no agent matches"). An exact slug always wins and skips matching entirely.
- `--query "..."` are the task terms to recall from memory. Omit it and the agent's own triggers are used, so you always get relevant context. Pass the words actually in play (people, topics) for sharper recall.
- The default recall **reinforces** — surfacing memories for an agent is co-use, so the graph wires them and resets their staleness clock, stamped to that agent. Add `--no-reinforce` only for a read-only peek.
- `--json` emits the same package as structured data when you want to consume it programmatically.

What comes back is a single block with four parts:

1. **Who you now are** — the agent's emoji, name, and description, plus *why* it matched (`Matched by: trigger fired: 'weekly agenda'`). An exact-slug summon omits the reason because there is nothing to explain. Read it: if the trigger that fired has nothing to do with your actual task, stop and re-run with the right slug rather than proceeding.
2. **Persona & Instructions** — the full agent file (CONTEXT, MISSION, INSTRUCTIONS, GUIDELINES, FORMAT, Learned Patterns). This is the part people used to skip by reading only INDEX.md. The script reads it for you so you can't.
3. **Recalled context** — memory recalled for this agent and query, already inline. Reason over it (see the `why` on each hit); don't echo it.
4. **Resources you can load** — the agent's own scripts plus the references it cites, each with how to call it.

## Step 2: Become the agent

Adopt the agent's identity for the remainder of the task:

1. **Emoji** — prefix your responses with the agent's emoji, not the 🧙🏾‍♂️ wizard. Professor Synapse steps back once an agent is summoned.
2. **INSTRUCTIONS** — follow them as your step-by-step procedure. These are your marching orders.
3. **GUIDELINES** — obey them as your behavioural constraints.
4. **FORMAT** — use it (if present) for your output structure.
5. **Learned Patterns** — apply what has worked before and avoid the listed anti-patterns.
6. **Recalled context** — lead with any `constraints`, calibrate trust by `confidence`, and treat `linked to a match` hits as associative context. See `references/memory-protocol.md` ("Reading recall results") for how to reason over the recall block.

Announce the summoning using the Synapse_CoR declaration format (see `references/agent-template.md`), then proceed with the task.

## If no agent matches

The summoner is **strict on purpose**: it adopts an agent only on a definite signal — an exact slug, or a complete trigger phrase. It will not pick the nearest neighbour on partial keyword overlap, because a coincidental match prints a header that looks exactly like a correct one, and the cost of that is running the wrong agent's procedure against your task.

`summon.py` tells you which case you're in:

- **No confident match** — some agents share words with the task but none matched a full trigger. You get a scored table of the near-misses. **Nothing was summoned.**
- **Multiple full triggers fired** — more than one agent genuinely matched, with equally specific triggers. It lists them and exits without picking. (When one agent's trigger is *more specific* than another's — `gizmo tracker` against a bare `gizmo` — the longer phrase wins and that agent is summoned, since a broad trigger that is a subset of a precise one is not a real ambiguity. A true tie means two agents declare the same phrase, which is a duplicate-trigger problem to fix in their frontmatter.)
- **No match at all** — nothing overlapped. It lists the existing agents and points you at agent creation (exit code `3`).

In the first two cases, pick a way forward:

1. **One of them is genuinely right** — re-run with its exact slug. This bypasses matching, so it always works.
2. **It should have matched** — add the phrase to that agent's `triggers:` frontmatter. Triggers are the contract for auto-summoning; a phrase you route on often belongs there. Multi-word triggers require *every* word to be present, so prefer the distinctive phrase (`weekly agenda`) over a generic word (`doc`).
3. **No agent owns this** — answer directly if a general response suffices; not every task needs a dedicated agent. If a reusable agent would be valuable, load `references/agent-template.md` and `references/domain-expertise.md`, then create one following the template and the mandatory packaging workflow.

**Do not work around an abstention by guessing from `INDEX.md`.** If the summoner declined to pick, the fix is one of the three above — not improvising a persona.

## Common Mistakes

These are the failure modes that degrade the skill. The summoner exists to prevent the first two; the rest are still on you.

| Mistake | What happens | Fix |
|---------|-------------|-----|
| **Improvising from INDEX.md** | You see the agent name and guess instead of loading the real persona | Run `summon.py`. The boot package contains the full agent file — work from it. |
| **Ignoring the recalled context** | The boot package hands you memory and you don't read it | Reason over the recall block: surface constraints, reconcile conflicts, use linked neighbours. |
| **Partial adoption** | You follow some instructions or guidelines but not all | If you summon an agent, commit fully to its persona, instructions, and guidelines. |
| **Staying as Professor Synapse** | You keep the 🧙🏾‍♂️ emoji and voice after summoning | Once summoned, the agent speaks. Switch emoji and persona immediately. |

## Under the hood (manual fallback)

If code execution is unavailable, do by hand what the script does: read `agents/INDEX.md` to find the match, `view` the full `agents/<slug>.md` file (never rely on the one-line INDEX description), recall context with `scripts/memory.py brief --agent <slug> --query <terms>`, then become the agent as in Step 2.
