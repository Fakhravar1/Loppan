"""Offline tests for the BigQuery fetch path. No network: the Algolia client is stubbed.

    python -m unittest discover tests
"""

import contextlib
import datetime as dt
import io
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


def read_rows(path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def prices_under(rows: list[dict], floor_ore: int) -> list[tuple[str, str, int]]:
    """Every price-like value under the floor, anywhere in any row."""
    return [(r.get("item_id"), k, v) for r in rows for k, v in r.items()
            if "price" in k and isinstance(v, int) and not isinstance(v, bool)
            and v < floor_ore]


class Schema(unittest.TestCase):
    def test_contract_tables_parse(self):
        s = bq_schema.load()
        cols = {c.name: c for c in s["sweep_staging"]}
        self.assertEqual(cols["run_date"].type, "DATE")
        self.assertEqual(cols["run_date"].mode, "REQUIRED")
        self.assertEqual(cols["materials"].mode, "REPEATED")
        self.assertEqual(cols["fetched_at"].type, "TIMESTAMP")
        self.assertEqual([c.name for c in s["adjudication_staging"]],
                         ["run_date", "item_id", "outcome", "final_price_ore", "adjudicated_at",
                          "item_status"])
        self.assertEqual({c.name: c.type for c in s["runs"]}["completeness"], "FLOAT64")

    def test_real_schema_alters_add_their_columns(self):
        """The ALTERs appended to schema.sql reach validate: each new column is
        last, nullable, of the declared type."""
        s = bq_schema.load()
        for table, name, typ in [("adjudication_staging", "item_status", "STRING"),
                                 ("circle_origin_staging", "bought_on", "DATE"),
                                 ("sweep_staging", "below_floor", "BOOL")]:
            col = s[table][-1]
            self.assertEqual((col.name, col.type, col.mode), (name, typ, "NULLABLE"), table)
            self.assertEqual([c.name for c in s[table]].count(name), 1, table)
        # ALTERs on other tables touch nothing here: items is parsed, but not altered.
        self.assertNotIn("first_price_ore", [c.name for c in s["items"]])

    def parse(self, extra: str) -> dict:
        sql = ("CREATE TABLE IF NOT EXISTS loppan.items (item_id STRING NOT NULL);\n" +
               "".join(f"CREATE TABLE IF NOT EXISTS loppan.{t} (run_date DATE NOT NULL);\n"
                       for t in bq_schema.CONTRACT) + extra)
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d, "schema.sql")
            p.write_text(sql, encoding="utf-8")
            return bq_schema.load(p)

    def test_alter_subset(self):
        s = self.parse("""
            ALTER TABLE loppan.runs ADD COLUMN IF NOT EXISTS note STRING
              OPTIONS (description = "a; b, c > d -- not a comment"),
              ADD COLUMN tags ARRAY<STRUCT<k STRING, v INT64>>;
            ALTER TABLE loppan.runs ADD COLUMN IF NOT EXISTS note STRING;  -- no-op
            ALTER TABLE loppan.runs ALTER COLUMN note SET OPTIONS (description = 'x');
            ALTER TABLE loppan.runs SET OPTIONS (description = 'y');
            ALTER TABLE loppan.items ADD COLUMN IF NOT EXISTS weird GEOGRAPHY;
            ALTER TABLE loppan.items ALTER COLUMN item_id SET OPTIONS (description = 'z');
            ALTER TABLE loppan.model_params DROP COLUMN value;
            UPDATE loppan.brand_rules SET note = 'a < b; c' WHERE rule = 'min_price_kr';
        """)
        runs = s["runs"]
        self.assertEqual([c.name for c in runs], ["run_date", "note", "tags"])
        self.assertEqual((runs[2].mode, [f.name for f in runs[2].fields]), ("REPEATED", ["k", "v"]))
        self.assertEqual([c.name for c in s["items"]], ["item_id"])

    def test_alter_that_changes_a_contract_column_is_refused(self):
        for bad in ["ALTER TABLE loppan.runs DROP COLUMN run_date;",
                    "ALTER TABLE loppan.sweep_staging RENAME COLUMN run_date TO d;",
                    "ALTER TABLE loppan.runs ADD COLUMN run_date DATE;",
                    "ALTER TABLE loppan.runs ADD COLUMN x GEOGRAPHY;"]:
            with self.assertRaises(ValueError, msg=bad):
                self.parse(bad)

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
    def setUp(self):
        # Never reach the real `bq` CLI from a test: the floor source is the default
        # unless a test says otherwise.
        self.real_bq = bq_fetch.floor_from_bq
        bq_fetch.floor_from_bq = lambda: None

    def tearDown(self):
        bq_fetch.floor_from_bq = self.real_bq

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
        """Census and new: an item under the floor gets no row at all."""
        class FakeCrawl:
            def run(self, on_hits):
                cheap = dict(HIT, objectID="cheap", price_SE={"amount": 14900})
                unpriced = dict(HIT, objectID="unpriced", price_SE={})
                # at the floor, but its old price is under it: kept, old price dropped
                edge = dict(HIT, objectID="edge", price_SE={"amount": 15000},
                            priceDrop_SE={"oldPrice": {"amount": 14000}})
                on_hits([HIT, cheap, HIT, unpriced, edge])
                return {"complete": True}
        for source in ("census", "new"):
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "c.ndjson")
                out = bq_fetch.NDJSON(path)
                res = bq_fetch.crawl_to_rows(FakeCrawl(), out, "2026-10-02", source, None, 15000)
                out.close()
                rows = read_rows(path)
            self.assertEqual((out.rows, res["below_floor"], res["duplicates"]), (2, 2, 1))
            self.assertEqual([r["item_id"] for r in rows], ["abc123", "edge"])
            self.assertEqual((rows[1]["price_ore"], rows[1]["old_price_ore"]), (15000, None))
            self.assertEqual([r["below_floor"] for r in rows], [False, False])
            self.assertEqual(prices_under(rows, 15000), [])

    def run_track_cli(self, answers: dict, *args) -> tuple[list[dict], dict]:
        """bq_fetch.py track end to end, on a stub index: id -> hit (None = absent)."""
        def fake(item_ids, workers=8, attributes=None):
            for i in range(0, len(item_ids), 100):
                chunk = item_ids[i:i + 100]
                yield chunk, [answers[x] and dict(answers[x], objectID=x) for x in chunk]
        real = bq_fetch.algolia.get_objects_parallel
        bq_fetch.algolia.get_objects_parallel = fake
        try:
            with tempfile.TemporaryDirectory() as d:
                ids, out, summary = (os.path.join(d, n) for n in ("ids.txt", "t.ndjson", "s.json"))
                pathlib.Path(ids).write_text("\n".join(answers) + "\n", encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    code = bq_fetch.main(["track", "--ids", ids, "--out", out,
                                          "--summary", summary, *args])
                self.assertEqual(code, 0)
                v = bq_schema.validate_file(pathlib.Path(out), "sweep_staging")
                self.assertTrue(v["ok"], v["errors"])
                return read_rows(out), json.loads(pathlib.Path(summary).read_text("utf-8"))
        finally:
            bq_fetch.algolia.get_objects_parallel = real

    def test_track_flags_a_markdown_under_the_floor_without_its_price(self):
        state = {"isForSale": True, "lastChance": True, "favouriteCount": 7}
        rows, summary = self.run_track_cli({
            "under": dict(state, price_SE={"amount": 14900},
                          priceDrop_SE={"oldPrice": {"amount": 20000}}),
            "at": dict(state, price_SE={"amount": 15000},
                       priceDrop_SE={"oldPrice": {"amount": 14000}}),
            "over": dict(state, price_SE={"amount": 30000},
                         priceDrop_SE={"oldPrice": {"amount": 35000}}),
            "gone": None,
            "unpriced": dict(state, price_SE={}),
        }, "--min-price-kr", "150")
        by_id = {r["item_id"]: r for r in rows}
        under = by_id["under"]
        self.assertEqual((under["present"], under["below_floor"], under["price_ore"],
                          under["old_price_ore"]), (True, True, None, None))
        self.assertEqual((under["favourites"], under["last_chance"], under["is_for_sale"]),
                         (7, True, True))
        self.assertEqual((by_id["at"]["price_ore"], by_id["at"]["old_price_ore"],
                          by_id["at"]["below_floor"]), (15000, None, False))
        self.assertEqual((by_id["over"]["price_ore"], by_id["over"]["old_price_ore"],
                          by_id["over"]["below_floor"]), (30000, 35000, False))
        self.assertEqual((by_id["gone"]["present"], by_id["gone"]["below_floor"]), (False, False))
        self.assertEqual((by_id["unpriced"]["price_ore"], by_id["unpriced"]["below_floor"]),
                         (None, False))
        self.assertEqual(summary["measured"]["below_floor"], 1)
        self.assertEqual(summary["min_price_kr"], {"value": 150.0, "source": "--min-price-kr"})

    def test_track_output_holds_no_price_under_15000_ore(self):
        """Scan the NDJSON itself: every price-like value, on every row."""
        answers = {f"id{n}": {"isForSale": True, "price_SE": {"amount": 100 * n},
                              "priceDrop_SE": {"oldPrice": {"amount": 100 * n + 5000}}}
                   for n in range(0, 400, 7)}
        rows, summary = self.run_track_cli(answers)        # no bq: the default, 150 kr
        self.assertEqual(summary["min_price_kr"], {"value": 150.0, "source": "default"})
        self.assertEqual(len(rows), len(answers))
        self.assertEqual(prices_under(rows, 15000), [])
        self.assertEqual(sum(r["below_floor"] for r in rows),
                         sum(1 for n in range(0, 400, 7) if 100 * n < 15000))

    def test_track_reads_the_floor_where_census_and_new_do(self):
        answers = {"x": {"isForSale": True, "price_SE": {"amount": 18000}}}
        real = bq_fetch.floor_from_bq
        try:
            bq_fetch.floor_from_bq = lambda: 200.0
            rows, summary = self.run_track_cli(answers)
            self.assertEqual(summary["min_price_kr"]["source"], "loppan.brand_rules.min_price_kr")
            self.assertEqual((rows[0]["below_floor"], rows[0]["price_ore"]), (True, None))
            rows, summary = self.run_track_cli(answers, "--min-price-kr", "150")
            self.assertEqual((rows[0]["below_floor"], rows[0]["price_ore"]), (False, 18000))
        finally:
            bq_fetch.floor_from_bq = real


def run_cli(table: str, *args) -> list[dict]:
    """Run a bq_fetch subcommand that reads --ids and writes --out; validate the
    output against `table` and return its rows. Ids are passed as a list first."""
    ids, *rest = args
    with tempfile.TemporaryDirectory() as d:
        ids_path, out = os.path.join(d, "ids.txt"), os.path.join(d, "out.ndjson")
        pathlib.Path(ids_path).write_text("\n".join(ids) + "\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = bq_fetch.main([*rest, "--ids", ids_path, "--out", out,
                                  "--run-date", "2026-10-02"])
        assert code == 0, code
        v = bq_schema.validate_file(pathlib.Path(out), table)
        assert v["ok"], v["errors"]
        return read_rows(out)


