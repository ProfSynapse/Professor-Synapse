#!/usr/bin/env python3
"""Test suite for _rescue.py — recovering stores written to the wrong dir.

Standard library only (unittest); no pip installs. Each test builds an isolated
temp plugins tree, so the real ~/.claude/plugins is never read or touched.
Run: python3 hooks/test_rescue.py
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _rescue  # noqa: E402

PLUGIN = _rescue.PLUGIN_NAME

SEED = {
    "meta": {"schema_version": 1, "updated_at": None},
    "profile": {"name": None, "notes": None, "focus_areas": [], "key_people": []},
    "active": [],
}


def write_store(root: Path, items=0, records=0, touched=None):
    """Lay down a memory/ store: `items` in working memory, `records` long-term.

    With items=0/records=0 this is the shipped seed — the state a pristine
    install is in, which must never be mistaken for a stray store.
    """
    mem = root / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    data = json.loads(json.dumps(SEED))
    data["active"] = [{"id": f"a{i}", "text": f"item {i}"} for i in range(items)]
    if touched is None:
        touched = bool(items or records)
    data["meta"]["updated_at"] = "2026-08-25T00:00:00Z" if touched else None
    (mem / "memory.json").write_text(json.dumps(data), encoding="utf-8")

    con = sqlite3.connect(mem / "longterm.db")
    con.execute("CREATE TABLE IF NOT EXISTS record (id TEXT PRIMARY KEY, body TEXT)")
    con.executemany("INSERT OR REPLACE INTO record VALUES (?, ?)",
                    [(f"r{i}", f"record {i}") for i in range(records)])
    con.commit()
    con.close()
    return root


class RescueTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ps-rescue-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.plugins = self.tmp / ".claude" / "plugins"
        self.live = self.plugins / "data" / f"{PLUGIN}-mkt"
        self.live.mkdir(parents=True)
        self.skill = (self.plugins / "cache" / "mkt" / PLUGIN / "3.7.0"
                      / "skills" / PLUGIN)
        self.skill.mkdir(parents=True)

    def rescue(self):
        return _rescue.rescue(self.live, self.skill)

    def rescued_dirs(self):
        return sorted(p.name for p in self.live.glob(_rescue.RESCUE_PREFIX + "*"))


class TestDetection(RescueTestCase):

    def test_pristine_install_says_nothing(self):
        write_store(self.live)                       # seed only
        write_store(self.skill)                      # seed only
        self.assertEqual(self.rescue(), [])
        self.assertEqual(self.rescued_dirs(), [])

    def test_no_store_at_all_says_nothing(self):
        self.assertEqual(self.rescue(), [])

    def test_the_live_store_is_never_rescued_from_itself(self):
        write_store(self.live, items=5, records=9)
        self.assertEqual(self.rescue(), [])
        self.assertEqual(self.rescued_dirs(), [])

    def test_a_store_with_only_profile_data_still_counts(self):
        """updated_at set, nothing else: someone used it, so it is not a seed."""
        write_store(self.skill, items=0, records=0, touched=True)
        self.assertIsNotNone(_rescue.store_summary(self.skill))

    def test_seed_summarises_as_no_store(self):
        write_store(self.live)
        self.assertIsNone(_rescue.store_summary(self.live))


class TestRescuing(RescueTestCase):

    def test_in_place_fork_is_copied_beside_the_live_store(self):
        """PR #57's machine: model-run scripts wrote into the skill root."""
        write_store(self.live, items=1, records=2)
        write_store(self.skill, items=7, records=17)
        lines = self.rescue()
        self.assertEqual(self.rescued_dirs(), ["rescued-store-in-place"])
        dest = self.live / "rescued-store-in-place"
        self.assertEqual(_rescue.store_summary(dest), (7, 17))
        self.assertEqual(len(lines), 1)
        self.assertIn("17 long-term record(s)", lines[0])

    def test_the_orphan_is_copied_not_moved(self):
        write_store(self.skill, items=3, records=4)
        self.rescue()
        self.assertEqual(_rescue.store_summary(self.skill), (3, 4))

    def test_the_live_store_is_left_untouched(self):
        write_store(self.live, items=1, records=2)
        write_store(self.skill, items=7, records=17)
        self.rescue()
        self.assertEqual(_rescue.store_summary(self.live), (1, 2))

    def test_a_stale_sibling_data_dir_is_rescued_with_its_agents(self):
        """A data dir's agents/ is purely the user's, so it comes along."""
        stale = self.plugins / "data" / f"{PLUGIN}-inline"
        write_store(stale, items=2, records=3)
        (stale / "agents").mkdir()
        (stale / "agents" / "my-agent.md").write_text("# mine", encoding="utf-8")
        self.rescue()
        dest = self.live / f"rescued-store-{PLUGIN}-inline"
        self.assertEqual(_rescue.store_summary(dest), (2, 3))
        self.assertEqual((dest / "agents" / "my-agent.md").read_text(), "# mine")

    def test_in_place_agents_are_not_copied(self):
        """A skill root's agents/ is the shipped roster; copying duplicates it."""
        write_store(self.skill, items=1, records=1)
        (self.skill / "agents").mkdir()
        (self.skill / "agents" / "domain-researcher.md").write_text("shipped")
        self.rescue()
        self.assertFalse((self.live / "rescued-store-in-place" / "agents").exists())

    def test_several_orphans_are_each_rescued(self):
        write_store(self.skill, items=1, records=1)
        write_store(self.plugins / "data" / f"{PLUGIN}-inline", items=2, records=2)
        self.rescue()
        self.assertEqual(self.rescued_dirs(),
                         ["rescued-store-in-place", f"rescued-store-{PLUGIN}-inline"])

    def test_rescue_dirs_are_not_themselves_treated_as_orphans(self):
        write_store(self.skill, items=1, records=1)
        self.rescue()
        self.rescue()
        self.assertEqual(self.rescued_dirs(), ["rescued-store-in-place"])


