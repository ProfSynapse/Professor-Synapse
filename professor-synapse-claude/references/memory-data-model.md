# Memory Data Model

The canonical reference for Professor Synapse's shared memory. Read before changing the schema or the CLI. All access goes through `scripts/memory.py`; nothing hand-edits the files.

## Principle: cheap versus expensive change

- **Cheap, anytime:** an optional field (a JSON key with a default, or a db column via `ALTER TABLE ADD COLUMN`). Read it with a default and old data still works.
- **Expensive, needs a migration:** renaming a field, changing its type, splitting one into several, or changing an enum's meaning.
- **Impossible to backfill:** data never captured. This is why `agent`, `people`, and `tags` are structured from the start.

## The agent dimension

Every active item, long-term record, and changelog row carries an `agent` slug: the agent that created it. This is what lets memory be filtered by agent. Slugs match each agent's frontmatter `name`. Professor Synapse itself uses `professor-synapse`.

## Stores

- `memory/memory.json` - working memory (shared profile + active items). Hot.
- `memory/longterm.db` - long-term records and a change log. Cold.

## Invariants

- Each item has one opaque id (`m-` plus 8 hex) for life.
- An id is active in `memory.json` or archived in `longterm.db`, never both. `resurface` moves it back under the same id and agent.
- Active-item and long-term-record field names match for shared columns, so archiving is a copy.
- Working-memory writes validate, back up to `memory.json.bak`, then atomically replace.

## memory.json

### meta
| field | type | notes |
|---|---|---|
| `schema_version` | int | migration anchor. Currently 1. |
| `updated_at` | UTC ISO datetime | set on every write |

### profile (shared across agents, person-level)
| field | type | notes |
|---|---|---|
| `name`, `title`, `notes` | string or null | |
| `focus_areas` | list of strings | |
| `key_people` | list of `{name, role, note}` | |

### active item
| field | type | notes |
|---|---|---|
| `id` | string `m-<8 hex>` | opaque, lifelong, unique across both stores |
| `agent` | string | the agent that owns it; powers filtering |
| `type` | string or null | free-form sub-type the agent defines (no fixed enum) |
| `text` | string | |
| `people` | list of strings | powers recall |
| `tags` | list of strings | powers recall |
| `owner` | string or null | |
| `due` | date `YYYY-MM-DD` or null | |
| `status` | enum: `open`, `done` | |
| `source` | string or null | confidence basis: evidence held / where the claim comes from |
| `goal` | string or null | what the item is in service of |
| `outcome` | string or null | what resulted / current state |
| `constraints` | list of strings | gotchas/limits; each is a phrase (not comma-split), powers recall |
| `confidence` | enum or null: `high`, `medium`, `low` | how sure; nudges recall fusion |
| `verify` | string or null | confidence basis: available evidence + how to get it (the upgrade path) |
| `unknowns` | string or null | confidence basis: what remains unknown / unknown-unknown |
| `created_at`, `updated_at` | date `YYYY-MM-DD` | |

## longterm.db

### record (archived item, decision, note, fact, or lesson; keyed by lifelong id)
| column | type | notes |
|---|---|---|
| `id` | TEXT PK | same id as when active |
| `agent` | TEXT | who created it |
| `kind` | TEXT NOT NULL: `item`,`decision`,`note`,`fact`,`lesson`,`insight` | discriminator. Validated in Python (`RECORD_KINDS`), **not** a DB CHECK, so new kinds need no table rebuild |
| `type` | TEXT | free-form sub-type |
| `text` | TEXT NOT NULL | the core memory / headline |
| `people`, `tags` | TEXT (JSON array string) | |
| `owner` | TEXT | |
| `due` | TEXT (date) | |
| `status` | TEXT, in (`open`,`done`,`deferred`,`dropped`) | (still a DB CHECK) |
| `source` | TEXT | confidence basis: evidence held / where the claim comes from |
| `created_at` | TEXT (date) | |
| `recorded_at` | TEXT (UTC datetime) | when it entered long-term |
| `reason` | TEXT | why archived/dropped |
| `rationale` | TEXT | for decisions, why decided |
| `goal` | TEXT | what was being attempted (esp. `lesson`) |
| `outcome` | TEXT | what resulted (esp. `lesson`) |
| `constraints` | TEXT (JSON array string) | gotchas/limits; each a phrase |
| `confidence` | TEXT: `high`/`medium`/`low` | how sure (esp. `fact`) |
| `verify` | TEXT | confidence basis: available evidence + how to get it (the upgrade path) |
| `unknowns` | TEXT | confidence basis: what remains unknown / unknown-unknown |
| `last_used` | TEXT (UTC datetime) | last reactivation (a `recall`/`brief` that surfaces it — reinforce is default — or `reinforce`/`link`); resets the staleness clock |
| `consolidated_into` | TEXT | NULL normally. Set to an `insight`'s id when this record was **folded under it** by `consolidate` (see Dream). A consolidated atom is kept as evidence but held out of *primary* recall — it resurfaces only one hop behind its summary via the graph. Additive column (cheap) |

