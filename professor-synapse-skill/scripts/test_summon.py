#!/usr/bin/env python3
"""Test suite for summon.py — Professor Synapse's programmatic agent summoning.

Standard library only (unittest); no pip installs. Each test builds an isolated
temp skill root (agents/, SKILL.md, scripts/memory.py copy) so the shipped skill
is never touched. Run: python3 scripts/test_summon.py
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import summon  # noqa: E402

AGENT_A = """---
name: alpha-agent
emoji: 🅰️
description: Handles alpha tasks and widget research
triggers: alpha, widget, research, widget audit
---

# 🅰️: Alpha

## INSTRUCTIONS
Do alpha things. See `references/alpha-protocol.md` and run `scripts/memory.py`.

## Scripts

| Script | Purpose | Invoke |
|--------|---------|--------|
| `scripts/memory.py` | shared memory | `python3 scripts/memory.py --help` |
"""

AGENT_B = """---
name: beta-agent
emoji: 🅱️
description: Handles beta concerns
triggers: beta, gizmo, forget this
---

# 🅱️: Beta

## INSTRUCTIONS
Do beta things.
"""

# Shaped like the agent from the reported mis-route: its triggers contain the
# generic words "tracker" and "doc" as parts of multi-word phrases, and its
# slug contains "formatter" so a raw substring scorer scores "for" against it.
GAMMA_AGENT = """---
name: gamma-agent
emoji: 📋
description: Formats the weekly team meeting agenda doc from raw notes
triggers: weekly agenda, meeting doc, gizmo tracker, team sync
---

# 📋: Gamma

## INSTRUCTIONS
Format the weekly agenda.
"""

# Declares a trigger phrase IDENTICAL to one of gamma's, to exercise the
# genuine-tie branch: no ranking can separate two agents claiming "gizmo
# tracker", so the summoner must abstain and say so.
DELTA_AGENT = """---
name: delta-agent
emoji: 🔺
description: Also claims to track gizmos
triggers: delta, gizmo tracker
---

# 🔺: Delta

## INSTRUCTIONS
Do delta things.
"""

SKILL = """---
name: test-skill
---
# Skill

