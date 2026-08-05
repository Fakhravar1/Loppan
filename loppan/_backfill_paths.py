"""One-off: fill in price paths for items that resolved before the ladder was stored."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from loppan import cohort, db, sellpy

todo = db.query("cohort_checks?select=item_id&outcome=in.(sold,expired)&ladder=is.null")
print(f"backfilling price paths for {len(todo)} already-resolved items")
for r in todo:
    path = cohort._path(sellpy.ladder(r["item_id"]))
    if path:
        db.update("cohort_checks", [{"item_id": r["item_id"], **path}], "item_id")
        print(f"  {r['item_id']}: {path['rungs']} steps, {path['opening_ask']} -> {path['final_price']} kr")
print("done")