The `insight` kind is a **synthesised roll-up** of a cluster of related records — memory's "dream" (L2/L3 consolidation). It is written by `consolidate`, which links it to each source atom (so they stay reachable one hop behind it) and stamps those atoms' `consolidated_into`. It carries the highest `KIND_WEIGHT` (1.35), so the summary out-ranks the atoms it absorbed. The `lesson` kind is a reusable how-to learned by doing — it conventionally fills `goal`, `outcome`, and `constraints`, but those fields are optional on every kind. `goal`/`outcome`/`constraints` are full-text indexed for recall; `confidence` and `kind` apply multiplicative nudges in fusion. **Confidence carries its basis**: `source` (evidence held), `verify` (the upgrade path — what evidence is available and how to get it), and `unknowns` (gaps). All three are full-text indexed, so a query like "how to confirm X" surfaces the `verify` path; they are explanatory (surfaced in recall) and don't themselves change ranking.

### changelog (append-only audit)
| column | type | notes |
|---|---|---|
| `seq` | INTEGER PK AUTOINCREMENT | |
| `ts` | TEXT (UTC datetime) | |
| `agent` | TEXT | who acted |
| `action` | TEXT | `archive`, `drop`, `resurface`, `decision`, `note`, `fact`, `lesson`, `amend` |
| `item_id` | TEXT | |
| `summary` | TEXT | |

`amend` updates only explicitly supplied fields on a non-dropped long-term record, preserves its id and graph edges, refreshes `last_used`, and records the changed field names in `summary`.

### edge (knowledge graph — undirected weighted link between two records)
| column | type | notes |
|---|---|---|
| `a`, `b` | TEXT | record ids, stored canonically `a <= b` (PK), so one row per undirected pair |
| `weight` | REAL | accumulated affinity; **read through time-decay** (`_decay`), never raw |
| `count` | INTEGER | how many co-use / link events have hit this edge |
| `source` | TEXT | `manual` (explicit `link`), `corecall` (co-use), or `mixed` |
| `created_at`, `updated_at` | TEXT (UTC datetime) | `updated_at` anchors the decay |

Edges form two ways: an explicit `link --a --b`, or a co-use event — `recall`/`brief --query` reinforce by default (suppress with `--no-reinforce`), and `reinforce --ids ...` is the explicit form; each bumps every pair among the records used together ("fire together, wire together"). Each bump first **decays** the existing weight by elapsed time (half-life `EDGE_HALFLIFE_DAYS`) then adds the increment, so stale associations fade and the graph re-clusters. Edges are cross-agent (no `agent` column). They are dropped when either endpoint is `forget`-en or `resurface`-d; `doctor` reports any dangling edge.

## Tunable constants