class Parse(unittest.TestCase):
    """The Parse subcommands, on a stubbed client: no request leaves the machine."""

    def setUp(self):
        self.parse = bq_fetch.outcomes.parse
        self.saved = {k: getattr(self.parse, k) for k in ("find", "item", "ladder")}

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.parse, k, v)

    def test_adjudicate_writes_the_raw_item_status_beside_the_outcome(self):
        offers = [("sold", "såld", 249.5), ("paid", "betald", 300), ("gave", "skänkt", 200),
                  ("odd", "reserverad", 450), ("bare", None, 180), ("live", "utlagd", 500)]

        def find(cls, where, limit=None, include=None):
            asked = {p["objectId"] for p in where["item"]["$in"]}
            return [{"item": {"objectId": i, **({"itemStatus": s} if s else {})},
                     "pricing": {"amount": kr}} for i, s, kr in offers if i in asked]

        self.parse.find = find
        rows = run_cli("adjudication_staging",
                       [i for i, _, _ in offers] + ["noOffer"], "adjudicate")
        got = {r["item_id"]: (r["outcome"], r["item_status"], r["final_price_ore"]) for r in rows}
        self.assertEqual(got, {
            "sold": ("sold", "såld", 24950), "paid": ("sold", "betald", 30000),
            "gave": ("expired", "skänkt", 20000), "odd": ("unknown", "reserverad", 45000),
            "bare": ("unknown", None, 18000)})       # still listed and no-offer: no row
        self.assertEqual(list(rows[0])[-1], "item_status")

    def test_origins_write_bought_on(self):
        items = {"linked": {"preceding": {"objectId": "o1"}},
                 "badDay": {"preceding": {"objectId": "o2"}},
                 "noLadder": {"preceding": {"objectId": "o3"}},
                 "unlinked": {}}
        ladders = {
            "o1": [{"pricing": {"amount": 400}},
                   {"pricing": {"amount": 120},
                    "endedAt": {"__type": "Date", "iso": "2026-09-01T10:15:00.000Z"}}],
            "o2": [{"pricing": {"amount": 300}, "endedAt": "not a date"}],
            "o3": []}

        def item(object_id):
            if object_id == "boom":
                raise OSError("connection reset")
            return items[object_id]

        self.parse.item = item
        self.parse.ladder = lambda item_id, region="SE": ladders[item_id]
        with contextlib.redirect_stderr(io.StringIO()):
            rows = run_cli("circle_origin_staging", [*items, "boom"], "origins")
        got = {r["item_id"]: (r["original_id"], r["bought_price_ore"], r["bought_on"])
               for r in rows}
        self.assertEqual(got, {"linked": ("o1", 12000, "2026-09-01"),
                               "badDay": ("o2", 30000, None),
                               "noLadder": ("o3", None, None),
                               "unlinked": (None, None, None)})     # boom: no row
        self.assertEqual(list(rows[0])[-1], "bought_on")


