"""Fetch the marketplace's listings as NDJSON for the BigQuery staging tables.

Each subcommand writes newline-delimited JSON whose keys and types match
deploy/bigquery/schema.sql (checked by `validate`, see bq_schema.py). Loading is not
done here: rows go to a file, and a workflow loads the file with `bq load`.

    python loppan/bq_fetch.py track --ids live.txt --out track.ndjson --runs-out runs.ndjson
    python loppan/bq_fetch.py new --since 2026-10-01 --out new.ndjson
    python loppan/bq_fetch.py adjudicate --ids gone.txt --out adj.ndjson
    python loppan/bq_fetch.py origins --ids p2p.txt --out origins.ndjson
    python loppan/bq_fetch.py brands --out brands.json
    python loppan/bq_fetch.py census --out census.ndjson
    python loppan/bq_fetch.py validate track.ndjson --table sweep_staging

No database is imported. Conduct is the clients' own: Algolia through algolia.py's
throttle and worker count, Parse strictly serial through outcomes.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Every client import for the BigQuery path is here or in outcomes.py, so the
# rename on the market-rename branch touches one line.
from loppan import algolia, bq_schema, outcomes

RESOLVE_GATE = 0.995      # docs/bigquery.md §3 change 3
SEASON_BITS = {"Vår": 1, "Sommar": 2, "Höst": 4, "Vinter": 8}

# What a track row needs. Everything else stays on the server.
STATE_ATTRS = ["price_SE.amount", "priceDrop_SE.oldPrice.amount", "favouriteCount",
               "lastChance", "isForSale"]
# What a census or new row needs: ~590 bytes a hit instead of ~3 KB.
HIT_ATTRS = STATE_ATTRS + [
    "metadata.brand", "metadata.type", "metadata.demography", "metadata.size",
    "metadata.condition", "metadata.defects.type", "metadata.fabric",
    "metadata.pattern", "metadata.material", "metadata.color", "metadata.season",
    "brandClassification.pricePoint", "categories.lvl1", "categories.lvl2",
    "weight", "p2p", "firstOfferedAt_SE", "createdAt"]


# ---------------------------------------------------------------- values


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def today_utc() -> str:
    return dt.datetime.now(dt.UTC).date().isoformat()


def _date_ms(ms) -> str | None:
    """Epoch milliseconds -> UTC date. UTC, not the runner's zone: enrol._date used
    the local zone, which makes the same item's date depend on where it ran."""
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        return None
    if ms < 1e11:            # seconds, not milliseconds
        ms *= 1000
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).date().isoformat()


def _int(v) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v == v and v.is_integer():
        return int(v)
    return None


def _bool(v) -> bool | None:
    return v if isinstance(v, bool) else None