| Resource | When to Load | What It Contains |
|----------|--------------|------------------|
| `references/alpha-protocol.md` | When doing alpha | The alpha steps |
| `scripts/memory.py` | When recalling | The memory CLI |
"""


class SummonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="psumm-test-")
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "agents"))
        os.makedirs(os.path.join(self.root, "scripts"))
        os.makedirs(os.path.join(self.root, "memory"))
        self._write("agents/alpha-agent.md", AGENT_A)
        self._write("agents/beta-agent.md", AGENT_B)
        self._write("agents/INDEX.md", "# Agent Index\n")  # must be ignored
        self._write("SKILL.md", SKILL)
        # Real memory.py so recall actually runs against a fresh temp store.
        shutil.copy(HERE / "memory.py", os.path.join(self.root, "scripts", "memory.py"))

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel, text):
        with open(os.path.join(self.root, rel), "w", encoding="utf-8") as f:
            f.write(text)

    def run_cli(self, *argv):
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf):
            try:
                summon.main(["--root", self.root, *argv])
            except SystemExit as e:
                code = e.code or 0
        return buf.getvalue(), code

    # -- loading & resolution ----------------------------------------------

    def test_index_md_is_not_an_agent(self):
        agents = summon.load_agents(self.root)
        slugs = {a["slug"] for a in agents}
        self.assertEqual(slugs, {"alpha-agent", "beta-agent"})

    def test_exact_slug_resolves(self):
        agents = summon.load_agents(self.root)
        a, cands, why = summon.resolve_agent(agents, "beta-agent")
        self.assertEqual(a["slug"], "beta-agent")
        self.assertEqual(why, "exact slug")

    def test_fuzzy_trigger_resolves(self):
        agents = summon.load_agents(self.root)
        a, _, why = summon.resolve_agent(agents, "I need widget research")
        self.assertEqual(a["slug"], "alpha-agent")
        self.assertIn("trigger fired", why)

    def test_no_match_returns_none(self):
        agents = summon.load_agents(self.root)
        a, cands, _ = summon.resolve_agent(agents, "underwater basketweaving")
        self.assertIsNone(a)
        self.assertEqual(cands, [])

    def test_ambiguous_tie_returns_candidates(self):
        agents = summon.load_agents(self.root)
        # "research beta" fires a full trigger on BOTH agents -> abstain.
        a, cands, why = summon.resolve_agent(agents, "research beta")
        self.assertIsNone(a)
        self.assertEqual({c["agent"]["slug"] for c in cands}, {"alpha-agent", "beta-agent"})
        self.assertIn("multiple", why)

    # -- strict routing: no summon without a definite signal -----------------

    def test_partial_trigger_words_do_not_summon(self):
        """The reported bug: an agent whose triggers contain "gizmo tracker"
        must NOT be summoned by a task that merely says "tracker"."""
        self._write("agents/gamma-agent.md", GAMMA_AGENT)
        agents = summon.load_agents(self.root)
        a, cands, why = summon.resolve_agent(
            agents, "create Teachable lesson build tracker doc for Welcome Flow course")
        self.assertIsNone(a)
        self.assertIn("full trigger", why)
        # It may still be *suggested* — it must simply not be adopted.
        self.assertNotIn("gamma-agent", [c["agent"]["slug"] for c in cands if c["score"] >= 0.5])

    def test_more_specific_trigger_wins(self):
        """A broad trigger that is a SUBSET of a precise one must not force a
        disambiguation prompt: beta declares "gizmo", gamma declares the more
        specific "gizmo tracker". The longer complete phrase is stronger
        evidence of intent, so gamma is adopted."""
        self._write("agents/gamma-agent.md", GAMMA_AGENT)
        agents = summon.load_agents(self.root)
        a, _, why = summon.resolve_agent(agents, "update the gizmo tracker")
        self.assertIsNotNone(a)
        self.assertEqual(a["slug"], "gamma-agent")
        self.assertIn("most specific", why)

    def test_equally_specific_triggers_abstain(self):
        """Two agents declaring the SAME trigger phrase is a data problem in
        the frontmatter, not something ranking can resolve. Surface it."""
        self._write("agents/gamma-agent.md", GAMMA_AGENT)
        self._write("agents/delta-agent.md", DELTA_AGENT)
        agents = summon.load_agents(self.root)
        a, cands, why = summon.resolve_agent(agents, "update the gizmo tracker")
        self.assertIsNone(a)
        self.assertIn("equally specific", why)
        self.assertEqual({c["agent"]["slug"] for c in cands},
                         {"gamma-agent", "delta-agent"})

    def test_short_fragment_query_does_not_summon(self):
        """Short queries are where coverage scoring is least trustworthy:
        "lesson tracker doc" covers a lot of a short query without meaning it."""
        self._write("agents/gamma-agent.md", GAMMA_AGENT)
        agents = summon.load_agents(self.root)
        a, _, _ = summon.resolve_agent(agents, "build a lesson tracker doc")
        self.assertIsNone(a)

    def test_substring_is_not_a_match(self):
        """Matching is word-boundary. The old scorer tested `tok in hay`
        against the joined blurb, so `format` scored against `formats` (and
        `for` against `formatter`) — free points for prepositions."""
        self._write("agents/gamma-agent.md", GAMMA_AGENT)
        agents = summon.load_agents(self.root)
        gamma = next(a for a in agents if a["slug"] == "gamma-agent")
        self.assertIn("formats", summon._agent_words(gamma))     # the whole word is there
        by_slug = {r["agent"]["slug"]: r for r in summon.score_agents(agents, "format")}
        self.assertEqual(by_slug["gamma-agent"]["score"], 0.0)   # ...but `format` != `formats`
        self.assertEqual(summon.score_agents(agents, "for the of to"), [])  # stopwords only

    def test_multiword_trigger_needs_every_word(self):
        agents = summon.load_agents(self.root)
        alpha = next(a for a in agents if a["slug"] == "alpha-agent")
        # AGENT_A has the multi-word trigger "widget audit".
        self.assertEqual(summon.fired_triggers(alpha, summon._words("run a widget audit")),
                         ["widget", "widget audit"])
        self.assertEqual(summon.fired_triggers(alpha, summon._words("schedule an audit")), [])

    def test_trigger_with_stopwords_still_fires(self):
        """Trigger matching runs on RAW words. If it ran on stopword-filtered
        tokens, "forget this" could never fire."""
        agents = summon.load_agents(self.root)
        beta = next(a for a in agents if a["slug"] == "beta-agent")
        self.assertIn("forget this", summon.fired_triggers(beta, summon._words("forget this, it was wrong")))
        a, _, why = summon.resolve_agent(agents, "forget this, it was wrong")
        self.assertEqual(a["slug"], "beta-agent")
        self.assertIn("forget this", why)

    def test_stopword_only_query_scores_nothing(self):
        agents = summon.load_agents(self.root)
        a, cands, _ = summon.resolve_agent(agents, "please can you help me with the")
        self.assertIsNone(a)
        self.assertEqual(cands, [])

    def test_single_agent_roster_still_scores(self):
        """Regression: textbook idf log(N/df) is 0 for every term when N==1,
        collapsing the denominator to zero and abstaining on everything."""
        os.remove(os.path.join(self.root, "agents", "beta-agent.md"))
        agents = summon.load_agents(self.root)
        self.assertEqual(len(agents), 1)
        ranked = summon.score_agents(agents, "widget questions")
        self.assertEqual(len(ranked), 1)
        self.assertGreater(ranked[0]["score"], 0.0)
        # An exact trigger still summons on a one-agent roster.
        a, _, _ = summon.resolve_agent(agents, "I need widget research")
        self.assertEqual(a["slug"], "alpha-agent")

    def test_scores_are_ranked_and_bounded(self):
        agents = summon.load_agents(self.root)
        ranked = summon.score_agents(agents, "widget gizmo research")
        self.assertEqual([r["agent"]["slug"] for r in ranked][0], "alpha-agent")
        for r in ranked:
            self.assertGreaterEqual(r["score"], 0.0)
            self.assertLessEqual(r["score"], 1.0)

    # -- confidence is visible to the caller --------------------------------

    def test_markdown_shows_why_matched(self):
        out, code = self.run_cli("I need widget research", "--no-reinforce")
        self.assertEqual(code, 0)
        self.assertIn("# Summoned: 🅰️ alpha-agent", out)
        self.assertIn("Matched by: trigger fired", out)

    def test_markdown_exact_slug_omits_why(self):
        out, _ = self.run_cli("alpha-agent", "--no-reinforce")
        self.assertNotIn("Matched by:", out)

    def test_abstain_markdown_lists_scored_candidates(self):
        self._write("agents/gamma-agent.md", GAMMA_AGENT)
        out, code = self.run_cli("build a lesson tracker doc")
        self.assertEqual(code, 0)              # candidates exist -> not a hard no-match
        self.assertIn("No confident match", out)
        self.assertNotIn("# Summoned:", out)   # the whole point
        self.assertIn("| Agent | Score | Matched on |", out)
        self.assertIn("`gamma-agent`", out)

    def test_abstain_json_carries_scores(self):
        self._write("agents/gamma-agent.md", GAMMA_AGENT)
        out, code = self.run_cli("build a lesson tracker doc", "--json")
        d = json.loads(out)
        self.assertFalse(d["matched"])
        self.assertIn("reason", d)
        self.assertTrue(d["candidates"])
        top = d["candidates"][0]
        self.assertIn("slug", top)
        self.assertIsInstance(top["score"], float)
        self.assertIn("matched", top)

    def test_match_json_carries_reason(self):
        out, _ = self.run_cli("I need widget research", "--no-reinforce", "--json")
        d = json.loads(out)
        self.assertTrue(d["matched"])
        self.assertIn("trigger fired", d["reason"])

    # -- resources ----------------------------------------------------------

    def test_skill_table_parsed(self):
        table = summon.parse_skill_resources(self.root)
        self.assertIn("references/alpha-protocol.md", table)
        self.assertEqual(table["references/alpha-protocol.md"][0], "When doing alpha")

    def test_resources_exclude_scripts_section(self):
        agents = summon.load_agents(self.root)
        alpha = next(a for a in agents if a["slug"] == "alpha-agent")
        scripts = summon.extract_scripts_section(alpha["body"])
        self.assertIn("scripts/memory.py", scripts)
        res = summon.collect_resources(alpha, summon.parse_skill_resources(self.root), exclude_text=scripts)
        paths = [r["path"] for r in res]
        self.assertIn("references/alpha-protocol.md", paths)
        self.assertNotIn("scripts/memory.py", paths)  # deduped: already in Scripts table

    # -- end-to-end markdown / json ----------------------------------------

    def test_markdown_boot_package(self):
        out, code = self.run_cli("alpha-agent", "--no-reinforce")
        self.assertEqual(code, 0)
        self.assertIn("# Summoned: 🅰️ alpha-agent", out)
        self.assertIn("## Persona & Instructions", out)
        self.assertIn("Do alpha things", out)
        self.assertIn("## Recalled context", out)
        self.assertIn("## Resources you can load", out)
        self.assertIn("references/alpha-protocol.md", out)

    def test_json_boot_package(self):
        out, code = self.run_cli("alpha-agent", "--query", "widget", "--no-reinforce", "--json")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertTrue(d["matched"])
        self.assertEqual(d["agent"]["slug"], "alpha-agent")
        self.assertEqual(d["query"], ["widget"])
        self.assertIn("memory", d)
        self.assertIn("profile", d["memory"])

    def test_default_query_falls_back_to_triggers(self):
        out, code = self.run_cli("alpha-agent", "--no-reinforce", "--json")
        d = json.loads(out)
        self.assertEqual(d["query"], ["alpha", "widget", "research", "widget audit"])

    def test_no_match_exit_code(self):
        out, code = self.run_cli("underwater basketweaving")
        self.assertEqual(code, 3)
        self.assertIn("No agent matches", out)

    def test_recall_is_real_and_read_only(self):
        # --no-reinforce on an empty store must not create records or error.
        out, code = self.run_cli("alpha-agent", "--query", "anything", "--no-reinforce", "--json")
        d = json.loads(out)
        self.assertNotIn("error", d["memory"])
        self.assertEqual(d["memory"]["matches"], [])

    def test_recall_hits_the_real_store(self):
        # Seed a record via memory.py into <root>/memory/, then summon must
        # surface it — proving summon points memory.py at the SAME store
        # (regression: it once nested the root and read an empty fresh db).
        import subprocess
        mem = os.path.join(self.root, "scripts", "memory.py")
        subprocess.run([sys.executable, mem, "--root", self.root, "--agent", "alpha-agent",
                        "record", "--kind", "fact", "--text", "widgets ship on tuesdays",
                        "--tags", "widget"], check=True, capture_output=True, text=True)
        out, code = self.run_cli("alpha-agent", "--query", "widget", "--no-reinforce", "--json")
        d = json.loads(out)
        texts = [m["text"] for m in d["memory"].get("matches", [])]
        self.assertIn("widgets ship on tuesdays", texts)
        # And no nested memory/memory/ store was created.
        self.assertFalse(os.path.exists(os.path.join(self.root, "memory", "memory")))


    def test_recall_memory_requests_utf8_decoding(self):
        payload = {"profile": {"notes": "\u03bb"}, "active": [], "due": [],
                   "matches": [], "recent": []}
        completed = summon.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(payload, ensure_ascii=False), stderr="")
        with mock.patch.object(summon.subprocess, "run", return_value=completed) as run:
            result = summon.recall_memory(
                self.root, "alpha-agent", ["widget"], no_reinforce=True)
        self.assertEqual(result, payload)
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
if __name__ == "__main__":
    unittest.main(verbosity=2)