class FakeIndex:
    """A stub search index for brand counting. Facet counts are exact only up to
    `exact_up_to` hits (an estimate, inflated, above it) and the facet list stops at
    bq_brands.FACET_MAX values, as the real index does."""

    def __init__(self, items, exact_up_to):
        self.items, self.exact_up_to, self.calls = items, exact_up_to, 0

    def search(self, filters="", facet_filters=None, hits_per_page=100, **kw):
        import re
        self.calls += 1
        hits = self.items
        for attr, op, v in re.findall(r"([\w.]+)(>=|<)(\d+)", filters):
            key = {"createdAt": "c", "price_SE.amount": "p"}[attr]
            hits = [h for h in hits if (h[key] >= int(v) if op == ">=" else h[key] < int(v))]
        exact = len(hits) <= self.exact_up_to
        counts = {}
        for h in hits:
            if h["b"]:
                counts[h["b"]] = counts.get(h["b"], 0) + (1 if exact else 2)
        top = dict(sorted(counts.items(), key=lambda kv: -kv[1])[:bq_fetch.bq_brands.FACET_MAX])
        return {"nbHits": len(hits), "exhaustiveNbHits": True, "hits": [],
                "exhaustive": {"nbHits": True, "facetsCount": exact},
                "facets": {"metadata.brand": top}}


