# Memory Protocol

How Professor Synapse remembers across sessions. Memory is shared by all agents and tagged with the agent that created each entry, so it can be filtered by agent. Everything goes through `scripts/memory.py`; never hand-edit the store. Run `python3 scripts/memory.py --help` for verbs. For the field-by-field schema and how to version it, see `references/memory-data-model.md`.

## The store

Two files under `memory/`:

- `memory/memory.json` - working memory: a shared profile plus active items. Human-readable, hot, read often.
- `memory/longterm.db` - SQLite long-term store: archived items, decisions, notes, and facts, plus a change log. Queryable, cold.

Every active item, long-term record, and log entry carries an `agent` field. Always pass `--agent <slug>` for the agent currently acting, using the agent's `name` from its frontmatter (for example `--agent chief-of-staff`). Professor Synapse's own slug is `professor-synapse`.

## The rhythm

Memory is a loop, not a lookup: **recall → reason → act → capture → maintain → persist.** Pull context before you work, *reason* over what comes back (don't just echo it), do the work, capture what's new and durable, run the janitor at save time, and rebuild once to persist. The sections below are that loop in order.

## Recall (start of work)

**Fast path:** `python3 scripts/memory.py --agent <slug> brief` is the one-shot prefetch — it returns the shared profile, the agent's active items, and any long-term items now due in a single call. Add `--query <terms>` once you know the people and topics in play and it folds ranked keyword matches in (`matches`) too. With **no query** (or a query that matches nothing), it instead folds in a `recent` set — the agent's most relevant recent long-term records, ranked by recency with the same kind/confidence nudges — so a bare prefetch still hands back substantive memory instead of nothing. Start here; reach for the individual verbs below when you want a narrower view.

If you'd rather surface things step by step:

1. `python3 scripts/memory.py --agent <slug> read` for that agent's active items, and `read` with no agent for the shared picture.
2. `python3 scripts/memory.py --agent <slug> recall` for long-term items whose due date has arrived.
3. Once you know the people and topics in play, `python3 scripts/memory.py recall --query <terms>` (add `--agent <slug>` to scope to one agent, omit it to search across all agents). `--query` is ranked full-text search (SQLite FTS5: stemming + prefix matching, best matches first), so terms like `renew` also surface `renewal`/`renewals`. The shared profile and other agents' facts can be relevant, so search broadly, then scope when you want one agent's view. `recall` with **no query** (or one that matches nothing) falls back to the recency-ranked recent set rather than returning empty, so a bare `recall --agent <slug>` enumerates that agent's most relevant recent records.

## Reading recall results (reason, don't just dump)

A recall hit is evidence to reason over, not a row to read aloud. Each carries a `why` that tells you *how* it surfaced — weigh it accordingly:

- **`matches <terms>`** — the record's own `text`/`tags`/`goal`/`constraints` matched your query. Direct, keyword-level relevance.
- **`due date reached`** — surfaced by time, not topic. It's a reminder, not necessarily related to what you asked.
- **`linked to a match`** — it did **not** match your query; it surfaced because it's wired (co-recalled before) to something that did. Treat it as **associative context** — "you'll probably also want this" — not as a keyword answer. This is often the most useful signal: it's how the graph hands you the rest of a cluster.

Read the whole record, not just `text`:

- **`constraints`** are gotchas to honour *before* you act — surface them proactively ("note: the API needs the featured image uploaded first").
- **`goal`/`outcome`** on a `lesson` tell you what it was for and whether it worked — reuse the approach, but check the constraints still hold before repeating it.
- **`confidence`** calibrates trust. Lean on `high`; treat `low` (especially if old) as a hypothesis to re-verify, and say so rather than asserting it. Its *basis* travels with it: **`source`** is the evidence held (where the belief comes from), **`verify`** is the upgrade path (what evidence is available and how to get it), and **`unknowns`** are the gaps. On a shaky hit, surface the path proactively — "I believe X (low confidence — only mentioned once; I can confirm via the HubSpot record; unknown whether it's still current)" — and offer to run down `verify` when it matters. Once you do, record the outcome with `reconfirm` (see "Closing the verify loop") so the level and its evidence move together.
- **`kind`** frames the record: a `decision` comes with a `rationale`; a `fact` is a durable truth; a `lesson` is a how-to to reuse; a `note` is background.

**Synthesize across hits.** When a direct match arrives with its `linked to a match` neighbours, that's a topic cluster — reason over it as a whole, lead with the constraints, and reconcile any conflicts (a newer or higher-confidence record beats an older or lower one; if they genuinely disagree, say so). Recall is associative + keyword search, not ground truth — don't over-trust a lone low-confidence or stale hit.

## Capture (during and after work)

- Working items: `add --agent <slug> --text "..." [--type ...] [--people ...] [--tags ...] [--due ...]`. Use these for things in flight — tasks, follow-ups, anything with a `--due`.
- Things born straight into long-term: `record --agent <slug> --kind decision|note|fact|lesson --text "..."`.

**Pick the kind by asking what it is:**

| If it's… | use | and add |
|---|---|---|
| a durable truth about the user/world | `fact` | `--confidence`, and `--source` if it could go stale |
| a choice you made | `decision` | `--rationale` (why), so future-you doesn't relitigate it |
| background context worth keeping | `note` | just `--people`/`--tags` |
| a reusable how-to you figured out by doing | `lesson` | `--goal` + `--outcome` + `--constraints` |

Capture a `lesson` the moment a task succeeds — or fails (put the failure in `--outcome` and the trap in `--constraints`). A lesson without its goal/outcome/constraints is just a note; the structure is what makes it reusable.

### Richer body (optional on any item or record)

- `--goal "..."` — what this is in service of (the objective).
- `--outcome "..."` — what resulted / the current state.
- `--constraints "gotcha one" "gotcha two"` — limits and gotchas. Each is a **quoted phrase** (a constraint may itself contain a comma), not a comma-list.
- `--confidence high|medium|low` — how sure you are; nudges recall ranking. Best on `fact`s that might decay.
- **Confidence basis** — *why* it's at that level, so future-you (and the user) can trust or upgrade it:
  - `--source "..."` — the **evidence held** (where the claim comes from: "user stated directly", "observed across 3 sessions", "inferred from the invoice").
  - `--verify "..."` — the **upgrade path**: what evidence is available and how to get it ("confirm via the HubSpot contact record", "ask the user", "check the signed contract"). This is what turns a `low` into a `high` when it matters.
  - `--unknowns "..."` — what's genuinely **unknown or unknown-unknown** (can't currently be determined). Honest about the ceiling.
  - All three are full-text searchable, so a later `recall --query "how to confirm employer"` surfaces the `verify` path directly. Reach for them whenever you save something below `high` — the basis is what makes a low-confidence record *actionable* instead of just doubtful.

