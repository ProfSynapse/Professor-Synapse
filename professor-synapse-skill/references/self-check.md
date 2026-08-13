# Self-Check

A repeatable verification of a Professor Synapse install — run it after an update, a rebuild, or whenever something seems off. It exercises the version marker, programmatic summoning, the memory loop (recall reinforces, graph clusters, forget retires), and both test suites.

Two ways to use it:

- **Automated:** the steps below are scripted; run each and confirm the expected result.
- **Hand to Claude:** paste the "Prompt for Claude" block (further down) into Claude Desktop and have it report PASS/FAIL per step.

> **Note on side effects:** steps 4–6 write a throwaway record (`tags: selfcheck`) to the **installed** store and then `forget` it. That's intentional — it proves reinforce/forget against the real store. Step 6 cleans it up. Nothing else is mutated.

## Steps

**1. Version marker**
```bash
grep '^\*\*Version' SKILL.md
```
Expect the current release version (e.g. `2.1.0`). Compare against `github.com/ProfSynapse/Professor-Synapse/releases/latest`.

**2. Test suites green**
```bash
python3 scripts/test_memory.py
python3 scripts/test_summon.py
```
Both must end in `OK` (80 and 27 cases respectively).

**3. Summoning — happy path, ambiguity, no-match**
```bash
python3 scripts/summon.py memory-agent --no-reinforce          # exact slug
python3 scripts/summon.py "what do you remember" --no-reinforce # trigger fires
python3 scripts/summon.py "underwater basketweaving"; echo "exit=$?"
```
Expect: the first two print a boot package with **Persona & Instructions**, **Recalled context**, and **Resources you can load** sections; the second also prints a `Matched by: trigger fired: ...` line. The third prints "No agent matches", lists existing agents, and exits `3`.

**3b. The summon gate refuses a coincidental match**
```bash
python3 scripts/summon.py "create a lesson tracker doc for the course"; echo "exit=$?"
```
Expect: **"No confident match"**, a scored candidate table, and **no `# Summoned:` header** — the words overlap some agents but no full trigger fired, so nothing is adopted. Exits `0` when there are candidates to show. If this prints a boot package, the strict gate has regressed and agents can be mis-routed onto the wrong procedure.

**4. Recall reinforces by default + reads the real store**
```bash
python3 scripts/memory.py --agent professor-synapse record \
  --kind fact --text "selfcheck probe fact" --tags selfcheck --confidence low
python3 scripts/summon.py memory-agent --query selfcheck --json \
  | python3 -c "import json,sys; m=json.load(sys.stdin)['memory']; \
print('matches:', [x['text'] for x in m.get('matches',[])])"
```
Expect `matches: ['selfcheck probe fact']` — proving summon recalls the **real** store (not a nested/empty one) and the default (reinforcing) recall surfaces it.

**5. Write-time duplicate check**
```bash
python3 scripts/memory.py check --kind fact --text "selfcheck probe fact" --tags selfcheck \
  | python3 -c "import json,sys; print('has_duplicate:', json.load(sys.stdin)['has_duplicate'])"
```
Expect `has_duplicate: True` — the probe from step 4 is flagged as a near-duplicate (read-only; writes nothing).

**6. Store integrity**
```bash
python3 scripts/memory.py validate    # working memory structure
python3 scripts/memory.py doctor       # long-term db integrity (integrity_check: ok)
```

**7. Forget retires the probe (cleanup)**
```bash
ID=$(python3 scripts/memory.py recall --query selfcheck --no-reinforce \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['candidates'][0]['id'])")
python3 scripts/memory.py forget --ids "$ID" --reason "self-check cleanup"
python3 scripts/memory.py recall --query selfcheck --no-reinforce \
  | python3 -c "import json,sys; print('remaining:', len(json.load(sys.stdin)['candidates']))"
```
Expect `remaining: 0` — the dropped record is excluded from recall.

## Prompt for Claude

Paste this into Claude Desktop to have it run the check and report:

```
Run the Professor Synapse self-check (references/self-check.md). For each step,
report PASS/FAIL with the relevant output:

1. SKILL.md Version matches the latest release tag.
2. Both suites end OK: `python3 scripts/test_memory.py` (44) and
   `python3 scripts/test_summon.py` (13).
3. Summoning: `summon.py memory-agent` returns a boot package with Persona,
   Recalled context, and Resources sections; `summon.py "underwater basketweaving"`
   reports no match and exits 3.
4. Recall reinforces + reads the real store: record a `fact` tagged `selfcheck`,
   then `summon.py memory-agent --query selfcheck` surfaces it under Recalled context.
5. Write-time check: `memory.py check --kind fact --text "selfcheck probe fact"
   --tags selfcheck` reports `has_duplicate: true`.
6. `memory.py validate` and `memory.py doctor` are clean.
7. Forget cleanup: forget the selfcheck fact, then a follow-up recall returns 0.

Note: steps 4–6 write and then remove a throwaway `selfcheck` record in the
installed store; that is expected and step 6 cleans it up.
```
