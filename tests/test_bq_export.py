"""bq_export: typing of the bq CLI's string JSON, and the sold-since-run drop."""

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import bq_export


class Typing(unittest.TestCase):
    def test_strings_become_typed(self):
        row = bq_export._typed({"item_id": "a", "price_ore": "15000", "sell_through": "0.7",
                                "p2p": "true", "history_complete": "false",
                                "peak_month": "1", "expected_profit_ore": None})
        self.assertEqual(row["price_ore"], 15000)
        self.assertEqual(row["sell_through"], 0.7)
        self.assertIs(row["p2p"], True)
        self.assertIs(row["history_complete"], False)
        self.assertEqual(row["peak_month"], 1)
        self.assertIsNone(row["expected_profit_ore"])

    def test_signal_is_carried(self):
        self.assertEqual(bq_export._typed({"item_id": "a", "signal": "now"})["signal"], "now")
        self.assertEqual(bq_export._typed({"item_id": "a", "signal": "season"})["signal"],
                         "season")

    def test_signal_missing_is_none_not_dropped(self):
        row = bq_export._typed({"item_id": "a"})
        self.assertIn("signal", row)
        self.assertIsNone(row["signal"])

    def test_native_json_types_also_work(self):
        row = bq_export._typed({"item_id": "a", "price_ore": 15000, "p2p": True})
        self.assertEqual(row["price_ore"], 15000)
        self.assertIs(row["p2p"], True)


class Export(unittest.TestCase):
    def test_sold_items_dropped_and_swap_called(self):
        cands = [{"item_id": "live", "price_ore": "20000", "signal": "season",
                  "as_of": "2026-10-03"},
                 {"item_id": "sold", "price_ore": "30000", "signal": "now",
                  "as_of": "2026-10-03"}]
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d, "c.json")
            path.write_text(json.dumps(cands), encoding="utf-8")
            images = {"live": ["a/1.jpg"], "sold": None}
            with mock.patch.object(bq_export, "image_paths_for", return_value=images), \
                 mock.patch.object(bq_export.db, "rpc", return_value=1) as rpc, \
                 mock.patch.object(bq_export.db, "upsert", return_value=1) as upsert, \
                 mock.patch.object(sys, "argv", ["bq_export", "--candidates", str(path)]):
                self.assertEqual(bq_export.main(), 0)
        staged = upsert.call_args.args[1]
        self.assertEqual([r["item_id"] for r in staged], ["live"])
        self.assertEqual(staged[0]["image_paths"], ["a/1.jpg"])
        self.assertEqual(staged[0]["signal"], "season")
        self.assertEqual([c.args[0] for c in rpc.call_args_list],
                         ["clear_shortlist_staging", "promote_shortlist"])

    def test_no_candidates_touches_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d, "c.json")
            path.write_text("[]", encoding="utf-8")
            with mock.patch.object(bq_export.db, "rpc") as rpc, \
                 mock.patch.object(sys, "argv", ["bq_export", "--candidates", str(path)]):
                self.assertEqual(bq_export.main(), 0)
        rpc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