A `lesson` shines when it carries all four: e.g. `record --kind lesson --text "Upload a blog to HubSpot" --goal "publish a draft via API" --outcome "POST /cms/v3/blogs/posts works" --constraints "upload featured image first" "publish_date is epoch ms" --confidence high`. `goal`, `outcome`, and `constraints` are all full-text searchable, so a later `recall --query "hubspot epoch"` surfaces the gotcha directly.

Always fill `--people` and `--tags`; they power recall. Tag with the acting agent so the entry can be filtered later. Short list options accept either form — `--tags a b` or `--tags a,b` both store two distinct tags (constraints are the exception: always quote each phrase).

## When to ask vs. just save

Saving isn't free — it shapes later recall and only persists on rebuild — but asking has a cost too. Route every save with two questions:

1. Did the user *tell* you this, or did you *infer* it?
2. Does this *destroy, contradict, or expose* something?

That gives three gates:

- **Just save (silent — mention it in passing).** The user asked you to remember, *or* plainly stated a durable fact/decision, *or* it's a working item from the task. Capture and note it ("noted you're at Synaptic Labs").
- **Save low-confidence + narrate.** You *inferred* it rather than being told, and it's neither sensitive nor destructive. Write it `--confidence low` and say it's a guess. Add the basis — `--source` (what you inferred it from) and especially `--verify` (how it could be confirmed) — so the guess carries its own upgrade path. Recall treats low-confidence as re-verify-me, so a wrong guess self-corrects later instead of interrupting now — don't ask.
- **Ask first.** Destructive (`forget`/`drop`), overwriting/contradicting an existing **high-confidence** record, or **sensitive** (health, finances, third-party personal data). Stop and confirm.