class Brands(unittest.TestCase):
    def setUp(self):
        self.real = (bq_fetch.bq_shapes.algolia.search, bq_fetch.bq_brands.FACET_MAX,
                     bq_fetch.floor_from_bq)
        bq_fetch.floor_from_bq = lambda: None
        bq_fetch.bq_brands.FACET_MAX = 10

    def tearDown(self):
        (bq_fetch.bq_shapes.algolia.search, bq_fetch.bq_brands.FACET_MAX,
         bq_fetch.floor_from_bq) = self.real

    def test_counts_are_exact_at_or_over_the_floor(self):
        # 60 brands of falling size, one unbranded item in 7, prices either side of 150 kr
        items = [{"c": 1_700_000_000 + 37 * n, "p": 10000 if n % 5 == 0 else 20000,
                  "b": None if n % 7 == 0 else f"B{n % 60 if n % 3 else n % 4:02d}"}
                 for n in range(3000)]
        truth = {}
        for h in items:
            if h["b"] and h["p"] >= 15000:
                truth[h["b"]] = truth.get(h["b"], 0) + 1
        index = FakeIndex(items, exact_up_to=200)
        bq_fetch.bq_shapes.algolia.search = index.search
        with tempfile.TemporaryDirectory() as d:
            out, summary = os.path.join(d, "b.ndjson"), os.path.join(d, "s.json")
            with contextlib.redirect_stdout(io.StringIO()):
                code = bq_fetch.main(["brands", "--out", out, "--summary", summary,
                                      "--run-date", "2026-10-02"])
            self.assertEqual(code, 0)
            v = bq_schema.validate_file(pathlib.Path(out), "brand_counts_staging")
            self.assertTrue(v["ok"], v["errors"])
            rows = read_rows(out)
            s = json.loads(pathlib.Path(summary).read_text("utf-8"))
        self.assertEqual({r["brand"]: r["listings"] for r in rows}, truth)
        self.assertEqual({r["run_date"] for r in rows}, {"2026-10-02"})
        self.assertEqual(s["listings_total"], sum(truth.values()))
        self.assertGreater(len(truth), bq_fetch.bq_brands.FACET_MAX)   # truncation mattered
        self.assertEqual(s["min_price_kr"]["value"], 150.0)

    def test_a_shape_that_cannot_be_counted_exactly_fails(self):
        # every item in the same second: no split helps, so the count is not exact
        items = [{"c": 1_700_000_000, "p": 20000, "b": f"B{n % 30}"} for n in range(500)]
        bq_fetch.bq_shapes.algolia.search = FakeIndex(items, exact_up_to=100).search
        with tempfile.TemporaryDirectory() as d:
            with contextlib.redirect_stdout(io.StringIO()):
                code = bq_fetch.main(["brands", "--out", os.path.join(d, "b.ndjson")])
        self.assertEqual(code, 2)

    def test_unnamed_brand_gets_no_row(self):
        rows = bq_fetch.brand_rows({"A": 3, "": 9, None: 4, "B": 30}, "2026-10-02")
        self.assertEqual([r["brand"] for r in rows], ["B", "A"])