class TestIdempotence(RescueTestCase):

    def test_a_second_session_does_not_copy_again_but_keeps_reporting(self):
        write_store(self.skill, items=7, records=17)
        first = self.rescue()
        self.assertIn("copied from", first[0])
        second = self.rescue()
        self.assertEqual(self.rescued_dirs(), ["rescued-store-in-place"])
        self.assertIn("not yet merged", second[0])

    def test_an_existing_rescue_is_not_overwritten_by_later_divergence(self):
        write_store(self.skill, items=7, records=17)
        self.rescue()
        write_store(self.skill, items=1, records=1)      # orphan changed after
        self.rescue()
        dest = self.live / "rescued-store-in-place"
        self.assertEqual(_rescue.store_summary(dest), (7, 17))

    def test_deleting_the_rescue_dir_ends_the_notice(self):
        write_store(self.skill, items=7, records=17)
        self.rescue()
        __import__("shutil").rmtree(self.skill / "memory")
        self.assertEqual(_rescue.notice(self.rescue()), "")


class TestFailsSoft(RescueTestCase):

    def test_a_corrupt_memory_json_does_not_raise(self):
        (self.skill / "memory").mkdir(parents=True)
        (self.skill / "memory" / "memory.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(_rescue.store_summary(self.skill))
        self.assertEqual(self.rescue(), [])

    def test_a_corrupt_longterm_db_does_not_raise(self):
        write_store(self.skill, items=2, records=0)
        (self.skill / "memory" / "longterm.db").write_text("garbage", encoding="utf-8")
        self.assertEqual(_rescue.store_summary(self.skill), (2, 0))

    def test_a_memory_json_holding_a_list_does_not_raise(self):
        (self.skill / "memory").mkdir(parents=True)
        (self.skill / "memory" / "memory.json").write_text("[]", encoding="utf-8")
        self.assertIsNone(_rescue.store_summary(self.skill))

    def test_portable_mode_where_live_is_the_skill_root_is_a_no_op(self):
        write_store(self.skill, items=4, records=4)
        self.assertEqual(_rescue.rescue(self.skill, self.skill), [])

    def test_reading_a_store_never_creates_a_database(self):
        """Opening read-only matters: a stray db here would look like a store."""
        (self.skill / "memory").mkdir(parents=True)
        self.assertEqual(_rescue._longterm_records(self.skill / "memory" / "longterm.db"), 0)
        self.assertFalse((self.skill / "memory" / "longterm.db").exists())


class TestNotice(RescueTestCase):

    def test_no_orphans_means_no_notice(self):
        self.assertEqual(_rescue.notice([]), "")

    def test_the_notice_says_copied_and_names_the_path(self):
        write_store(self.skill, items=7, records=17)
        text = _rescue.notice(self.rescue())
        self.assertIn("COPIED", text)
        self.assertIn("rescued-store-in-place", text)
        self.assertIn("memory.py", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