Two things keep this low-friction:

- **`confidence` is the release valve.** When tempted to ask "is this right?", save it `low` instead.
- **Confirm in a batch at persist, not per item.** Nothing is permanent until the rebuild, so capture freely during work and, at save time, show what's about to persist for pruning. That's one confirmation — and where the ask-first items get their yes/no.

### Checking before you write

`check --kind <k> --text "..." [--tags ...] [--people ...]` is a **read-only probe**: does this resemble something already stored? It returns the most similar records plus two flags — `has_duplicate` (near-identical text) and `has_conflict` (a high-confidence `fact`/`decision` on the same subject you may be superseding). `record` runs the same check automatically and prints a `⚠` advisory when it fires, **without blocking the write** (the record still lands — the store is a draft until rebuild).

Use the signal to route the gates: a **duplicate** → consolidate (`link` the two, or `forget` the older) instead of stacking a near-copy; a **conflict** → that's an ask-first, confirm the update with the user before an existing high-confidence record is contradicted; **no signal** → just save.

### Amending a long-term record

Use `amend --id <id>` to correct or enrich a non-dropped long-term record without retiring it or changing its lifelong id. Pass only the fields that should change:

```
python3 scripts/memory.py --agent <slug> amend --id <id> --text "corrected text"
```

`amend` preserves graph links, refreshes `last_used`, and adds an `amend` changelog entry. Omitted fields remain unchanged. Passing `--people`, `--tags`, or `--constraints` with no values clears that list. Quote arguments according to the active shell so literal metacharacters are preserved.
## Linking & the knowledge graph

Memories can be linked into a weighted graph that feeds recall. Edges form two ways:

