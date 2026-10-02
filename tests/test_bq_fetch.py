"""Offline tests for the BigQuery fetch path. No network: the Algolia client is stubbed.

    python -m unittest discover tests
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loppan import bq_fetch, bq_schema  # noqa: E402

HIT = {
    "objectID": "abc123",
    "isForSale": True, "lastChance": False, "favouriteCount": 19, "p2p": False,
    "price_SE": {"amount": 67500}, "priceDrop_SE": {"oldPrice": {"amount": 70500}},
    "weight": 4.35, "firstOfferedAt_SE": 1788542073492,
    "brandClassification": {"pricePoint": 3},
    "categories": {"lvl1": ["Kvinna > Kläder"],
                   "lvl2": ["Kvinna > Kläder > Sovplagg & Morgonrockar"]},
    "metadata": {"brand": "Some Brand", "type": "Morgonrock", "size": "WMN-INT-XL",
                 "condition": "Nytt", "demography": "Kvinna", "pattern": "Enfärgat",
                 "material": ["Silke"], "color": ["Grå"], "season": ["Vår", "Höst"],
                 "defects": [{"type": "Fläck"}]},
}


class Schema(unittest.TestCase):
    def test_contract_tables_parse(self):
        s = bq_schema.load()
        cols = {c.name: c for c in s["sweep_staging"]}
        self.assertEqual(cols["run_date"].type, "DATE")
        self.assertEqual(cols["run_date"].mode, "REQUIRED")
        self.assertEqual(cols["materials"].mode, "REPEATED")
        self.assertEqual(cols["fetched_at"].type, "TIMESTAMP")
        self.assertEqual([c.name for c in s["adjudication_staging"]],
                         ["run_date", "item_id", "outcome", "final_price_ore", "adjudicated_at"])
        self.assertEqual({c.name: c.type for c in s["runs"]}["completeness"], "FLOAT64")

    def test_rows_carry_exactly_the_schema_columns_in_order(self):
        s = bq_schema.load()
        row = bq_fetch.sweep_row("2026-10-02", "abc123", "census", True, bq_fetch.utc_now(), HIT)
        self.assertEqual(list(row), [c.name for c in s["sweep_staging"]])
        self.assertEqual(bq_schema.row_errors(row, s["sweep_staging"]), [])
        bare = bq_fetch.sweep_row("2026-10-02", "x", "track", False, bq_fetch.utc_now())
        self.assertEqual(bq_schema.row_errors(bare, s["sweep_staging"]), [])

    def test_mapping(self):
        row = bq_fetch.sweep_row("2026-10-02", "abc123", "new", True, "2026-10-02T00:00:00Z", HIT)
        self.assertEqual(row["weight_g"], 4350)            # not 4349
        self.assertEqual(row["season_mask"], 1 | 4)
        self.assertEqual(row["category"], "Kvinna > Kläder > Sovplagg & Morgonrockar")
        self.assertEqual(row["size_code"], "WMN-INT-XL")
        self.assertEqual(row["first_offered"], "2026-09-04")
        self.assertEqual((row["brand_tier"], row["has_defect"]), (3, True))
        self.assertEqual(row["old_price_ore"], 70500)

    def test_validator_catches_wrong_types(self):
        cols = bq_schema.load()["sweep_staging"]
        row = bq_fetch.sweep_row("2026-10-02", "a", "track", True, bq_fetch.utc_now(), HIT)
        for key, bad in [("price_ore", "675"), ("present", None), ("run_date", "2026-10-2"),
                         ("materials", None), ("fetched_at", "2026-10-02T10:00:00"),
                         ("has_defect", 1)]:
            broken = dict(row, **{key: bad})
            self.assertTrue(bq_schema.row_errors(broken, cols), key)
        self.assertTrue(bq_schema.row_errors(dict(row, is_reserved=False), cols))
        del row["brand"]
        self.assertTrue(bq_schema.row_errors(row, cols))


class Track(unittest.TestCase):
    def setUp(self):
        self.real = bq_fetch.algolia.get_objects_parallel

    def tearDown(self):
        bq_fetch.algolia.get_objects_parallel = self.real

    def run_track(self, ids, fail_heads):
        """Stub: chunks whose head is in `fail_heads` error (are not yielded) on the
        first pass; odd-numbered ids are absent from the index."""
        calls = []

        def fake(item_ids, workers=8, attributes=None):
            calls.append(list(item_ids))
            for i in range(0, len(item_ids), 100):
                chunk = item_ids[i:i + 100]
                if len(calls) == 1 and chunk[0] in fail_heads:
                    continue
                yield chunk, [None if int(x[2:]) % 2 else {"objectID": x, "isForSale": True,
                                                           "price_SE": {"amount": 100}}
                              for x in chunk]

        bq_fetch.algolia.get_objects_parallel = fake
        with tempfile.TemporaryDirectory() as d:
            out = bq_fetch.NDJSON(os.path.join(d, "t.ndjson"))
            res = bq_fetch.track(ids, out, "2026-10-02", retries=0)
            out.close()
        return res["run"], calls

    def test_errored_chunk_is_not_fetched_and_not_missing(self):
        ids = [f"id{n}" for n in range(1000)]
        run, _ = self.run_track(ids, fail_heads={"id300"})
        self.assertEqual(run["fetched"], 900)
        self.assertEqual(run["missing"], 450)           # odd ids among the 900 answered
        self.assertAlmostEqual(run["completeness"], 0.9)
        self.assertFalse(run["resolve_allowed"])

    def test_full_answer_allows_resolution(self):
        ids = [f"id{n}" for n in range(250)]
        run, _ = self.run_track(ids, fail_heads=set())
        self.assertEqual((run["fetched"], run["missing"]), (250, 125))
        self.assertTrue(run["resolve_allowed"])


class Floor(unittest.TestCase):
    def test_cli_override_wins_and_default_is_150(self):
        class A:
            min_price_kr = 99.0
        self.assertEqual(bq_fetch.price_floor(A), (99.0, "--min-price-kr"))
        real = bq_fetch.floor_from_bq
        bq_fetch.floor_from_bq = lambda: None
        try:
            A.min_price_kr = None
            self.assertEqual(bq_fetch.price_floor(A), (150.0, "default"))
        finally:
            bq_fetch.floor_from_bq = real

    def test_rows_under_the_floor_are_dropped(self):
        class FakeCrawl:
            def run(self, on_hits):
                cheap = dict(HIT, objectID="cheap", price_SE={"amount": 14900})
                on_hits([HIT, cheap, HIT])
                return {"complete": True}
        with tempfile.TemporaryDirectory() as d:
            out = bq_fetch.NDJSON(os.path.join(d, "c.ndjson"))
            res = bq_fetch.crawl_to_rows(FakeCrawl(), out, "2026-10-02", "census", None, 15000)
            out.close()
        self.assertEqual((out.rows, res["below_floor"], res["duplicates"]), (1, 1, 1))


class NoDatabase(unittest.TestCase):
    def test_fetch_path_never_imports_db(self):
        code = ("import sys; sys.path.insert(0, %r); import loppan.bq_fetch; "
                "print('loppan.db' in sys.modules)" % str(ROOT))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)


if __name__ == "__main__":
    unittest.main()
