#!/usr/bin/env python3
"""Test suite for _pluginpaths.py — where Professor Synapse's writable state lives.

Standard library only (unittest); no pip installs. Each test builds an isolated
temp plugins tree, so the real ~/.claude/plugins is never read or touched.
Run: python3 scripts/test_pluginpaths.py
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _pluginpaths  # noqa: E402

PLUGIN = _pluginpaths.PLUGIN_NAME
DAY = 86400.0


class DataRootTestCase(unittest.TestCase):
    """Builds <tmp>/plugins/{cache,data} and points the resolver at it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ps-pluginpaths-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.plugins = self.tmp / ".claude" / "plugins"
        (self.plugins / "data").mkdir(parents=True)
        # A cache-shaped location for this module, so _plugins_root_from finds
        # the temp plugins root instead of the real home dir.
        self.here = (self.plugins / "cache" / "mkt" / PLUGIN / "1.0.0"
                     / "skills" / PLUGIN / "scripts" / "_pluginpaths.py")
        self.here.parent.mkdir(parents=True)
        self.skill_root = self.here.parents[1]
        # No CLAUDE_PLUGIN_DATA: this is model-invoked-Bash context, the case
        # the whole module exists for.
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def make_data_dir(self, suffix, created, used=None, state=("memory", ".summon-state")):
        """A data dir created at `created`, whose state was last written at `used`."""
        d = self.plugins / "data" / f"{PLUGIN}-{suffix}"
        for sub in state:
            (d / sub).mkdir(parents=True)
        d.mkdir(exist_ok=True)
        for sub in state:
            child = d / sub
            # Atomic writes (tmp + os.replace) land the rename inside the state
            # dir — that is what moves its mtime on every real write.
            (child / "payload.json").write_text(json.dumps({"n": 1}))
            os.utime(child, (used if used is not None else created,) * 2)
        os.utime(d, (created, created))
        return d

    def glob(self):
        return _pluginpaths._glob_data(self.here)


class TestGlobData(DataRootTestCase):

    def test_no_data_dir_at_all_returns_none(self):
        self.assertIsNone(self.glob())

    def test_single_match_wins(self):
        live = self.make_data_dir("mkt", created=1000.0)
        self.assertEqual(self.glob(), live)

    def test_files_are_not_mistaken_for_data_dirs(self):
        (self.plugins / "data" / f"{PLUGIN}-stray").write_text("not a dir")
        live = self.make_data_dir("mkt", created=1000.0)
        self.assertEqual(self.glob(), live)

    def test_unrelated_plugins_are_ignored(self):
        (self.plugins / "data" / "some-other-plugin-mkt").mkdir()
        live = self.make_data_dir("mkt", created=1000.0)
        self.assertEqual(self.glob(), live)

    def test_ambiguity_picks_the_recently_used_dir_not_the_stale_one(self):
        now = 1_700_000_000.0
        self.make_data_dir("inline", created=now - 30 * DAY, used=now - 20 * DAY)
        live = self.make_data_dir("mkt", created=now - 29 * DAY, used=now - 60.0)
        self.assertEqual(self.glob(), live)

    def test_live_dir_wins_even_when_the_stale_dir_was_touched_later(self):
        """The regression the naive fix misses.

        A data dir's own mtime freezes once its children exist, so a dir in
        daily use looks a month old while one stray file dropped into a stale
        dir makes that dir look fresh. Judging by the state dirs sees through it.
        """
        now = 1_700_000_000.0
        stale = self.make_data_dir("inline", created=now - 30 * DAY, used=now - 20 * DAY)
        live = self.make_data_dir("mkt", created=now - 29 * DAY, used=now - 3600.0)
        (stale / ".DS_Store").write_text("")           # Finder, a backup restore, a stray cp
        os.utime(stale, (now - DAY, now - DAY))
        self.assertGreater(stale.stat().st_mtime, live.stat().st_mtime)  # naive key inverts
        self.assertEqual(self.glob(), live)

    def test_a_dir_with_no_state_loses_to_one_in_use(self):
        now = 1_700_000_000.0
        self.make_data_dir("empty", created=now - DAY, state=())        # created, never used
        live = self.make_data_dir("mkt", created=now - 30 * DAY, used=now - 3600.0)
        self.assertEqual(self.glob(), live)

    def test_ties_break_deterministically_by_name(self):
        now = 1_700_000_000.0
        self.make_data_dir("zzz", created=now, used=now)
        first = self.make_data_dir("aaa", created=now, used=now)
        self.assertEqual(self.glob(), first)
        self.assertEqual(self.glob(), first)          # and the same answer twice

    def test_unstattable_state_dir_does_not_raise(self):
        """A dangling symlink where a state dir should be: stat() raises, and
        the resolver has to shrug it off rather than take down summon.py."""
        now = 1_700_000_000.0
        live = self.make_data_dir("mkt", created=now, used=now)
        (live / "agents").symlink_to(self.tmp / "gone")
        self.assertRaises(OSError, (live / "agents").stat)
        self.assertEqual(self.glob(), live)


class TestResolveDataRoot(DataRootTestCase):

    def test_env_var_wins_when_present(self):
        self.make_data_dir("mkt", created=1000.0)
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": "/explicit/data"}):
            self.assertEqual(_pluginpaths.resolve_data_root(str(self.skill_root)), "/explicit/data")

    def test_falls_back_in_place_outside_a_plugin(self):
        """A portable-skill or dev checkout: no cache path, no data dirs."""
        with mock.patch.object(_pluginpaths, "_glob_data", return_value=None):
            with mock.patch.object(_pluginpaths, "_derive_from_cache", return_value=None):
                self.assertEqual(_pluginpaths.resolve_data_root("/some/skill"), "/some/skill")

    def test_glob_result_is_used_when_the_cache_layout_does_not_derive(self):
        live = self.make_data_dir("mkt", created=1000.0)
        with mock.patch.object(_pluginpaths, "_derive_from_cache", return_value=None):
            with mock.patch.object(_pluginpaths, "Path") as fake_path:
                fake_path.side_effect = Path
                fake_path.home.return_value = self.tmp
                fake_path.return_value = self.here
                got = _pluginpaths.resolve_data_root(str(self.skill_root))
        self.assertEqual(got, str(live))


if __name__ == "__main__":
    unittest.main(verbosity=2)