Near the top of `scripts/memory.py`:
- **Lifecycle:** `SCHEMA_VERSION`, `STALE_DAYS` (21, active items), `LONGTERM_STALE_DAYS` (60, long-term chopping block), `GRAPH_SPARE_AFFINITY` (1.0 — wired-in records are spared), `LOG_CAP_DAYS` (90), `DEFAULT_AGENT`.
- **Recall fusion:** `RRF_K` (60), `W_TEXT` (1.0), `W_RECENCY` (0.5), `W_GRAPH` (0.6), `BM25_WEIGHTS` (id/text/people/tags/owner/goal/outcome/constraints/source/verify/unknowns — 11 columns), `KIND_WEIGHT` (includes `insight` at the top, then `fact`/`lesson`/`decision`), `CONFIDENCE_WEIGHT` (`high`/`medium`/`low`).
- **Graph:** `EDGE_HALFLIFE_DAYS` (30), `COUSE_INCREMENT` (1.0), `MANUAL_LINK_FLOOR` (3.0), `GRAPH_SEEDS` (5), `GRAPH_FANOUT` (20), `GRAPH_MIN_AFFINITY` (0.1).
- **Dream (consolidation):** `DREAM_EDGE_FLOOR` (0.5 — min decayed edge weight for two records to count as intra-cluster), `DREAM_MIN_CLUSTER` (3 — smallest cluster worth proposing), `DREAM_MAX_CLUSTERS` (10 — cap surfaced per `dream`), `DREAM_PERSONA_FACTS` (12 — high-confidence facts surfaced for persona re-synthesis).
- **Write-time similarity check** (`check` verb + `record` advisory): `SIMILAR_POOL` (12 FTS candidates), `SIMILAR_RETURN` (5 surfaced), `DUP_RATIO` (0.82 difflib ratio = likely duplicate), `SIMILAR_FLOOR` (0.45 — below this with no shared tag/person, not surfaced), `CONFLICT_KINDS` (`fact`/`decision` — kinds whose high-confidence records warrant a conflict flag). The check is read-only and reuses the transient FTS retrieval plus a `difflib` text-overlap score — **no schema change**.

## Adding a field, a column, or a version

- Working-memory field: add it to `empty_working()` with a default and read it defensively. No migration needed if purely additive.
- Db column: add it to `RECORD_DDL` and to `RECORD_COLUMNS`, then add an idempotent guard in `_migrate_record_schema()` (the loop that runs `ALTER TABLE record ADD COLUMN ...` when a column is missing). Existing dbs upgrade in place on the next `connect_db()`.
- New `kind` value: add it to `RECORD_KINDS` (and `KIND_WEIGHT` if it should rank distinctly). Because `kind` is validated in Python rather than by a DB CHECK, no rebuild is needed for *new* dbs. For dbs created before that change, `_migrate_record_schema()` detects the old restrictive `CHECK(kind IN (...))` in the stored DDL and **rebuilds the `record` table once** (rename → recreate from `RECORD_DDL` → copy all rows → drop old), preserving every row. This ran for the `lesson` addition.
- Working-memory breaking change: bump `SCHEMA_VERSION` and register a transform in `MIGRATIONS` keyed by the version you migrate FROM. `migrate_working` runs it on load, refuses if a step is missing, and refuses files newer than the skill, so a mismatch fails loud.

## Search: ranked fusion over full-text, no extra storage

`recall --query` and `brief --query` are a two-stage ranker:

1. **Retrieve** with **SQLite FTS5** (stemming + prefix matching). The index is **built transiently at query time** from the `record` table and dropped immediately — no persistent FTS table, no triggers, **no schema change or migration**. Retrieval uses **column-weighted BM25** (`BM25_WEIGHTS`) over `text/people/tags/owner/goal/outcome/constraints/source/verify/unknowns` so a hit in `people`/`tags`/`constraints` outranks one buried in free text, `goal`/`outcome` rank above plain text, and the confidence-basis fields (`source`/`verify`/`unknowns`) are searchable but weighted modestly (`verify` a touch higher, so "how do I confirm X" finds the upgrade path). It pulls a candidate pool (≈3× the result limit).
2. **Spread** through the knowledge graph: the top `GRAPH_SEEDS` text hits seed one-hop spreading activation over `edge` (decayed weights), pulling in strongly-linked records — so a memory wired to what you asked about surfaces *even on a weak text match*. The seed hubs earn their own connection weight too, so a well-connected match isn't out-ranked by its neighbours.
3. **Re-rank** by **fusing three ranked lists** with Reciprocal Rank Fusion (RRF): BM25 relevance (`W_TEXT`), recency by `recorded_at` (`W_RECENCY`), and graph affinity (`W_GRAPH`). Graph-only candidates get the worst text rank but can still surface on the graph term. Then multiplicative nudges — `KIND_WEIGHT` so `insight`/`fact`/`lesson`/`decision` edge out `note`, and `CONFIDENCE_WEIGHT` so a `high`-confidence record edges out a `low` one. `dropped` records are excluded — they were retired on purpose. **Consolidated atoms** (`consolidated_into` set) are also excluded from the *primary* text candidates, but — unlike dropped — they remain eligible as graph-spread neighbours, so a folded atom resurfaces one hop behind its `insight` (`"why": "linked to a match"`), never on its own words alone. With no edges, the graph term is constant and ranking is identical to text+recency alone.

