"""One-off: add image paths to rows collected before they were captured.

No Sellpy requests — every image URL is already in the local caches.
"""
import glob, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from loppan import db, search

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"

imgs = {}
for path in glob.glob(str(DATA / "*.jsonl")):
    for line in open(path, encoding="utf-8"):
        row = json.loads(line)
        item = row.get("item") or row
        oid = item.get("objectId") or item.get("item_id") or item.get("id")
        if oid and item.get("images"):
            imgs.setdefault(oid, search.image_paths(item["images"]))
print(f"{len(imgs)} items with images found in local caches")

for table in ("cohort_items", "season_clearings"):
    todo = [r["item_id"] for r in db.query(f"{table}?select=item_id&image_paths=is.null")]
    rows = [{"item_id": i, "image_paths": imgs[i]} for i in todo if i in imgs]
    print(f"  {table}: {len(todo)} missing, {len(rows)} recoverable")
    for row in rows:
        db.update(table, [row], "item_id")

# Cohort items were enrolled from the search index, not the offer cache, so their
# images are not in any local file. One batched lookup per 100 ids.
todo = [r["item_id"] for r in db.query("cohort_items?select=item_id&image_paths=is.null")]
found = []
for i in range(0, len(todo), 100):
    chunk = todo[i:i + 100]
    for hit in search.search(filter_by="id:[" + ",".join(chunk) + "]", per_page=100)["hits"]:
        paths = search.image_paths(hit["document"].get("images"))
        if paths:
            found.append({"item_id": hit["document"]["id"], "image_paths": paths})
print(f"  cohort_items via search index: {len(todo)} missing, {len(found)} found")
for row in found:
    db.update("cohort_items", [row], "item_id")
print("done")