- **Explicit:** `link --a <id> --b <id> [--weight N]` asserts a relationship (`unlink` removes it; `links --id <id>` lists a record's neighbours by current affinity).
- **Co-use (Hebbian):** memories surfaced by the same query are treated as used together. `recall --query` and `brief --query` **reinforce by default** — they bump affinity across every pair of query matches and reset each surfaced record's staleness clock. Pass `--no-reinforce` for a purely read-only/exploratory query. `reinforce --ids <id> <id> ...` is the explicit form when you want to wire a specific set without a query. Repeated co-use strengthens the link; weights **decay** with time, so the graph keeps re-clustering around what's currently active.

At query time, `recall --query` seeds from the top text matches and spreads one hop along strong edges, so a memory wired to what you asked about surfaces even if its own text barely matches (`"why": "linked to a match"`). Because recall reinforces by default, the graph gets smarter simply by being used.

**In practice:**
- You rarely call `reinforce` by hand — just recalling things together wires them. Use explicit `reinforce --ids` only when you used a set you assembled *without* a single query (e.g. records you pulled across several searches and then acted on together).
- Use `link` for a deliberate, lasting relationship you want to assert regardless of co-use — e.g. a `lesson` ↔ the `decision` that motivated it. It's stronger and more intentional than incidental co-recall.
- Use `--no-reinforce` on broad/speculative sweeps so you don't wire together things that merely share a keyword.
- `links --id <id>` shows a record's neighbourhood — a quick way to expand context: "what tends to come up with this?"

## Janitor (keep it from going stale)

`python3 scripts/memory.py scan` (optionally `--agent <slug>`) returns overdue, done, stale, and duplicate active items, plus `stale_longterm` — long-term records not used in `LONGTERM_STALE_DAYS` (60) **and** not well-connected in the graph — and `unverified` (see below). Add judgment a script cannot, then propose a short maintenance list and let the user approve.

- Active items: `compact --archive <ids>` or `--drop <ids> --reason "..."`. Resurface a parked item with `resurface --id <id>` (it returns under its original id and agent; its edges are cleared).
- Long-term records on the chopping block: `forget --ids <ids> [--reason ...]` retires them (marked `dropped`, excluded from recall, edges removed).

### Closing the verify loop

`scan` also returns **`unverified`** — below-`high` records that carry a `verify` path (a known way to firm them up), sorted by reliance (most recently used / best-connected first), so the shaky things the work keeps leaning on float to the top. When you actually run one down, record the result with **`reconfirm`**:

```
reconfirm --id <id> [--confidence high|medium|low] [--source "what you confirmed it with"] [--verify ""]
```

It folds the new evidence into `source` (appended to the existing trail, or `--replace-source` to overwrite), adjusts `confidence` — **up or down**, since disconfirming evidence is just as valid — clears or rewrites the `verify` path (pass `--verify ""` once it's walked), resets the staleness clock, and logs the change. So a low-confidence guess doesn't stay a guess forever: `scan` shows what's worth confirming, you check it, and `reconfirm` promotes it (or demotes it) with the evidence attached.

**Use it or lose it:** any reactivation — a `recall`/`brief` that surfaces it (reinforce is the default), an explicit `reinforce`, or a `link` — resets a record's `last_used`, and a strongly-wired record is spared even when dormant. So frequently-recalled and well-connected memories survive; truly forgotten ones surface for cleanup.

## Dream (consolidate & re-synthesise)

Where `scan` is the janitor that *prunes*, **`dream` is the consolidator that *integrates*** — memory's version of sleep. Run it occasionally at save time (after `scan`), when the store has grown enough that clusters have formed. Two movements:

**1. Roll up a cluster into an `insight`.** `python3 scripts/memory.py dream` (optionally `--agent <slug>`) returns `clusters` — dense neighbourhoods of records the graph has wired together — each with its members. When a cluster is a coherent topic that's essentially closed out, *reason a summary of it* and fold it:

```
consolidate --agent <slug> --from <id> <id> <id> --text "<the synthesised insight>" [--tags ... --confidence ... --goal ... --outcome ... --constraints ...]
```

This writes a new `insight` (the highest-ranked kind), links it to each source atom, and marks those atoms **consolidated** — held out of primary recall but kept as evidence, surfacing one hop behind the summary (`"why": "linked to a match"`). So later recall leads with the synthesis and the atoms are its trail back to ground truth. Consolidate only what's *settled* — a live, still-changing cluster should stay as atoms. Don't fold across unrelated subjects just because they're linked; read the members and confirm they're one thing.

**Carry the keywords across so the topic still comes up.** A folded atom is no longer a primary hit on *its own* words, so the insight has to inherit them. It automatically keeps the union of the atoms' `tags`/`people`; on top of that, **pull the major keywords out of the folded memories and pass them as `--tags`** — `dream` hands you a `suggested_keywords` list per cluster (the members' tags plus the salient words from their text, e.g. `terse`, `async`, `dark`), so lift the ones that matter, including any that lived only in an atom's prose. Tags are weighted well above free text in recall, so a good keyword set means a query like `"terse"` still lands on the consolidated insight.

**2. Re-synthesise the persona.** `dream`'s `persona` block returns the current profile plus the newest high-confidence `fact`s. When the facts have drifted ahead of the profile, reason a fresh profile and write it with `profile --name/--title/--notes/--focus-areas/--key-people`. The profile is person-level and can contradict what's there, so this is an **ask-first** write — show the user the proposed profile before saving.

`dream` itself writes nothing; it only surfaces the material. Nothing is folded or rewritten until you run `consolidate`/`profile`, so treat a dream pass like `scan`: propose a short list, get a yes, then act.

## Filtering by agent

`--agent <slug>` filters reads and scopes recall, scan, export, and render. The `agents` verb lists every agent that has touched memory with active and long-term counts. Omit `--agent` on a read to see the whole landscape.

## Persisting (important)

The store lives in the plugin's writable **data** dir (`<data_root>/memory/`),
separate from the read-only core. A `memory.py` write lands there and is durable
immediately. (`memory.py` resolves the data dir automatically; pass
`--root <dir>` only to target a different store.)
If code execution is unavailable, you cannot write; tell the user and offer the
memory as text (`render`).

## Integrity

Writes to `memory.json` validate, back up to `memory.json.bak`, and replace atomically, so a bad write cannot land. If `read` reports trouble, run `validate --fix` (safe mechanical repairs only) and `doctor` for the db.
