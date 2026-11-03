"""listing_drafts: mapping, title length, the price rule and needs_review. No network."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import listing_drafts as ld


def doc(**meta_overrides):
    """A search-index record shaped like the live ones, minus everything unused."""
    meta = {"brand": "Ganni", "type": "Kappa", "size": "WMN-INT-L", "condition": "Bra",
            "demography": "Kvinna", "color": ["Svart"], "material": ["Elastan", "Polyester"],
            "defects": []}
    meta.update(meta_overrides)
    return {"objectID": "abc123", "metadata": meta, "sizes": [meta.get("size")],
            "price_SE": {"amount": 24000, "currency": "SEK"},
            "categories": {"lvl2": ["Kvinna > Kläder > Jackor & Ytterkläder"]}}


class Categories(unittest.TestCase):
    def test_type_beats_source_path(self):
        path, reasons = ld.map_category("Kvinna > Kläder > Jackor & Ytterkläder", "Kappa")
        self.assertEqual(path, "Mode > Kläder > Jackor > Kappor")
        self.assertEqual(reasons, [])

    def test_unknown_type_falls_to_confirmed_source(self):
        path, reasons = ld.map_category("Man > Kläder > Jackor & Ytterkläder", "Fleecejacka")
        self.assertEqual(path, "Mode > Kläder > Jackor")
        self.assertEqual(reasons, [])

    def test_assumed_type_is_flagged(self):
        path, reasons = ld.map_category("Man > Kläder > Jackor & Ytterkläder", "Allvädersjacka")
        self.assertEqual(path, "Mode > Kläder > Jackor > Vindjackor")
        self.assertTrue(reasons and reasons[0].startswith("category assumed"))

    def test_assumed_source_is_flagged(self):
        path, reasons = ld.map_category("Kvinna > Skor > Finskor", "Derbyskor")
        self.assertEqual(path, "Mode > Skor > Övrigt inom skor")
        self.assertTrue(reasons[0].startswith("category assumed"))

    def test_unmapped_falls_back_by_group(self):
        path, reasons = ld.map_category("Man > Skor > Okänt", "Okänd sko")
        self.assertEqual(path, "Mode > Skor > Övrigt inom skor")
        self.assertTrue(reasons[0].startswith("category missing"))
        path, _ = ld.map_category(None, None)
        self.assertEqual(path, "Mode > Övrigt Inom Mode")

    def test_every_mapped_path_starts_at_a_plick_root(self):
        cats = ld.mapping()["categories"]
        paths = [p for table in ("by_type", "by_source") for s in ("confirmed", "assumed")
                 for p in cats[table][s].values()] + list(cats["fallback"].values())
        for p in paths:
            self.assertTrue(p.startswith(("Mode", "Sportkläder")), p)


class Sizes(unittest.TestCase):
    def test_letter_sizes_are_confirmed(self):
        self.assertEqual(ld.map_size("WMN-INT-M", "Kvinna"), ("M", "M", []))
        self.assertEqual(ld.map_size("MEN-INT-3XL", "Man"), ("XXL+", "3XL", []))

    def test_eu_clothing_converts_and_flags(self):
        size, label, reasons = ld.map_size("WMN-EU-38", "Kvinna")
        self.assertEqual((size, label), ("M", "38"))
        self.assertTrue(reasons[0].startswith("size assumed"))
        self.assertEqual(ld.map_size("MEN-EU-50", "Man")[0], "L")

    def test_range_takes_first_value_and_flags(self):
        size, label, reasons = ld.map_size("WMN-INT-S/M", "Kvinna")
        self.assertEqual((size, label), ("S", "S/M"))
        self.assertTrue(reasons)

    def test_shoes_depend_on_demography(self):
        self.assertEqual(ld.map_size("SHOES-EU-38", "Kvinna"), ("38", "38", []))
        self.assertEqual(ld.map_size("SHOES-EU-44", "Kvinna")[0], "43+")
        self.assertEqual(ld.map_size("SHOES-EU-44", "Man"), ("44", "44", []))
        self.assertEqual(ld.map_size("SHOES-EU-46", "Man")[0], "45+")
        self.assertIsNone(ld.map_size("SHOES-EU-36", "Man")[0])
        self.assertIsNone(ld.map_size("SHOES-EU-38", "Barn")[0])

    def test_half_shoe_size_rounds_down_and_flags(self):
        size, label, reasons = ld.map_size("SHOES-EU-37.5", "Kvinna")
        self.assertEqual((size, label), ("37", "37.5"))
        self.assertIn("half size", reasons[0])

    def test_waist(self):
        self.assertEqual(ld.map_size("PANTS-INCH-31", "Man"), ("W31", "W31", []))
        self.assertEqual(ld.map_size("PANTS-INCH-40", "Kvinna")[0], "W40+")

    def test_missing_sizes(self):
        for code in ("ONE SIZE", "NO SIZE", None, "CHILD-CM-104", "SOCKS-EU-36/38"):
            size, _, reasons = ld.map_size(code, "Kvinna")
            self.assertIsNone(size, code)
            self.assertTrue(reasons[0].startswith("size missing"), code)


class Conditions(unittest.TestCase):
    def test_all_five_source_grades_map(self):
        for src in ("Nytt", "Mycket bra", "Bra", "Acceptabelt", "Dåligt"):
            plick, _ = ld.map_condition(src)
            self.assertEqual(plick, f"{src} skick")

    def test_nytt_needs_a_check(self):
        self.assertTrue(ld.map_condition("Nytt")[1])
        self.assertEqual(ld.map_condition("Bra")[1], [])

    def test_unknown_condition(self):
        plick, reasons = ld.map_condition("Okänt")
        self.assertIsNone(plick)
        self.assertTrue(reasons)


class Titles(unittest.TestCase):
    def test_brand_type_size(self):
        self.assertEqual(ld.make_title("Ganni", "Kappa", "L"), "Ganni kappa strl L")
        self.assertEqual(ld.make_title("Levi's", "Jeans", "W31"), "Levi's jeans W31")
        self.assertEqual(ld.make_title("Dr. Martens", "Chelsea boots", "38"),
                         "Dr. Martens Chelsea boots strl 38")

    def test_never_over_the_limit(self):
        limit = ld.mapping()["limits"]["title_max"]
        long_brand = "Maison Margiela Artisanal Couture Collection Atelier Paris"
        for brand, typ, size in [(long_brand, "Vändbar jacka", "XXL"),
                                 ("X" * 120, "Kappa", "M"), (long_brand, None, None)]:
            title = ld.make_title(brand, typ, size)
            self.assertLessEqual(len(title), limit, title)
            self.assertEqual(title, title.strip())

    def test_drops_detail_before_cutting_words(self):
        title = ld.make_title("Very Long Brand Name Collection Edition", "Chelsea boots", "37.5",
                              limit=50)
        self.assertTrue(title.endswith("Chelsea boots 37.5"), title)

    def test_missing_brand_still_titles(self):
        self.assertEqual(ld.make_title(None, "Kofta", "S"), "Kofta strl S")


class Price(unittest.TestCase):
    def test_shortlist_expectation_wins_and_rounds_to_ten(self):
        self.assertEqual(ld.price_kr(24000, 96350), (960, "shortlist expected_now_ore"))
        self.assertEqual(ld.price_kr(24000, 96500)[0], 970)  # halves go up

    def test_multiple_of_ask_otherwise(self):
        self.assertEqual(ld.price_kr(24000, None), (480, "2 x current ask"))
        self.assertEqual(ld.price_kr(17450, None)[0], 350)
        self.assertEqual(ld.price_kr(24000, None, multiple=2.5)[0], 600)

    def test_shortlist_can_be_ignored(self):
        self.assertEqual(ld.price_kr(24000, 96350, use_shortlist=False)[0], 480)

    def test_no_price_at_all(self):
        self.assertIsNone(ld.price_kr(None, None)[0])


class Drafts(unittest.TestCase):
    PATHS = ["a/1.jpg", "a/2.jpg"]

    def test_clean_item_needs_no_review(self):
        d = ld.build_draft(doc(), self.PATHS, ["p/01.jpg", "p/02.jpg"], expected_now_ore=96350)
        self.assertFalse(d["needs_review"], d["review_reasons"])
        self.assertEqual(d["title"], "Ganni kappa strl L")
        self.assertEqual(d["category"], "Mode > Kläder > Jackor > Kappor")
        self.assertEqual((d["size"], d["condition"], d["passform"]), ("L", "Bra skick", "Kvinna"))
        self.assertEqual(d["price_kr"], 960)
        self.assertEqual(d["venue"], "plick")

    def test_description_is_swedish_and_complete(self):
        text = ld.build_draft(doc(), self.PATHS)["description"]
        for part in ("Ganni kappa.", "Storlek: L.", "Skick: Bra skick.",
                     "Material: Elastan, polyester.", "Färg: Svart.", "Defekter: inga noterade."):
            self.assertIn(part, text)
        self.assertEqual(len(text.splitlines()), 7)

    def test_defects_are_spelled_out(self):
        d = ld.build_draft(doc(defects=[{"type": "Nopprig", "location": "Hela varan"}]),
                           self.PATHS)
        self.assertIn("Defekter: ja, nopprig (hela varan). Se bilderna.", d["description"])

    def test_each_guess_is_a_reason(self):
        d = ld.build_draft(doc(type="Allvädersjacka", size="WMN-EU-38", condition="Nytt"),
                           self.PATHS)
        self.assertTrue(d["needs_review"])
        kinds = [r.split(":")[0] for r in d["review_reasons"]]
        self.assertEqual(kinds, ["category assumed", "size assumed", "condition check"])

    def test_missing_things_are_reasons(self):
        d = ld.build_draft(doc(brand=None, size="ONE SIZE", demography="Barn"), None)
        reasons = " | ".join(d["review_reasons"])
        for expected in ("size missing", "passform missing", "brand missing", "no images"):
            self.assertIn(expected, reasons)

    def test_partial_photo_download_is_flagged(self):
        d = ld.build_draft(doc(), self.PATHS, ["p/01.jpg"])
        self.assertIn("only 1 of 2 photos downloaded", d["review_reasons"])

    def test_price_not_above_ask_is_flagged(self):
        d = ld.build_draft(doc(), self.PATHS, expected_now_ore=20000)
        self.assertTrue(any("at or below the current ask" in r for r in d["review_reasons"]))

    def test_multiple_parameter_reaches_the_draft(self):
        d = ld.build_draft(doc(), self.PATHS, multiple=3.0)
        self.assertEqual((d["price_kr"], d["price_basis"]), (720, "3 x current ask"))


if __name__ == "__main__":
    unittest.main()
