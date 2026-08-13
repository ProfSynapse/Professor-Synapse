#!/usr/bin/env python3
"""Test suite for memory.py — Professor Synapse's shared memory CLI.

Standard library only (unittest); no pip installs, mirroring memory.py itself.
Run from anywhere:  python3 scripts/test_memory.py   (or: python3 -m unittest)
Each test runs against an isolated temp root, so the shipped store is never touched.
"""

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory  # noqa: E402


class MemTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="psmem-test-")
        self.root = self.dir.name
        os.makedirs(os.path.join(self.root, "memory"), exist_ok=True)

    def tearDown(self):
        self.dir.cleanup()

    # -- helpers ------------------------------------------------------------
    # NB: not named run()/run_json() — TestCase.run is the framework's driver.
    def cli(self, *argv):
        """Invoke the CLI with --root injected; return stdout text."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            memory.main(["--root", self.root, *argv])
        return buf.getvalue()

    def cli_json(self, *argv):
        return json.loads(self.cli(*argv))

    def db(self):
        return sqlite3.connect(os.path.join(self.root, "memory", "longterm.db"))

    def id_by_text(self, text):
        con = self.db()
        row = con.execute("SELECT id FROM record WHERE text=?", (text,)).fetchone()
        con.close()
        return row[0] if row else None

    def set_recorded_at(self, item_id, ts):
        con = self.db()
        con.execute("UPDATE record SET recorded_at=? WHERE id=?", (ts, item_id))
        con.commit()
        con.close()

    def texts(self, candidates):
        return [c["text"] for c in candidates]


class WorkingMemory(MemTest):
    def test_add_read_roundtrip(self):
        self.cli("--agent", "alpha", "add", "--text", "ship the thing", "--tags", "work")
        data = self.cli_json("read")
        self.assertEqual(len(data["active"]), 1)
        item = data["active"][0]
        self.assertEqual(item["text"], "ship the thing")
        self.assertEqual(item["agent"], "alpha")
        self.assertEqual(item["status"], "open")
        self.assertTrue(item["id"].startswith("m-"))

    def test_read_agent_filter(self):
        self.cli("--agent", "alpha", "add", "--text", "a-task")
        self.cli("--agent", "beta", "add", "--text", "b-task")
        self.assertEqual(len(self.cli_json("read")["active"]), 2)
        only = self.cli_json("--agent", "alpha", "read")
        self.assertEqual([i["text"] for i in only["active"]], ["a-task"])

    def test_profile_roundtrip(self):
        self.cli("profile", "--name", "Jordan", "--focus-areas", "ai", "ops")
        prof = self.cli_json("read")["profile"]
        self.assertEqual(prof["name"], "Jordan")
        self.assertEqual(prof["focus_areas"], ["ai", "ops"])

    def test_resolve_marks_done(self):
        self.cli("--agent", "a", "add", "--text", "do it")
        iid = self.cli_json("read")["active"][0]["id"]
        self.cli("resolve", "--id", iid)
        self.assertEqual(self.cli_json("read")["active"][0]["status"], "done")

    def test_update_fields(self):
        self.cli("--agent", "a", "add", "--text", "draft")
        iid = self.cli_json("read")["active"][0]["id"]
        self.cli("update", "--id", iid, "--text", "final", "--tags", "x", "y")
        item = self.cli_json("read")["active"][0]
        self.assertEqual(item["text"], "final")
        self.assertEqual(item["tags"], ["x", "y"])

    def test_tags_comma_or_space_separated(self):
        # An agent may type --tags a,b OR --tags a b; both must yield distinct tags.
        self.cli("--agent", "a", "add", "--text", "comma form", "--tags", "test,install")
        self.cli("--agent", "a", "add", "--text", "space form", "--tags", "test", "install")
        self.cli("--agent", "a", "add", "--text", "mixed form",
                 "--tags", "test, install", "extra")
        active = self.cli_json("read")["active"]
        by_text = {i["text"]: i["tags"] for i in active}
        self.assertEqual(by_text["comma form"], ["test", "install"])
        self.assertEqual(by_text["space form"], ["test", "install"])
        self.assertEqual(by_text["mixed form"], ["test", "install", "extra"])


class LongTerm(MemTest):
    def test_record_then_query(self):
        self.cli("--agent", "a", "record", "--kind", "fact",
                 "--text", "Alice owns billing", "--people", "Alice", "--tags", "billing")
        hits = self.cli_json("recall", "--query", "billing")["candidates"]
        self.assertEqual(len(hits), 1)
        self.assertIn("Alice owns billing", hits[0]["text"])

    def test_compact_archive_and_resurface(self):
        self.cli("--agent", "a", "add", "--text", "archive me")
        iid = self.cli_json("read")["active"][0]["id"]
        self.cli("compact", "--archive", iid)
        self.assertEqual(self.cli_json("read")["active"], [])
        con = self.db()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM record WHERE id=?", (iid,)).fetchone()[0], 1)
        con.close()
        self.cli("resurface", "--id", iid)
        back = self.cli_json("read")["active"]
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["id"], iid)  # same id for life

    def test_recall_due_surfaces_past_due_archived_item(self):
        self.cli("--agent", "a", "add", "--text", "call vendor", "--due", "2020-01-01")
        iid = self.cli_json("read")["active"][0]["id"]
        self.cli("compact", "--archive", iid)
        hits = self.cli_json("recall")["candidates"]
        self.assertEqual([h["id"] for h in hits], [iid])
        self.assertEqual(hits[0]["why"], "due date reached")

    def test_agents_counts(self):
        self.cli("--agent", "a", "add", "--text", "x")
        self.cli("--agent", "b", "record", "--kind", "note", "--text", "y")
        counts = self.cli_json("agents")
        self.assertEqual(counts["a"]["active"], 1)
        self.assertEqual(counts["b"]["longterm"], 1)


class Amend(MemTest):
    def seed(self, text, agent="alpha", **kw):
        argv = ["--agent", agent, "record", "--kind", "fact", "--text", text]
        for key, value in kw.items():
            argv += [f"--{key}", *(value if isinstance(value, list) else [value])]
        self.cli(*argv)
        return self.id_by_text(text)

    def test_updates_selected_fields_in_place_and_preserves_graph(self):
        target = self.seed("original record", tags=["initial"], confidence="low")
        neighbour = self.seed("related record")
        self.cli("link", "--a", target, "--b", neighbour)

        self.cli("--agent", "alpha", "amend", "--id", target,
                 "--text", "revised record", "--tags", "revised", "stable",
                 "--constraints", "retain associations", "--confidence", "high",
                 "--source", "confirmed source")

        con = self.db()
        row = con.execute(
            "SELECT id,agent,text,tags,constraints,confidence,source,last_used "
            "FROM record WHERE id=?", (target,)).fetchone()
        edge_count = con.execute(
            "SELECT COUNT(*) FROM edge WHERE a=? AND b=?",
            tuple(sorted((target, neighbour)))).fetchone()[0]
        event = con.execute(
            "SELECT action,item_id,summary FROM changelog ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        con.close()

        self.assertEqual(row[0], target)
        self.assertEqual(row[1], "alpha")
        self.assertEqual(row[2], "revised record")
        self.assertEqual(json.loads(row[3]), ["revised", "stable"])
        self.assertEqual(json.loads(row[4]), ["retain associations"])
        self.assertEqual(row[5], "high")
        self.assertEqual(row[6], "confirmed source")
        self.assertTrue(row[7])
        self.assertEqual(edge_count, 1)
        self.assertEqual(event[0:2], ("amend", target))
        self.assertIn("text", event[2])

    def test_omitted_fields_stay_unchanged_and_lists_can_be_cleared(self):
        target = self.seed("stable record", tags=["one", "two"], source="source")
        self.cli("--agent", "alpha", "amend", "--id", target,
                 "--tags", "--source", "")
        con = self.db()
        row = con.execute(
            "SELECT text,tags,source FROM record WHERE id=?", (target,)).fetchone()
        con.close()
        self.assertEqual(row[0], "stable record")
        self.assertEqual(json.loads(row[1]), [])
        self.assertIsNone(row[2])

    def test_rejects_unknown_dropped_and_wrong_agent_records(self):
        target = self.seed("protected record")
        with self.assertRaises(SystemExit):
            self.cli("--agent", "beta", "amend", "--id", target, "--text", "change")
        with self.assertRaises(SystemExit):
            self.cli("amend", "--id", "m-00000000", "--text", "change")
        self.cli("forget", "--ids", target)
        with self.assertRaises(SystemExit):
            self.cli("--agent", "alpha", "amend", "--id", target, "--text", "change")

    def test_requires_a_change_and_nonempty_text(self):
        target = self.seed("valid record")
        with self.assertRaises(SystemExit):
            self.cli("amend", "--id", target)
        with self.assertRaises(SystemExit):
            self.cli("amend", "--id", target, "--text", "   ")

class Search(MemTest):
    def seed(self, text, kind="note", agent="a", **kw):
        argv = ["--agent", agent, "record", "--kind", kind, "--text", text]
        for k, v in kw.items():
            argv += [f"--{k}", *( v if isinstance(v, list) else [v])]
        self.cli(*argv)
        return self.id_by_text(text)

    def test_stemming(self):
        self.seed("Acme contract renewals are quarterly")
        self.assertTrue(self.cli_json("recall", "--query", "renew")["candidates"],
                        "porter stemming should match renewals from 'renew'")

    def test_prefix(self):
        self.seed("Client onboarding checklist")
        self.assertTrue(self.cli_json("recall", "--query", "onboard")["candidates"],
                        "prefix match should match onboarding from 'onboard'")

    def test_special_chars_no_crash(self):
        self.seed("billing for Alice")
        hits = self.cli_json("recall", "--query", "alice's!! (billing)")["candidates"]
        self.assertEqual(len(hits), 1)  # operators stripped, still matches

    def test_no_match_empty(self):
        self.seed("totally unrelated")
        self.assertEqual(self.cli_json("recall", "--query", "zzzznope")["candidates"], [])

    def test_column_weighting_tag_beats_body(self):
        # both match 'acme'; one in tags, one only deep in free text. Tag hit should win.
        body = self.seed("a long note mentioning acme somewhere in the middle of prose")
        tag = self.seed("renewal terms", tags=["acme"])
        for i in (body, tag):
            self.set_recorded_at(i, "2026-01-01T00:00:00+00:00")  # equal recency
        order = [h["id"] for h in self.cli_json("recall", "--query", "acme")["candidates"]]
        self.assertEqual(order[0], tag, "tag/people columns are weighted above free text")

    def test_recency_breaks_relevance_tie(self):
        old = self.seed("acme contract alpha")  # identical relevance for 'acme'
        new = self.seed("acme contract bravo")
        self.set_recorded_at(old, "2026-01-01T00:00:00+00:00")
        self.set_recorded_at(new, "2026-06-01T00:00:00+00:00")
        order = [h["id"] for h in self.cli_json("recall", "--query", "acme")["candidates"]]
        self.assertEqual(order, [new, old], "newer record wins an equal-relevance tie")

    def test_kind_weight_breaks_tie(self):
        note = self.seed("acme thing one", kind="note")
        fact = self.seed("acme thing two", kind="fact")
        ts = "2026-03-01T00:00:00+00:00"
        self.set_recorded_at(note, ts)
        self.set_recorded_at(fact, ts)  # equal recency + equal relevance -> kind decides
        order = [h["id"] for h in self.cli_json("recall", "--query", "acme")["candidates"]]
        self.assertEqual(order[0], fact, "fact outranks note at equal relevance/recency")

    def test_dropped_excluded(self):
        self.cli("--agent", "a", "add", "--text", "obsolete acme idea")
        iid = self.cli_json("read")["active"][0]["id"]
        self.cli("compact", "--drop", iid, "--reason", "obsolete")
        self.assertEqual(self.cli_json("recall", "--query", "acme")["candidates"], [],
                         "dropped records must not resurface in query results")

    def test_like_fallback_when_fts_unavailable(self):
        self.seed("Alice handles billing", tags=["finance"])
        orig = memory._fts_ranked
        memory._fts_ranked = lambda *a, **k: None  # simulate no FTS5
        try:
            hits = self.cli_json("recall", "--query", "billing")["candidates"]
        finally:
            memory._fts_ranked = orig
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["why"], "matches 'billing'")


class Brief(MemTest):
    def test_shape_without_query(self):
        self.cli("--agent", "a", "add", "--text", "active one")
        out = self.cli_json("--agent", "a", "brief")
        self.assertEqual(set(out), {"agent", "profile", "active", "due"})
        self.assertEqual(len(out["active"]), 1)

    def test_query_adds_matches_and_dedupes_due(self):
        # one archived item is BOTH past-due and a keyword match -> appears once, under due
        self.cli("--agent", "a", "add", "--text", "acme renewal call", "--due", "2020-01-01")
        iid = self.cli_json("read")["active"][0]["id"]
        self.cli("compact", "--archive", iid)
        out = self.cli_json("--agent", "a", "brief", "--query", "acme")
        self.assertIn("matches", out)
        self.assertEqual([h["id"] for h in out["due"]], [iid])
        self.assertNotIn(iid, [h["id"] for h in out["matches"]], "due item not duplicated in matches")

    def test_agent_scope(self):
        self.cli("--agent", "a", "add", "--text", "mine")
        self.cli("--agent", "b", "add", "--text", "theirs")
        out = self.cli_json("--agent", "a", "brief")
        self.assertEqual([i["text"] for i in out["active"]], ["mine"])


class Maintenance(MemTest):
    def test_scan_flags(self):
        self.cli("--agent", "a", "add", "--text", "overdue", "--due", "2020-01-01")
        self.cli("--agent", "a", "add", "--text", "dup")
        self.cli("--agent", "a", "add", "--text", "dup")  # duplicate text
        scan = self.cli_json("scan")
        self.assertTrue(scan["overdue"])
        self.assertTrue(scan["duplicates"])

    def test_validate_fix_repairs(self):
        # write a deliberately broken memory.json (missing id + agent)
        p = os.path.join(self.root, "memory", "memory.json")
        broken = memory.empty_working()
        broken["active"] = [{"text": "no id no agent", "status": "open",
                             "people": [], "tags": []}]
        with open(p, "w", encoding="utf-8") as f:
            json.dump(broken, f)
        self.cli("validate", "--fix")
        item = self.cli_json("read")["active"][0]
        self.assertTrue(item["id"].startswith("m-"))
        self.assertEqual(item["agent"], memory.DEFAULT_AGENT)

    def test_doctor_ok_on_fresh_db(self):
        self.cli("--agent", "a", "record", "--kind", "fact", "--text", "hi")
        doc = self.cli_json("doctor")
        self.assertEqual(doc["integrity_check"], "ok")
        self.assertEqual(doc["invariant_issues"], [])
        self.assertEqual(doc["record_rows"], 1)

    def test_migration_guard_rejects_newer_schema(self):
        p = os.path.join(self.root, "memory", "memory.json")
        future = memory.empty_working()
        future["meta"]["schema_version"] = memory.SCHEMA_VERSION + 1
        with open(p, "w", encoding="utf-8") as f:
            json.dump(future, f)
        with self.assertRaises(SystemExit):
            self.cli("read")


class RichBody(MemTest):
    """goal / outcome / constraints / confidence and the `lesson` kind."""

    def test_lesson_roundtrip_and_recall_on_goal_and_constraints(self):
        self.cli("--agent", "ht", "record", "--kind", "lesson",
                 "--text", "Uploading a blog to HubSpot",
                 "--goal", "publish a draft post programmatically",
                 "--outcome", "worked via POST /cms/v3/blogs/posts",
                 "--constraints", "featured image first", "publish_date is epoch ms",
                 "--confidence", "high")
        # recall matches a term that appears ONLY in the constraints
        on_constraint = self.cli_json("recall", "--query", "epoch")["candidates"]
        self.assertEqual(len(on_constraint), 1)
        hit = on_constraint[0]
        self.assertEqual(hit["kind"], "lesson")
        self.assertEqual(hit["confidence"], "high")
        self.assertEqual(hit["constraints"],
                         ["featured image first", "publish_date is epoch ms"])
        self.assertEqual(hit["goal"], "publish a draft post programmatically")
        # and a term that appears ONLY in the goal
        self.assertTrue(self.cli_json("recall", "--query", "programmatically")["candidates"])

    def test_constraints_are_phrases_not_comma_split(self):
        # a single gotcha may contain a comma; it must stay one constraint
        self.cli("--agent", "a", "record", "--kind", "lesson", "--text", "x",
                 "--constraints", "set publish_date, in epoch ms", "second gotcha")
        hit = self.cli_json("recall", "--query", "publish_date")["candidates"][0]
        self.assertEqual(hit["constraints"], ["set publish_date, in epoch ms", "second gotcha"])

    def test_confidence_validated_and_aliased(self):
        self.cli("--agent", "a", "add", "--text", "t", "--confidence", "med")
        self.assertEqual(self.cli_json("read")["active"][0]["confidence"], "medium")
        with self.assertRaises(SystemExit):
            self.cli("--agent", "a", "record", "--kind", "fact", "--text", "y",
                     "--confidence", "superhigh")

    def test_confidence_weights_fusion(self):
        lo = self.id_by_text  # alias for brevity
        self.cli("--agent", "a", "record", "--kind", "fact", "--text", "acme one",
                 "--confidence", "low")
        self.cli("--agent", "a", "record", "--kind", "fact", "--text", "acme two",
                 "--confidence", "high")
        low_id, high_id = lo("acme one"), lo("acme two")
        ts = "2026-03-01T00:00:00+00:00"
        self.set_recorded_at(low_id, ts)
        self.set_recorded_at(high_id, ts)  # equal relevance + recency + kind -> confidence decides
        order = [h["id"] for h in self.cli_json("recall", "--query", "acme")["candidates"]]
        self.assertEqual(order[0], high_id, "high confidence outranks low, all else equal")

    def test_lesson_fields_survive_archive_and_resurface(self):
        self.cli("--agent", "a", "add", "--text", "build the thing",
                 "--goal", "ship v2", "--constraints", "needs review", "--confidence", "medium")
        iid = self.cli_json("read")["active"][0]["id"]
        self.cli("compact", "--archive", iid)
        self.cli("resurface", "--id", iid)
        item = self.cli_json("read")["active"][0]
        self.assertEqual(item["goal"], "ship v2")
        self.assertEqual(item["constraints"], ["needs review"])
        self.assertEqual(item["confidence"], "medium")

    def test_confidence_basis_surfaces_in_recall(self):
        self.cli("--agent", "a", "record", "--kind", "fact",
                 "--text", "User is founder of Synaptic Labs", "--tags", "employer",
                 "--confidence", "low", "--source", "mentioned once",
                 "--verify", "confirm via the HubSpot contact record",
                 "--unknowns", "whether the title is still current")
        hit = self.cli_json("recall", "--query", "synaptic", "--no-reinforce")["candidates"][0]
        self.assertEqual(hit["confidence"], "low")
        self.assertEqual(hit["source"], "mentioned once")
        self.assertEqual(hit["verify"], "confirm via the HubSpot contact record")
        self.assertEqual(hit["unknowns"], "whether the title is still current")

    def test_verify_text_is_searchable(self):
        # The upgrade path lives only in --verify; a query on its words must find the record.
        self.cli("--agent", "a", "record", "--kind", "fact", "--text", "User is founder of X",
                 "--verify", "confirm via the HubSpot contact record")
        hits = self.cli_json("recall", "--query", "hubspot contact", "--no-reinforce")["candidates"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["text"], "User is founder of X")

    def test_basis_omitted_keys_absent(self):
        self.cli("--agent", "a", "record", "--kind", "note", "--text", "plain note")
        hit = self.cli_json("recall", "--query", "plain", "--no-reinforce")["candidates"][0]
        self.assertNotIn("verify", hit)
        self.assertNotIn("unknowns", hit)
        self.assertNotIn("source", hit)

    def test_basis_on_active_item_survives_archive_resurface(self):
        self.cli("--agent", "a", "add", "--text", "verify the SSO claim",
                 "--confidence", "low", "--verify", "ask IT", "--unknowns", "which IdP")
        iid = self.cli_json("read")["active"][0]["id"]
        self.cli("compact", "--archive", iid)
        self.cli("resurface", "--id", iid)
        item = self.cli_json("read")["active"][0]
        self.assertEqual(item["verify"], "ask IT")
        self.assertEqual(item["unknowns"], "which IdP")

    def test_migration_from_old_schema_preserves_data_and_allows_lesson(self):
        # Build a db with the pre-2.1 schema: restrictive kind CHECK, no new columns.
        db = os.path.join(self.root, "memory", "longterm.db")
        con = sqlite3.connect(db)
        con.executescript(
            "CREATE TABLE record (id TEXT PRIMARY KEY, agent TEXT,"
            " kind TEXT NOT NULL CHECK(kind IN ('item','decision','note','fact')),"
            " type TEXT, text TEXT NOT NULL, people TEXT, tags TEXT, owner TEXT, due TEXT,"
            " status TEXT CHECK(status IN ('open','done','deferred','dropped')), source TEXT,"
            " created_at TEXT, recorded_at TEXT, reason TEXT, rationale TEXT);"
            "CREATE TABLE changelog (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,"
            " agent TEXT, action TEXT, item_id TEXT, summary TEXT);")
        con.execute("INSERT INTO record(id,agent,kind,text,recorded_at) "
                    "VALUES('m-legacy01','old','fact','legacy fact','2026-01-01T00:00:00+00:00')")
        con.commit()
        con.close()
        # New code must migrate on connect: accept a lesson AND keep the legacy row.
        self.cli("--agent", "a", "record", "--kind", "lesson", "--text", "post-migration lesson")
        doc = self.cli_json("doctor")
        self.assertEqual(doc["integrity_check"], "ok")
        self.assertEqual(doc["invariant_issues"], [])
        self.assertEqual(doc["record_rows"], 2)
        kinds = {r["kind"] for r in self.cli_json("export")["record"]}
        self.assertEqual(kinds, {"fact", "lesson"})


class Graph(MemTest):
    """Co-recall affinity edges, spreading activation, decay, and chopping-block."""

    def rec(self, text, kind="note", agent="a"):
        self.cli("--agent", agent, "record", "--kind", kind, "--text", text)
        return self.id_by_text(text)

    def set_edge_updated(self, ts):
        con = self.db()
        con.execute("UPDATE edge SET updated_at=?", (ts,))
        con.commit()
        con.close()

    def set_last_used(self, rid, ts):
        con = self.db()
        con.execute("UPDATE record SET last_used=? WHERE id=?", (ts, rid))
        con.commit()
        con.close()

    def links_of(self, rid):
        return {l["id"]: l for l in self.cli_json("links", "--id", rid)["links"]}

    def test_reinforce_builds_weighted_edges(self):
        a, b, g = self.rec("alpha"), self.rec("beta"), self.rec("gamma")
        self.cli("reinforce", "--ids", a, b, g)   # a-b, a-g, b-g each +1
        self.cli("reinforce", "--ids", a, g)       # a-g +1 more
        la = self.links_of(a)
        self.assertGreater(la[g]["weight"], la[b]["weight"], "a-g co-used more than a-b")
        self.assertEqual(la[g]["count"], 2)
        self.assertEqual(la[b]["count"], 1)

    def test_reinforce_requires_two_ids(self):
        a = self.rec("solo")
        with self.assertRaises(SystemExit):
            self.cli("reinforce", "--ids", a)

    def test_spreading_activation_pulls_linked_neighbor(self):
        seed = self.rec("alpha apple")        # only this matches 'apple'
        neigh = self.rec("gamma grape")
        self.cli("link", "--a", seed, "--b", neigh)
        hits = self.cli_json("recall", "--query", "apple")["candidates"]
        ids = [h["id"] for h in hits]
        self.assertEqual(ids[0], seed, "the direct text match (hub) leads")
        self.assertIn(neigh, ids, "a linked neighbour surfaces on a query it doesn't match")
        whys = {h["id"]: h["why"] for h in hits}
        self.assertEqual(whys[neigh], "linked to a match")

    def test_no_edges_means_recall_unchanged(self):
        self.rec("alpha apple")
        self.rec("beta banana")
        hits = self.cli_json("recall", "--query", "apple")["candidates"]
        self.assertEqual([h["text"] for h in hits], ["alpha apple"])

    def test_manual_link_and_unlink(self):
        a, b = self.rec("one"), self.rec("two")
        self.cli("link", "--a", a, "--b", b, "--weight", "4")
        self.assertEqual(self.links_of(a)[b]["weight"], 4.0)
        self.cli("unlink", "--a", a, "--b", b)
        self.assertEqual(self.links_of(a), {})

    def test_link_rejects_unknown_ids(self):
        a = self.rec("real")
        with self.assertRaises(SystemExit):
            self.cli("link", "--a", a, "--b", "m-deadbeef")

    def test_edge_weight_decays_over_time(self):
        a, b = self.rec("one"), self.rec("two")
        self.cli("link", "--a", a, "--b", b, "--weight", "4")
        self.set_edge_updated("2020-01-01T00:00:00+00:00")  # ancient -> decays toward 0
        self.assertLess(self.links_of(a)[b]["weight"], 0.01)

    def test_recall_reinforces_by_default(self):
        a = self.rec("acme alpha")
        b = self.rec("acme beta")
        self.set_last_used(a, "2020-01-01T00:00:00+00:00")
        self.cli("recall", "--query", "acme")   # reinforce is the DEFAULT now
        # an edge now exists between the two co-returned records...
        self.assertIn(b, self.links_of(a))
        # ...and the stale clock was reset to today
        con = self.db()
        lu = con.execute("SELECT last_used FROM record WHERE id=?", (a,)).fetchone()[0]
        con.close()
        self.assertTrue(lu.startswith(memory.utc_today()[:7]), "last_used reset to now")

    def test_recall_no_reinforce_stays_read_only(self):
        a = self.rec("acme alpha")
        self.rec("acme beta")
        self.set_last_used(a, "2020-01-01T00:00:00+00:00")
        self.cli("recall", "--query", "acme", "--no-reinforce")
        self.assertEqual(self.links_of(a), {}, "--no-reinforce creates no edges")
        con = self.db()
        lu = con.execute("SELECT last_used FROM record WHERE id=?", (a,)).fetchone()[0]
        con.close()
        self.assertTrue(lu.startswith("2020-01"), "--no-reinforce leaves last_used untouched")

    def test_scan_flags_dormant_unconnected_and_spares_connected(self):
        dormant = self.rec("dormant fact", kind="fact")
        anchor = self.rec("anchor fact", kind="fact")
        self.set_last_used(dormant, "2020-01-01T00:00:00+00:00")
        flagged = [x["id"] for x in self.cli_json("scan")["stale_longterm"]]
        self.assertIn(dormant, flagged)
        # wire it into a live cluster, re-age it -> spared
        self.cli("link", "--a", dormant, "--b", anchor)
        self.set_last_used(dormant, "2020-01-01T00:00:00+00:00")
        flagged = [x["id"] for x in self.cli_json("scan")["stale_longterm"]]
        self.assertNotIn(dormant, flagged, "a well-connected record is spared the chopping block")

    def test_forget_retires_record_and_drops_edges(self):
        a, b = self.rec("forget me"), self.rec("keep me")
        self.cli("link", "--a", a, "--b", b)
        self.cli("forget", "--ids", a)
        self.assertEqual(self.cli_json("recall", "--query", "forget")["candidates"], [],
                         "forgotten record is excluded from recall")
        self.assertEqual(self.links_of(b), {}, "edges to a forgotten record are dropped")
        doc = self.cli_json("doctor")
        self.assertEqual(doc["invariant_issues"], [])

    def test_resurface_clears_edges(self):
        self.cli("--agent", "a", "add", "--text", "archive me")
        iid = self.cli_json("read")["active"][0]["id"]
        other = self.rec("neighbor")
        self.cli("compact", "--archive", iid)
        self.cli("link", "--a", iid, "--b", other)
        self.cli("resurface", "--id", iid)
        self.assertEqual(self.links_of(other), {}, "resurfacing clears the record's edges")
        self.assertEqual(self.cli_json("doctor")["invariant_issues"], [])


class SimilarityCheck(MemTest):
    """Write-time duplicate/contradiction signal (check verb + record advisory)."""

    def seed(self, text, kind="fact", agent="a", **kw):
        argv = ["--agent", agent, "record", "--kind", kind, "--text", text]
        for k, v in kw.items():
            argv += [f"--{k}", *(v if isinstance(v, list) else [v])]
        self.cli(*argv)
        return self.id_by_text(text)

    def test_check_flags_duplicate(self):
        self.seed("The user works at Synaptic Labs as founder",
                  tags=["employer"], confidence="high")
        d = self.cli_json("check", "--kind", "fact",
                           "--text", "User works at Synaptic Labs as the founder",
                           "--tags", "employer")
        self.assertTrue(d["has_duplicate"])
        self.assertTrue(d["similar"][0]["duplicate"])

    def test_check_flags_conflict_on_high_confidence(self):
        self.seed("The user works at Synaptic Labs as founder",
                  tags=["employer"], confidence="high")
        d = self.cli_json("check", "--kind", "fact",
                           "--text", "The user now works at OpenAI instead",
                           "--tags", "employer")
        self.assertFalse(d["has_duplicate"])
        self.assertTrue(d["has_conflict"], "same-subject high-confidence fact is a conflict")

    def test_low_confidence_existing_is_not_a_conflict(self):
        self.seed("The user works at Synaptic Labs as founder",
                  tags=["employer"], confidence="low")
        d = self.cli_json("check", "--kind", "fact",
                           "--text", "The user now works at OpenAI instead",
                           "--tags", "employer")
        self.assertFalse(d["has_conflict"], "only high-confidence records warrant a conflict flag")

    def test_check_unrelated_is_quiet(self):
        self.seed("The user works at Synaptic Labs as founder", tags=["employer"])
        d = self.cli_json("check", "--kind", "note",
                          "--text", "Water the office plants on fridays")
        self.assertEqual(d["similar"], [])
        self.assertFalse(d["has_duplicate"] or d["has_conflict"])

    def test_check_is_read_only(self):
        self.seed("a durable fact", tags=["x"])
        before = self.cli_json("export")["record"]
        self.cli_json("check", "--kind", "fact", "--text", "a durable fact", "--tags", "x")
        after = self.cli_json("export")["record"]
        self.assertEqual(len(before), len(after), "check must not write a record")

    def test_record_advisory_warns_on_duplicate(self):
        self.seed("The user works at Synaptic Labs as founder", tags=["employer"])
        out = self.cli("--agent", "a", "record", "--kind", "fact",
                       "--text", "User works at Synaptic Labs as the founder",
                       "--tags", "employer")
        self.assertIn("possible duplicate", out)
        self.assertIn("recorded fact", out, "the write still succeeds (advisory is non-blocking)")

    def test_dropped_records_are_not_surfaced(self):
        rid = self.seed("The user works at Synaptic Labs as founder",
                        tags=["employer"], confidence="high")
        self.cli("forget", "--ids", rid)
        d = self.cli_json("check", "--kind", "fact",
                          "--text", "User works at Synaptic Labs as the founder",
                          "--tags", "employer")
        self.assertEqual(d["similar"], [], "a forgotten record is not a duplicate target")


class VerifyLoop(MemTest):
    """scan surfaces relied-on-but-unverified records; reconfirm closes the loop."""

    def seed(self, text, **kw):
        argv = ["--agent", "a", "record", "--kind", "fact", "--text", text]
        for k, v in kw.items():
            argv += [f"--{k}", v]
        self.cli(*argv)
        return self.id_by_text(text)

    def test_scan_flags_unverified_below_high(self):
        self.seed("low with path", confidence="low", verify="ask the user")
        self.seed("medium with path", confidence="medium", verify="check the PDF")
        self.seed("high with path", confidence="high", verify="already done")
        self.seed("low no path", confidence="low")
        u = self.cli_json("scan")["unverified"]
        ids = {x["id"] for x in u}
        self.assertIn(self.id_by_text("low with path"), ids)
        self.assertIn(self.id_by_text("medium with path"), ids)
        self.assertNotIn(self.id_by_text("high with path"), ids, "high confidence isn't unverified")
        self.assertNotIn(self.id_by_text("low no path"), ids, "no verify path → nothing to do")

    def test_reconfirm_raises_and_appends_source(self):
        rid = self.seed("founder claim", confidence="low", source="mentioned once",
                        verify="confirm via HubSpot")
        self.cli("reconfirm", "--id", rid, "--confidence", "high",
                 "--source", "confirmed in HubSpot")
        hit = self.cli_json("recall", "--query", "founder", "--no-reinforce")["candidates"][0]
        self.assertEqual(hit["confidence"], "high")
        self.assertEqual(hit["source"], "mentioned once; confirmed in HubSpot")
        self.assertEqual(hit.get("verify"), "confirm via HubSpot", "verify kept unless cleared")

    def test_reconfirm_clear_verify_removes_from_scan(self):
        rid = self.seed("x", confidence="low", verify="ask")
        self.assertIn(rid, {u["id"] for u in self.cli_json("scan")["unverified"]})
        self.cli("reconfirm", "--id", rid, "--confidence", "high", "--verify", "")
        self.assertEqual(self.cli_json("scan")["unverified"], [])
        hit = self.cli_json("recall", "--query", "x", "--no-reinforce")["candidates"][0]
        self.assertNotIn("verify", hit)

    def test_reconfirm_replace_source(self):
        rid = self.seed("y", confidence="low", source="old basis", verify="ask")
        self.cli("reconfirm", "--id", rid, "--source", "new basis", "--replace-source")
        hit = self.cli_json("recall", "--query", "y", "--no-reinforce")["candidates"][0]
        self.assertEqual(hit["source"], "new basis")

    def test_reconfirm_can_lower_confidence(self):
        rid = self.seed("shaky", confidence="high", verify="re-check")
        self.cli("reconfirm", "--id", rid, "--confidence", "low",
                 "--source", "disconfirming evidence found")
        hit = self.cli_json("recall", "--query", "shaky", "--no-reinforce")["candidates"][0]
        self.assertEqual(hit["confidence"], "low")

    def test_reconfirm_logs_the_change(self):
        rid = self.seed("z", confidence="low", verify="ask")
        self.cli("reconfirm", "--id", rid, "--confidence", "medium", "--source", "found a doc")
        actions = [r for r in self.cli_json("export")["changelog"] if r["action"] == "reconfirm"]
        self.assertEqual(len(actions), 1)
        self.assertIn("low→medium", actions[0]["summary"])

    def test_reconfirm_errors(self):
        rid = self.seed("w", confidence="low", verify="ask")
        with self.assertRaises(SystemExit):
            self.cli("reconfirm", "--id", rid)  # nothing to change
        with self.assertRaises(SystemExit):
            self.cli("reconfirm", "--id", "m-nope", "--confidence", "high")  # unknown id
        self.cli("forget", "--ids", rid)
        with self.assertRaises(SystemExit):
            self.cli("reconfirm", "--id", rid, "--confidence", "high")  # dropped


class Dream(MemTest):
    """Consolidation: dream surfaces dense clusters + persona material; consolidate
    folds atoms under an insight (kept as evidence, one hop behind the summary)."""

    def rec(self, text, kind="fact", agent="a", **kw):
        argv = ["--agent", agent, "record", "--kind", kind, "--text", text]
        for k, v in kw.items():
            argv += [f"--{k}", *(v if isinstance(v, list) else [v])]
        self.cli(*argv)
        return self.id_by_text(text)

    def consolidated_into(self, rid):
        con = self.db()
        row = con.execute("SELECT consolidated_into FROM record WHERE id=?", (rid,)).fetchone()
        con.close()
        return row[0] if row else None

    def cluster_of_three(self):
        a = self.rec("user prefers dark mode", tags="ui")
        b = self.rec("user prefers terse replies", tags="style")
        c = self.rec("user prefers async comms", tags="comms")
        self.cli("reinforce", "--ids", a, b, c)  # wire them into one cluster
        return a, b, c

    def test_dream_surfaces_dense_cluster(self):
        a, b, c = self.cluster_of_three()
        clusters = self.cli_json("dream")["clusters"]
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["size"], 3)
        self.assertEqual({m["id"] for m in clusters[0]["members"]}, {a, b, c})

    def test_dream_ignores_sparse_pairs(self):
        a = self.rec("lonely one")
        b = self.rec("lonely two")
        self.cli("reinforce", "--ids", a, b)  # a pair is below DREAM_MIN_CLUSTER (3)
        self.assertEqual(self.cli_json("dream")["clusters"], [])

    def test_dream_persona_surfaces_high_confidence_facts(self):
        self.rec("user founded Synaptic Labs", confidence="high")
        self.rec("a low-confidence guess", confidence="low")
        self.rec("a plain note", kind="note")
        persona = self.cli_json("dream")["persona"]
        texts = [f["text"] for f in persona["high_confidence_facts"]]
        self.assertEqual(texts, ["user founded Synaptic Labs"])
        self.assertEqual(persona["fact_count"], 1)
        self.assertIn("profile", persona)

    def test_consolidate_creates_insight_and_folds_atoms(self):
        a, b, c = self.cluster_of_three()
        out = self.cli("--agent", "a", "consolidate", "--from", a, b, c,
                       "--text", "user prefers a minimal dark async UX",
                       "--tags", "preferences", "--confidence", "high")
        self.assertIn("consolidated 3", out)
        insight = self.id_by_text("user prefers a minimal dark async UX")
        con = self.db()
        kind = con.execute("SELECT kind FROM record WHERE id=?", (insight,)).fetchone()[0]
        con.close()
        self.assertEqual(kind, "insight")
        for atom in (a, b, c):
            self.assertEqual(self.consolidated_into(atom), insight)
        self.assertIsNone(self.consolidated_into(insight))  # the summary isn't itself folded

    def test_insight_is_primary_hit_and_atoms_drill_down(self):
        a, b, c = self.cluster_of_three()
        self.cli("--agent", "a", "consolidate", "--from", a, b, c,
                 "--text", "user prefers a minimal dark async UX", "--tags", "preferences")
        # 'dark' appears in both the summary and a folded atom: the insight leads,
        # the atoms surface one hop behind as evidence.
        hits = self.cli_json("recall", "--query", "dark", "--no-reinforce")["candidates"]
        self.assertEqual(hits[0]["kind"], "insight", "the summary is the primary hit")
        self.assertEqual(hits[0]["why"], "matches dark")
        whys = {h["id"]: h["why"] for h in hits}
        self.assertIn(a, whys)
        self.assertEqual(whys[a], "linked to a match", "folded atom surfaces as evidence")

    def test_folded_atom_hidden_from_primary_recall(self):
        a, b, c = self.cluster_of_three()
        self.cli("--agent", "a", "consolidate", "--from", a, b, c,
                 "--text", "user prefers a minimal dark UX", "--tags", "preferences")
        # 'terse' lives only in a folded atom and NOT in the summary -> no primary hit,
        # and nothing else matched to pull it in via the graph.
        self.assertEqual(self.cli_json("recall", "--query", "terse", "--no-reinforce")["candidates"], [],
                         "a folded atom is not a primary recall hit on its own words")

    def test_consolidated_cluster_not_re_surfaced_by_dream(self):
        a, b, c = self.cluster_of_three()
        self.cli("--agent", "a", "consolidate", "--from", a, b, c, "--text", "rolled up")
        self.assertEqual(self.cli_json("dream")["clusters"], [],
                         "an already-consolidated cluster is not proposed again")

    def test_scan_skips_consolidated_atoms(self):
        a, b, c = self.cluster_of_three()
        self.cli("--agent", "a", "consolidate", "--from", a, b, c, "--text", "rolled up")
        # age a folded atom into the chopping-block window; it must not be flagged —
        # it is retained as evidence under its summary.
        con = self.db()
        con.execute("UPDATE record SET last_used=? WHERE id=?",
                    ("2020-01-01T00:00:00+00:00", a))
        con.commit()
        con.close()
        flagged = [x["id"] for x in self.cli_json("scan")["stale_longterm"]]
        self.assertNotIn(a, flagged)

    def test_consolidate_rejects_bad_and_double(self):
        a, b, c = self.cluster_of_three()
        with self.assertRaises(SystemExit):  # unknown id
            self.cli("consolidate", "--from", a, "m-deadbeef", "--text", "x")
        with self.assertRaises(SystemExit):  # fewer than two
            self.cli("consolidate", "--from", a, "--text", "x")
        self.cli("--agent", "a", "consolidate", "--from", a, b, c, "--text", "rolled up")
        with self.assertRaises(SystemExit):  # already consolidated
            self.cli("consolidate", "--from", a, b, "--text", "again")

    def test_consolidate_logs_event(self):
        a, b, c = self.cluster_of_three()
        self.cli("--agent", "a", "consolidate", "--from", a, b, c, "--text", "rolled up")
        actions = [r for r in self.cli_json("export")["changelog"] if r["action"] == "consolidate"]
        self.assertEqual(len(actions), 1)
        self.assertIn("folded 3", actions[0]["summary"])

    def test_dream_suggests_keywords_from_members(self):
        a, b, c = self.cluster_of_three()
        cluster = self.cli_json("dream")["clusters"][0]
        kws = cluster["suggested_keywords"]
        # curated tags are always kept, and distinctive text words are pulled in
        for t in ("ui", "style", "comms"):
            self.assertIn(t, kws)
        self.assertIn("terse", kws, "a distinctive text-only word is suggested")
        self.assertNotIn("prefers", kws, "stopwords are filtered out")

    def test_consolidate_inherits_source_tags_and_people(self):
        a = self.rec("alice owns billing", tags="finance", people="Alice")
        b = self.rec("alice approves invoices", tags="approvals", people="Alice")
        c = self.rec("billing runs monthly", tags="cadence")
        self.cli("reinforce", "--ids", a, b, c)
        self.cli("--agent", "a", "consolidate", "--from", a, b, c,
                 "--text", "Alice owns the monthly billing/approvals flow", "--tags", "billing")
        rec = [r for r in self.cli_json("export")["record"] if r["kind"] == "insight"][0]
        # agent-supplied keyword first, then the union of the atoms' tags
        self.assertEqual(rec["tags"], '["billing", "finance", "approvals", "cadence"]')
        self.assertEqual(rec["people"], '["Alice"]', "people are unioned and de-duplicated")

    def test_keyword_tag_resurfaces_folded_topic(self):
        # The agent pulls a text-only keyword ('async') from an atom and tags the insight
        # with it, so the folded topic still surfaces — the summary, as a primary hit.
        a = self.rec("user prefers dark mode", tags="ui")
        b = self.rec("user prefers async communication", tags="comms")
        c = self.rec("user prefers short replies", tags="style")
        self.cli("reinforce", "--ids", a, b, c)
        self.cli("--agent", "a", "consolidate", "--from", a, b, c,
                 "--text", "user UX preferences", "--tags", "async")
        hits = self.cli_json("recall", "--query", "async", "--no-reinforce")["candidates"]
        self.assertTrue(hits)
        self.assertEqual(hits[0]["kind"], "insight")
        self.assertEqual(hits[0]["why"], "matches async",
                         "an inherited/added keyword makes the folded topic a primary hit")

    def test_consolidate_survives_doctor(self):
        a, b, c = self.cluster_of_three()
        self.cli("--agent", "a", "consolidate", "--from", a, b, c, "--text", "rolled up")
        doc = self.cli_json("doctor")
        self.assertEqual(doc["integrity_check"], "ok")
        self.assertEqual(doc["invariant_issues"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