def _str(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _strs(v) -> list[str]:
    return [x for x in (v or []) if isinstance(x, str) and x] if isinstance(v, list) else []


def _amount(obj, *path):
    for key in path:
        obj = obj.get(key) if isinstance(obj, dict) else None
    return _int(obj)


# ---------------------------------------------------------------- rows


def state_of(hit: dict) -> dict:
    return {
        "is_for_sale": _bool(hit.get("isForSale")),
        "price_ore": _amount(hit, "price_SE", "amount"),
        "old_price_ore": _amount(hit, "priceDrop_SE", "oldPrice", "amount"),
        "favourites": _int(hit.get("favouriteCount")),
        "last_chance": _bool(hit.get("lastChance")),
    }


def attributes_of(hit: dict) -> dict:
    """The item's attributes, read the way enrol.row_of reads them."""
    m = hit.get("metadata") or {}
    cats = (hit.get("categories") or {}).get("lvl2") or (hit.get("categories") or {}).get("lvl1") or []
    weight_kg = hit.get("weight")
    season = 0
    for s in _strs(m.get("season")):
        season |= SEASON_BITS.get(s, 0)
    return {
        "brand": _str(m.get("brand")),           # absent = unbranded; there is no label
        "brand_tier": _int((hit.get("brandClassification") or {}).get("pricePoint")),
        "category": _str(cats[0]) if cats else None,
        "item_type": _str(m.get("type")),
        "demography": _str(m.get("demography")),
        "size_code": _str(m.get("size")),
        "condition": _str(m.get("condition")),
        "has_defect": bool(m.get("defects")),
        "fabric": _str(m.get("fabric")),
        "pattern": _str(m.get("pattern")),
        "materials": _strs(m.get("material")),
        "colours": _strs(m.get("color")),
        "season_mask": season,     # 0 = untagged, as enrol.row_of had it
        # round, not int(): 4.35 kg * 1000 is 4349.999... in floating point
        "weight_g": int(round(weight_kg * 1000)) if isinstance(weight_kg, (int, float))
                    and not isinstance(weight_kg, bool) and weight_kg > 0 else None,
        "p2p": _bool(hit.get("p2p")),
        "first_offered": _date_ms(hit.get("firstOfferedAt_SE")),
    }


NO_ATTRIBUTES = {k: ([] if k in ("materials", "colours") else None)
                 for k in attributes_of({"metadata": {}})}
NO_ATTRIBUTES["has_defect"] = None
NO_STATE = dict.fromkeys(state_of({}))


def sweep_row(run_date: str, item_id: str, source: str, present: bool,
              fetched_at: str, hit: dict | None = None, attributes: bool = True) -> dict:
    """One sweep_staging row, every column present, in schema order."""
    state = state_of(hit) if hit else NO_STATE
    attrs = attributes_of(hit) if (hit and attributes) else NO_ATTRIBUTES
    return {"run_date": run_date, "item_id": item_id, "source": source,
            "present": present, "is_for_sale": state["is_for_sale"],
            "fetched_at": fetched_at, "price_ore": state["price_ore"],
            "old_price_ore": state["old_price_ore"], "favourites": state["favourites"],
            "last_chance": state["last_chance"], **attrs}


# ---------------------------------------------------------------- files


class NDJSON:
    """Write rows to a UTF-8 NDJSON file, counting rows and bytes as they go."""

    def __init__(self, path: str):
        self.path = path
        self.fh = open(path, "w", encoding="utf-8", newline="\n")
        self.rows = 0
        self.bytes = 0

    def write(self, row: dict) -> None:
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.fh.write(line)
        self.rows += 1
        self.bytes += len(line.encode("utf-8"))

    def close(self) -> dict:
        self.fh.close()
        return {"file": self.path, "rows": self.rows, "bytes": self.bytes,
                "bytes_per_row": round(self.bytes / self.rows, 1) if self.rows else None}


def read_ids(path: str) -> list[str]:
    """One id per line; blank lines and an `item_id` header are skipped. Order is
    kept and duplicates dropped, so a chunk never asks for the same id twice."""
    seen: dict[str, None] = {}
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            x = line.strip().split(",")[0].strip().strip('"')
            if x and x != "item_id":
                seen.setdefault(x, None)
    return list(seen)


def write_ids(path: str | None, ids: list[str]) -> None:
    if path:
        pathlib.Path(path).write_text("".join(f"{i}\n" for i in ids), encoding="utf-8")


def emit_summary(summary: dict, path: str | None = None) -> None:
    text = json.dumps(summary, ensure_ascii=False, indent=1)
    print(text)
    if path:
        pathlib.Path(path).write_text(text + "\n", encoding="utf-8")


# ---------------------------------------------------------------- track


def fetch_by_id(ids: list[str], attributes: list[str]):
    """Yield (chunk_ids, results, fetched_at) for every chunk that got an answer.

    A chunk that errors, or whose answer does not line up id for id, is not
    yielded. Its ids were NOT fetched, which is different from missing: missing is
    an answer (the index said null), not-fetched is the absence of one.
    """
    for chunk_ids, results in algolia.get_objects_parallel(ids, attributes=attributes):
        if len(results) != len(chunk_ids) or any(
                r is not None and r.get("objectID") != i for i, r in zip(chunk_ids, results)):
            print(f"  chunk at {chunk_ids[0]}: answer does not match the request; "
                  f"counted as not fetched", file=sys.stderr)
            continue
        yield chunk_ids, results, utc_now()


def track(ids: list[str], out: NDJSON, run_date: str, full: bool = False,
          retries: int = 1, gone: list[str] | None = None) -> dict:
    """Fetch known live ids. Returns a `runs` row plus measurements.

    present=false only for an id the index answered with null. An id in a chunk
    that never got an answer gets no row at all, so the MERGE leaves it untouched
    and it counts against completeness, never towards missing.

    `gone`, if given, collects the adjudication candidates (§5 step 6): ids that
    came back missing, or present with isForSale false.
    """
    started = utc_now()
    t0 = time.time()
    attrs = HIT_ATTRS if full else STATE_ATTRS
    answered: set[str] = set()       # chunk heads; a 6M-id pass holds ~60k of these
    fetched = missing = not_for_sale = requests = 0
    todo = ids
    for attempt in range(1 + retries):
        for chunk_ids, results, at in fetch_by_id(todo, attrs):
            requests += 1
            answered.add(chunk_ids[0])
            for item_id, got in zip(chunk_ids, results):
                fetched += 1
                if got is None:
                    missing += 1
                    out.write(sweep_row(run_date, item_id, "track", False, at))
                    if gone is not None:
                        gone.append(item_id)
                    continue
                if got.get("isForSale") is False:
                    not_for_sale += 1
                    if gone is not None:
                        gone.append(item_id)
                out.write(sweep_row(run_date, item_id, "track", True, at, got, full))
        # What was not answered, rebuilt from the chunk heads; asked once more.
        chunks = [todo[i:i + 100] for i in range(0, len(todo), 100)]
        todo = [x for c in chunks if c[0] not in answered for x in c]
        if not todo:
            break
        print(f"  {len(todo):,} ids unanswered after pass {attempt + 1}", file=sys.stderr)

    secs = time.time() - t0
    completeness = fetched / len(ids) if ids else None
    run = {"run_date": run_date, "started_at": started, "finished_at": utc_now(),
           "live_ids": len(ids), "fetched": fetched, "missing": missing,
           "new_found": None, "completeness": completeness,
           "resolve_allowed": bool(completeness is not None
                                   and completeness >= RESOLVE_GATE),
           "note": (f"track: {len(todo):,} ids unanswered, {not_for_sale:,} present "
                    f"but not for sale" if ids else "no live ids")}
    measured = {"requests": requests, "seconds": round(secs, 2),
                "requests_per_s": round(requests / secs, 1) if secs else None,
                "s_per_1000_ids": round(secs / len(ids) * 1000, 3) if ids else None,
                "not_for_sale": not_for_sale, "unanswered": len(todo)}
    return {"run": run, "measured": measured}


def cmd_track(a) -> int:
    ids = read_ids(a.ids)
    out = NDJSON(a.out)
    gone: list[str] = []
    res = track(ids, out, a.run_date, full=a.full, retries=a.retries, gone=gone)
    res["output"] = out.close()
    if a.runs_out:
        runs = NDJSON(a.runs_out)
        runs.write(res["run"])
        res["runs_output"] = runs.close()
    write_ids(a.gone_out, gone)
    res["adjudication_candidates"] = len(gone)
    emit_summary(res, a.summary)
    return 0


# ---------------------------------------------------------------- validate


def cmd_validate(a) -> int:
    res = bq_schema.validate_file(pathlib.Path(a.file), a.table)
    emit_summary(res, a.summary)
    return 0 if res["ok"] else 1


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bq_fetch", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, out=True):
        sp.add_argument("--run-date", default=today_utc(),
                        help="run_date for every row (default: today, UTC)")
        sp.add_argument("--summary", help="also write the JSON summary here")
        if out:
            sp.add_argument("--out", required=True, help="NDJSON file to write")

    sp = sub.add_parser("track", help="fetch known live ids (sweep_staging, source=track)")
    common(sp)
    sp.add_argument("--ids", required=True, help="file of live ids, one per line")
    sp.add_argument("--runs-out", help="write the runs row (completeness gate) here")
    sp.add_argument("--gone-out", help="write adjudication candidates here")
    sp.add_argument("--full", action="store_true",
                    help="also fetch and write attributes (default: state only)")
    sp.add_argument("--retries", type=int, default=1,
                    help="extra passes over ids whose chunk got no answer")
    sp.set_defaults(fn=cmd_track)

    sp = sub.add_parser("validate", help="check an NDJSON file against schema.sql")
    sp.add_argument("file")
    sp.add_argument("--table", required=True, choices=bq_schema.CONTRACT)
    sp.add_argument("--summary")
    sp.set_defaults(fn=cmd_validate)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
