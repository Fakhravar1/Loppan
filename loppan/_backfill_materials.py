"""One-off: add fibre content to rows collected before materials were captured.

Costs no Sellpy requests. The season data comes from the locally cached offers,
which already carry the full item inline; the cohort data comes from a batched
id lookup against the search index.
"""
import glob, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from loppan import db, search

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"

# --- season_clearings, from the local offer cache -------------------------
mats = {}
for path in glob.glob(str(DATA / "season_offers.jsonl")) + glob.glob(str(DATA / "scan_*.jsonl")):
    for line in open(path, encoding="utf-8"):
        item = (json.loads(line).get("item") or {})
        meta = item.get("metadata") or {}
        if item.get("objectId") and meta.get("material"):
            mats.setdefault(item["objectId"], meta["material"])

todo = [r["item_id"] for r in db.query("season_clearings?select=item_id&materials=is.null")]
rows = [{"item_id": i, "materials": mats[i]} for i in todo if i in mats]
print(f"season_clearings: {len(todo)} missing, {len(rows)} recoverable from cache")
for row in rows:
    db.update("season_clearings", [row], "item_id")

# --- cohort_items, from the search index ---------------------------------
todo = [r["item_id"] for r in db.query("cohort_items?select=item_id&materials=is.null")]
found = []
for i in range(0, len(todo), 100):
    chunk = todo[i:i+100]
    for hit in search.search(filter_by="id:[" + ",".join(chunk) + "]", per_page=100)["hits"]:
        s = search.summarise(hit["document"])
        if s.get("materials"):
            found.append({"item_id": s["item_id"], "materials": s["materials"]})
print(f"cohort_items: {len(todo)} missing, {len(found)} found in the index")
for row in found:
    db.update("cohort_items", [row], "item_id")
print("done")