def utc(*args) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.UTC)


def ms(t: dt.datetime) -> int:
    return int(t.timestamp() * 1000)


class Stockholm(unittest.TestCase):
    """Dates are Stockholm days, from the tz database or, without one, the fixed rule.
    Every test runs both ways."""

    def setUp(self):
        self.tz = bq_fetch.STOCKHOLM

    def tearDown(self):
        bq_fetch.STOCKHOLM = self.tz

    def both(self, check):
        for tz in ([self.tz] if self.tz else []) + [None]:
            bq_fetch.STOCKHOLM = tz
            with self.subTest(source="tz database" if tz else "fixed rule"):
                check()

    def test_listing_just_after_local_midnight(self):
        def check():
            for after, before, day in [
                    (utc(2026, 7, 14, 22, 5), utc(2026, 7, 14, 21, 55), "2026-07-15"),  # CEST
                    (utc(2026, 1, 14, 23, 5), utc(2026, 1, 14, 22, 55), "2026-01-15")]:  # CET
                row = bq_fetch.sweep_row("2026-10-02", "a", "new", True, "2026-10-02T00:00:00Z",
                                         dict(HIT, firstOfferedAt_SE=ms(after)))
                self.assertEqual(row["first_offered"], day)
                early = bq_fetch.sweep_row("2026-10-02", "a", "new", True, "2026-10-02T00:00:00Z",
                                           dict(HIT, firstOfferedAt_SE=ms(before)))
                self.assertLess(early["first_offered"], day)
                self.assertEqual(bq_fetch.today_local(after), day)      # run_date
                self.assertEqual(bq_fetch.today_local(before), early["first_offered"])
        self.both(check)

    def test_since_starts_at_stockholm_midnight(self):
        # 2026 changes: summer time from Sunday 29 March, winter time from Sunday 25 October
        def check():
            for day, start in [("2026-07-15", utc(2026, 7, 14, 22)),
                               ("2026-01-15", utc(2026, 1, 14, 23)),
                               ("2026-03-29", utc(2026, 3, 28, 23)),
                               ("2026-03-30", utc(2026, 3, 29, 22)),
                               ("2026-10-25", utc(2026, 10, 24, 22)),
                               ("2026-10-26", utc(2026, 10, 25, 23))]:
                self.assertEqual(bq_fetch.since_ms(day), ms(start), day)
        self.both(check)

    @unittest.skipUnless(bq_fetch.STOCKHOLM, "no tz database here to compare against")
    def test_fixed_rule_matches_the_tz_database(self):
        tz = self.tz
        instants = [utc(2023, 12, 1) + dt.timedelta(hours=h) for h in range(37_000)]
        for y in range(2024, 2028):                # a second either side of each change
            for change in (bq_fetch._last_sunday(y, 3), bq_fetch._last_sunday(y, 10)):
                at = dt.datetime.combine(change, dt.time(1), dt.UTC)
                instants += [at - dt.timedelta(seconds=1), at]
        for t in instants:
            self.assertEqual(bq_fetch._cet_offset(t), t.astimezone(tz).utcoffset(), t)
            bq_fetch.STOCKHOLM = None
            fixed = bq_fetch.local_date(t)
            bq_fetch.STOCKHOLM = tz
            self.assertEqual(fixed, bq_fetch.local_date(t), t)
        day = dt.date(2024, 1, 1)
        while day < dt.date(2028, 1, 1):
            bq_fetch.STOCKHOLM = None
            fixed = bq_fetch.local_midnight(day)
            bq_fetch.STOCKHOLM = tz
            self.assertEqual(fixed, bq_fetch.local_midnight(day), day)
            day += dt.timedelta(days=1)

    def test_timestamps_stay_utc(self):
        self.assertTrue(bq_fetch.utc_now().endswith("Z"))
        dt.datetime.fromisoformat(bq_fetch.utc_now().replace("Z", "+00:00"))


class NoDatabase(unittest.TestCase):
    def test_fetch_path_never_imports_db(self):
        code = ("import sys; sys.path.insert(0, %r); import loppan.bq_fetch; "
                "print('loppan.db' in sys.modules)" % str(ROOT))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)


if __name__ == "__main__":
    unittest.main()