RRF is scale-free, so the disparate signals combine without normalizing raw scores. At this store's scale (tens to low hundreds of records) the whole thing is instant and needs zero maintenance. If a SQLite build lacks FTS5, `recall` falls back to substring `LIKE`, so it is never worse than before.

**No-query fallback (`_recent_hits`).** Keyword recall needs query terms; a bare summon would otherwise return empty even when the agent has records. So the fallback surfaces the agent's `RECENT_DEFAULT` most relevant recent records: pull a recency-ordered pool (`ORDER BY recorded_at DESC`, excluding `dropped`), then rerank *inside* that pool by recency (`W_RECENCY`) fused with the same `KIND_WEIGHT`/`CONFIDENCE_WEIGHT` nudges. Only recent records are ever in play, but a high-value `fact`/`lesson` can edge out a slightly newer `note`.

The trigger differs by verb, on purpose:
- **`brief`** (the summon prefetch — `summon.py` auto-fills the agent's triggers as the query) falls back whenever there are **no matches**, since a fresh agent whose triggers don't lexically match its stored memory is the exact case we're fixing. Returns the set under a `recent` key.
- **`recall`** (an explicit search verb) falls back only when **no query terms were supplied at all**. An explicit query that simply misses still returns empty — that's an honest "no match," not an excuse to dump recent records. Folds the set into `candidates`.

These hits carry `"why": "recent (no query match)"` and are *not* co-wired into the graph (no query, no Hebbian event) — they only reset their own staleness clock when reinforcement is on.

Semantic/vector search (e.g. `sqlite-vec` + embeddings) was considered and **rejected as overkill**: it needs a loadable extension (often disabled in the sandbox's `sqlite3`) plus vendored model weights, for gains that aren't perceptible at this scale — and the LLM driving the skill already supplies the semantic layer (it can issue synonym queries itself). Revisit only if the store grows by orders of magnitude.

## Dream: consolidation (L2/L3 roll-up)

Memory consolidates the way sleep does: dense clusters of related atoms get rolled up into a higher-altitude summary, and the persona is re-derived from what's been learned. Two verbs, both driven by the agent's reasoning (the CLI supplies mechanics, not an LLM):

- **`dream`** (read-only) surfaces the raw material: **clusters** — connected components of the co-recall graph over edges whose *decayed* weight clears `DREAM_EDGE_FLOOR`, among live records (non-`dropped`, non-consolidated), returned densest-first for components of at least `DREAM_MIN_CLUSTER` members, each carrying `suggested_keywords` (the members' curated tags plus the most frequent stopword-filtered words in their text/constraints) — and **persona material** — the current profile plus the newest `DREAM_PERSONA_FACTS` high-confidence `fact`s. It writes nothing.
- **`consolidate --from <ids> --text "<summary>"`** folds a cluster into a new `insight`: it writes the summary record, `link`s it to each source atom (a `manual` edge, so drill-down works), and stamps each atom's `consolidated_into`. The atoms are then held out of primary recall (see search, above) but kept as evidence, reachable one hop behind the summary. **The insight inherits recall keys** — its `tags`/`people` are the union of every source atom's `tags`/`people` plus whatever `--tags`/`--people` the agent passes — so a folded topic still surfaces on its own words (a tag hit is weighted well above free text). The agent is meant to pull the *major keywords* of the folded memories (dream hands them over as `suggested_keywords`, including salient words that lived only in the atoms' prose) and pass them as `--tags`; the atoms' own tags come along automatically. Guards: every `--from` id must exist, not be `dropped`, and not already be consolidated.

The graph edge from summary → atoms *is* the traceability link back to evidence — and because it points record → record (durable), not record → conversation (ephemeral), it can't dangle. Persona re-synthesis has no dedicated verb: the agent reads `dream`'s persona block, reasons, and writes the result with the existing `profile` verb (a person-level, ask-first write).

## Deferred on purpose

`people` and `tags` are JSON arrays in a TEXT column, not a junction table. FTS5 (and `LIKE` as a fallback) over the joined values is enough at this scale. Migrating to a `record_people` junction table later is possible by parsing the JSON; reach for it only when simple search stops being enough.
