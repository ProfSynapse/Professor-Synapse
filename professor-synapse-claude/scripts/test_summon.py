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
triggers: alpha, widget, research
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
triggers: beta, gizmo
---

# 🅱️: Beta

## INSTRUCTIONS
Do beta things.
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
        a, cands = summon.resolve_agent(agents, "beta-agent")
        self.assertEqual(a["slug"], "beta-agent")

    def test_fuzzy_trigger_resolves(self):
        agents = summon.load_agents(self.root)
        a, _ = summon.resolve_agent(agents, "I need widget research")
        self.assertEqual(a["slug"], "alpha-agent")

    def test_no_match_returns_none(self):
        agents = summon.load_agents(self.root)
        a, cands = summon.resolve_agent(agents, "underwater basketweaving")
        self.assertIsNone(a)
        self.assertEqual(cands, [])

    def test_ambiguous_tie_returns_candidates(self):
        agents = summon.load_agents(self.root)
        # "research beta" hits alpha (research) and beta (beta) equally -> tie.
        a, cands = summon.resolve_agent(agents, "research beta")
        self.assertIsNone(a)
        self.assertEqual({c["slug"] for c in cands}, {"alpha-agent", "beta-agent"})

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
        self.assertEqual(d["query"], ["alpha", "widget", "research"])

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
