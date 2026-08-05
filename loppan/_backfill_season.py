"""One-off: add season/category/demography to items enrolled before they were captured."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from loppan import db, search

todo = [r["item_id"] for r in db.query("cohort_items?select=item_id&season=is.null")]
print(f"{len(todo)} items to enrich")
found, rows = 0, []
for i in range(0, len(todo), 100):
    chunk = todo[i:i+100]
    for doc in search.search(filter_by="id:[" + ",".join(chunk) + "]", per_page=100)["hits"]:
        s = search.summarise(doc["document"])
        rows.append({"item_id": s["item_id"], "season": s["season"],
                     "category": s["category"], "demography": s["demography"]})
        found += 1
    print(f"  {min(i+100,len(todo))}/{len(todo)} scanned, {found} found", file=sys.stderr)

for row in rows:
    db.update("cohort_items", [row], "item_id")
print(f"updated {len(rows)} ({len(todo)-len(rows)} no longer in the index)")
