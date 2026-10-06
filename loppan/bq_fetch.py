"""Fetch the marketplace's listings as NDJSON for the BigQuery staging tables.

Each subcommand writes newline-delimited JSON whose keys and types match
deploy/bigquery/schema.sql (checked by `validate`, see bq_schema.py). Loading is not
done here: rows go to a file, and a workflow loads the file with `bq load`.

    python loppan/bq_fetch.py track --ids live.txt --out track.ndjson --runs-out runs.ndjson
    python loppan/bq_fetch.py new --since 2026-10-01 --out new.ndjson
    python loppan/bq_fetch.py adjudicate --ids gone.txt --out adj.ndjson
    python loppan/bq_fetch.py origins --ids p2p.txt --out origins.ndjson
    python loppan/bq_fetch.py brands --out brands.ndjson
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
import shutil
import subprocess
import sys
import time
import zoneinfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Every client import for the BigQuery path is here or in outcomes.py, so the
# rename on the market-rename branch touches one line.
from loppan import algolia, bq_brands, bq_schema, bq_shapes, outcomes

RESOLVE_GATE = 0.995      # docs/bigquery.md §3 change 3

# The hard price floor (docs/bigquery.md §12): no price under it is ever written.
# Read from loppan.brand_rules at run time; this is only the fallback.
FLOOR_DEFAULT_KR = 150.0
FLOOR_DEFAULT_ORE = int(FLOOR_DEFAULT_KR * 100)

# Every request here answers in well under a second (p90 0.3 s locally, 1.1 s on
# Actions), so a dead kept-alive connection should cost 10 s, not 30, before the
# client's own retry. Same throttle, same workers: this only shortens a stall.
algolia.TIMEOUT_S = 10
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
    """Timestamps (fetched_at, adjudicated_at, started_at) stay UTC, ending in Z."""
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Dates (run_date, first_offered, the `new --since` boundary) are Stockholm calendar
# days, whatever zone the runner is in. From the tz database when the runner has
# one; otherwise from the fixed EU rule below, which is what the database says for
# Stockholm for every year since 1996.
try:
    STOCKHOLM: zoneinfo.ZoneInfo | None = zoneinfo.ZoneInfo("Europe/Stockholm")
except (zoneinfo.ZoneInfoNotFoundError, OSError, ValueError):
    STOCKHOLM = None


def _last_sunday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, 31)            # March and October both have 31 days
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def _cet_offset(utc: dt.datetime) -> dt.timedelta:
    """CEST (UTC+2) from 01:00 UTC on the last Sunday of March to 01:00 UTC on the
    last Sunday of October; CET (UTC+1) otherwise. `utc` must be aware."""
    start = dt.datetime.combine(_last_sunday(utc.year, 3), dt.time(1), dt.UTC)
    end = dt.datetime.combine(_last_sunday(utc.year, 10), dt.time(1), dt.UTC)
    return dt.timedelta(hours=2 if start <= utc < end else 1)


def local_date(utc: dt.datetime) -> dt.date:
    """The Stockholm calendar day of an aware instant."""
    if STOCKHOLM is not None:
        return utc.astimezone(STOCKHOLM).date()
    utc = utc.astimezone(dt.UTC)
    return (utc + _cet_offset(utc)).date()


def local_midnight(day: dt.date) -> dt.datetime:
    """The UTC instant at which `day` begins in Stockholm. Midnight is never inside a
    clock change (those happen at 02:00 and 03:00 local), so it exists exactly once."""
    if STOCKHOLM is not None:
        return dt.datetime.combine(day, dt.time(), STOCKHOLM).astimezone(dt.UTC)
    naive = dt.datetime.combine(day, dt.time(), dt.UTC)
    return naive - _cet_offset(naive - dt.timedelta(hours=1))


def today_local(now: dt.datetime | None = None) -> str:
    return local_date(now or dt.datetime.now(dt.UTC)).isoformat()


def _date_ms(ms) -> str | None:
    """Epoch milliseconds -> Stockholm date. Never the runner's zone: enrol._date
    used that, which makes the same item's date depend on where it ran."""
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        return None
    if ms < 1e11:            # seconds, not milliseconds
        ms *= 1000
    return local_date(dt.datetime.fromtimestamp(ms / 1000, dt.UTC)).isoformat()


def _iso_day(v) -> str | None:
    """A YYYY-MM-DD string that is a real date, else None: one malformed date from
    a source must not fail the whole load."""
    try:
        return dt.date.fromisoformat(v).isoformat() if isinstance(v, str) and len(v) == 10 else None
    except ValueError:
        return None


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
              fetched_at: str, hit: dict | None = None, attributes: bool = True,
              floor_ore: int = FLOOR_DEFAULT_ORE) -> dict:
    """One sweep_staging row, every column present, in schema order (below_floor
    is appended by an ALTER, so it comes last).

    The floor is applied here, so no row can carry a price under it. A current
    price under the floor sets below_floor and writes neither price; merge_sweep.sql
    then closes the item. An old price under it alone is dropped, nothing else.
    Census and new never get this far with a price under the floor: an item under
    it gets no row at all (crawl_to_rows).
    """
    state = state_of(hit) if hit else NO_STATE
    attrs = attributes_of(hit) if (hit and attributes) else NO_ATTRIBUTES
    price, old = state["price_ore"], state["old_price_ore"]
    below = price is not None and price < floor_ore
    if below or (old is not None and old < floor_ore):
        old = None
    return {"run_date": run_date, "item_id": item_id, "source": source,
            "present": present, "is_for_sale": state["is_for_sale"],
            "fetched_at": fetched_at, "price_ore": None if below else price,
            "old_price_ore": old, "favourites": state["favourites"],
            "last_chance": state["last_chance"], **attrs, "below_floor": below}


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
          retries: int = 1, gone: list[str] | None = None,
          floor_ore: int = FLOOR_DEFAULT_ORE) -> dict:
    """Fetch known live ids. Returns a `runs` row plus measurements.

    present=false only for an id the index answered with null. An id in a chunk
    that never got an answer gets no row at all, so the MERGE leaves it untouched
    and it counts against completeness, never towards missing.

    An id answered with a price under `floor_ore` is present, below_floor=true,
    with no price (see sweep_row): the floor is hard, so that price is never written.

    `gone`, if given, collects the adjudication candidates (§5 step 6): ids that
    came back missing, or present with isForSale false.
    """
    started = utc_now()
    t0 = time.time()
    attrs = HIT_ATTRS if full else STATE_ATTRS
    answered: set[str] = set()       # chunk heads; a 6M-id pass holds ~60k of these
    fetched = missing = not_for_sale = below_floor = requests = 0
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
                row = sweep_row(run_date, item_id, "track", True, at, got, full, floor_ore)
                below_floor += row["below_floor"]
                out.write(row)
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
           "completed_at": None,   # stamped by daily.sh once every step is done
           "note": (f"track: {len(todo):,} ids unanswered, {not_for_sale:,} present "
                    f"but not for sale, {below_floor:,} below the floor"
                    if ids else "no live ids")}
    measured = {"requests": requests, "seconds": round(secs, 2),
                "requests_per_s": round(requests / secs, 1) if secs else None,
                "s_per_1000_ids": round(secs / len(ids) * 1000, 3) if ids else None,
                "not_for_sale": not_for_sale, "below_floor": below_floor,
                "unanswered": len(todo)}
    return {"run": run, "measured": measured}


def cmd_track(a) -> int:
    ids = read_ids(a.ids)
    floor_kr, floor_src = price_floor(a)
    out = NDJSON(a.out)
    gone: list[str] = []
    res = track(ids, out, a.run_date, full=a.full, retries=a.retries, gone=gone,
                floor_ore=kr_to_ore(floor_kr))
    res["min_price_kr"] = {"value": floor_kr, "source": floor_src}
    res["output"] = out.close()
    if a.runs_out:
        runs = NDJSON(a.runs_out)
        runs.write(res["run"])
        res["runs_output"] = runs.close()
    write_ids(a.gone_out, gone)
    res["adjudication_candidates"] = len(gone)
    emit_summary(res, a.summary)
    return 0


# ---------------------------------------------------------------- search: new, census


def read_brands(path: str | None) -> set[str] | None:
    if not path:
        return None
    with open(path, encoding="utf-8-sig") as fh:
        return {line.rstrip("\n\r") for line in fh if line.strip()}


def crawl_to_rows(crawl: bq_shapes.Crawl, out: NDJSON, run_date: str, source: str,
                  brands: set[str] | None, floor_ore: int = FLOOR_DEFAULT_ORE) -> dict:
    """Run a crawl, writing one row per distinct live item. With a brand set, items
    of other brands, and unbranded ones, are counted and dropped. The price floor is
    in the query; an item under it, or with no price, is counted and gets no row."""
    seen: set[str] = set()
    tally = {"duplicates": 0, "dropped_by_brand": 0, "unbranded": 0, "below_floor": 0}

    def on_hits(hits):
        at = utc_now()
        for h in hits:
            item_id = h.get("objectID")
            if not item_id or item_id in seen:
                tally["duplicates"] += 1
                continue
            seen.add(item_id)
            price = _amount(h, "price_SE", "amount")
            if price is None or price < floor_ore:
                tally["below_floor"] += 1
                continue
            brand = (h.get("metadata") or {}).get("brand")
            if not brand:
                tally["unbranded"] += 1
            if brands is not None and brand not in brands:
                tally["dropped_by_brand"] += 1
                continue
            out.write(sweep_row(run_date, item_id, source, True, at, h, floor_ore=floor_ore))

    report = crawl.run(on_hits)
    report.update(tally, distinct_items=len(seen))
    return report


def since_ms(day: str) -> int:
    """Epoch ms of Stockholm midnight starting `day` (YYYY-MM-DD)."""
    return int(local_midnight(dt.date.fromisoformat(day)).timestamp() * 1000)


FLOOR_SQL = "select value from loppan.brand_rules where rule = 'min_price_kr'"


def kr_to_ore(kr: float) -> int:
    return int(round(kr * 100))


def floor_from_bq() -> float | None:
    """`min_price_kr` from loppan.brand_rules, through the `bq` CLI the Actions
    runner has after auth. None when there is no `bq`, no credentials, or no row."""
    exe = shutil.which("bq")
    if not exe:
        return None
    try:
        p = subprocess.run([exe, "--quiet", "--location=EU", "--format=json", "query",
                            "--use_legacy_sql=false", FLOOR_SQL],
                           capture_output=True, text=True, timeout=90)
        text = p.stdout if p.returncode == 0 else ""
        rows = json.loads(text[text.find("["):]) if "[" in text else []
        return float(rows[0]["value"]) if rows and rows[0].get("value") is not None else None
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError):
        return None


def price_floor(a) -> tuple[float, str]:
    """The hard price floor in kr, and where it came from: --min-price-kr, else
    loppan.brand_rules, else the default. One source for track, census and new.
    Census and new write no row for an item under it; track flags a tracked item
    marked down under it as below_floor, without its price (§12)."""
    if a.min_price_kr is not None:
        return a.min_price_kr, "--min-price-kr"
    v = floor_from_bq()
    if v is not None:
        return v, "loppan.brand_rules.min_price_kr"
    print(f"  min_price_kr not readable from BigQuery; using {FLOOR_DEFAULT_KR:g} kr",
          file=sys.stderr)
    return FLOOR_DEFAULT_KR, "default"


def search_crawl(a, first_dim: bq_shapes.Dim, source: str) -> int:
    """Shared by census and new: live, in scope, at or above the price floor.
    price_SE.amount is öre (it is pricing.amount x 100, checked 2026-10-02)."""
    floor_kr, floor_src = price_floor(a)
    floor_ore = kr_to_ore(floor_kr)
    base = f"isForSale:true AND price_SE.amount>={floor_ore}"
    dims = [first_dim, bq_shapes.Dim("price_SE.amount", floor_ore, 10 ** 9)]
    crawl = bq_shapes.Crawl(base, bq_shapes.scope(a.categories), dims, HIT_ATTRS,
                            max_leaves=a.max_leaves)
    out = NDJSON(a.out)
    res = crawl_to_rows(crawl, out, a.run_date, source, read_brands(a.brands), floor_ore)
    res["output"] = out.close()
    res["min_price_kr"] = {"value": floor_kr, "source": floor_src}
    if source == "new":
        res["new_found"] = res["output"]["rows"]
    emit_summary(res, a.summary)
    return 0 if res["complete"] else 2


def cmd_new(a) -> int:
    """Items first offered on or after --since, from midnight in Stockholm."""
    return search_crawl(a, bq_shapes.Dim("firstOfferedAt_SE", since_ms(a.since),
                                         bq_shapes.FAR * 1000, always=True), "new")


def cmd_census(a) -> int:
    """Every live item in scope: the one-off seed. createdAt is the split axis
    because it is on every item and never changes (see bq_shapes)."""
    return search_crawl(a, bq_shapes.Dim("createdAt", 0, bq_shapes.FAR), "census")


# ---------------------------------------------------------------- Parse: adjudicate, origins


def cmd_adjudicate(a) -> int:
    """adjudication_staging rows for ids that left the index or stopped being for
    sale. Strictly serial, 60 ids per request, at the Parse client's interval.

    Written: sold | expired | unknown. NOT written, so they stay live: still_listed,
    ids whose request failed (no answer yet), and ids Parse has no offer for.
    final_price_ore is Parse's last ask; the resolve MERGE may fall back to the
    item's last seen price where it is null, as v1's track.py did. item_status is
    Parse's itemStatus exactly as returned, beside the outcome it mapped to, so the
    reason for an `unknown` survives.
    """
    ids = read_ids(a.ids)
    out = NDJSON(a.out)
    t0 = time.time()
    counts = {"sold": 0, "expired": 0, "unknown": 0, "still_listed": 0,
              "failed": 0, "unaccounted": 0}
    statuses: dict[str, int] = {}
    for i in range(0, len(ids), outcomes.ADJUDICATE):
        chunk = ids[i:i + outcomes.ADJUDICATE]
        verdicts, failed = outcomes.adjudicate_detailed(chunk)
        at = utc_now()
        counts["failed"] += len(failed)
        for item_id in chunk:
            v = verdicts.get(item_id)
            if v is None:
                counts["unaccounted"] += 0 if item_id in failed else 1
                continue
            verdict, final, status = v
            counts[verdict] += 1
            statuses[str(status)] = statuses.get(str(status), 0) + 1
            if verdict == "still_listed":
                continue
            out.write({"run_date": a.run_date, "item_id": item_id, "outcome": verdict,
                       "final_price_ore": final, "adjudicated_at": at,
                       "item_status": None if status is None else str(status)})
    secs = time.time() - t0
    requests = -(-len(ids) // outcomes.ADJUDICATE)
    emit_summary({"ids": len(ids), **counts, "item_status": statuses,
                  "requests": requests, "seconds": round(secs, 2),
                  "requests_per_s": round(requests / secs, 2) if secs else None,
                  "output": out.close()}, a.summary)
    return 0


def cmd_origins(a) -> int:
    """circle_origin_staging rows: what each Circle seller paid. Strictly serial;
    two Parse requests per linked item. A listing with no `preceding` pointer gets a
    row with a null original_id, which records that it was looked at; a request
    that fails gets no row, so it is asked again next run. bought_on is the day the
    original's last price step ended, as origin_of reads it."""
    ids = read_ids(a.ids)
    out = NDJSON(a.out)
    t0 = time.time()
    linked = unlinked = no_ladder = failed = 0
    for item_id in ids:
        try:
            o = outcomes.origin_of(item_id)
        except Exception as exc:
            failed += 1
            print(f"  {item_id}: {type(exc).__name__}", file=sys.stderr)
            continue
        if o is None:
            unlinked += 1
            o = {}
        elif o.get("bought_price_ore") is None:
            no_ladder += 1
        else:
            linked += 1
        out.write({"run_date": a.run_date, "item_id": item_id,
                   "original_id": o.get("original_id"),
                   "bought_price_ore": o.get("bought_price_ore"),
                   "opening_ore": o.get("original_opening_ore"),
                   "rungs": o.get("original_rungs"), "fetched_at": utc_now(),
                   "bought_on": _iso_day(o.get("bought_on"))})
    secs = time.time() - t0
    requests = failed + unlinked + 2 * (linked + no_ladder)    # at most; failures vary
    emit_summary({"ids": len(ids), "with_price": linked, "linked_no_ladder": no_ladder,
                  "no_preceding": unlinked, "failed": failed, "requests": requests,
                  "seconds": round(secs, 2),
                  "requests_per_s": round(requests / secs, 2) if secs else None,
                  "output": out.close()}, a.summary)
    return 0


# ---------------------------------------------------------------- brands


def brand_rows(counts: dict, run_date: str) -> list[dict]:
    """brand_counts_staging rows, largest first. A brand with no name is not a
    brand (unbranded items have no key at all), so it never gets a row."""
    return [{"run_date": run_date, "brand": b, "listings": int(n)}
            for b, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            if isinstance(b, str) and b]


def cmd_brands(a) -> int:
    """§12 input: brand_counts_staging NDJSON, the exact count of live listings at
    or above the price floor per brand, in scope. Exit 2 if any shape could not be
    counted exactly, so a partial count is never loaded as if it were whole."""
    floor_kr, floor_src = price_floor(a)
    base = f"isForSale:true AND price_SE.amount>={kr_to_ore(floor_kr)}"
    res = bq_brands.run(bq_shapes.scope(a.categories), a.median_top, base)
    out = NDJSON(a.out)
    for row in brand_rows(res.pop("counts"), a.run_date):
        out.write(row)
    summary = {k: v for k, v in res.items() if k != "brands"}
    summary.update(output=out.close(), scope=a.categories, generated_at=utc_now(),
                   min_price_kr={"value": floor_kr, "source": floor_src},
                   listings_total=sum(r["live_listings"] for r in res["brands"]),
                   at_or_over_20=sum(r["live_listings"] >= 20 for r in res["brands"]),
                   top_10=res["brands"][:10])
    emit_summary(summary, a.summary)
    return 0 if res["partition"]["complete"] else 2


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
        sp.add_argument("--run-date", default=today_local(),
                        help="run_date for every row (default: today in Stockholm)")
        sp.add_argument("--summary", help="also write the JSON summary here")
        if out:
            sp.add_argument("--out", required=True, help="NDJSON file to write")

    def floored(sp):
        sp.add_argument("--min-price-kr", type=float,
                        help="hard price floor in kr; default: loppan.brand_rules "
                             f"min_price_kr, else {FLOOR_DEFAULT_KR:g}")

    sp = sub.add_parser("track", help="fetch known live ids (sweep_staging, source=track)")
    common(sp)
    floored(sp)
    sp.add_argument("--ids", required=True, help="file of live ids, one per line")
    sp.add_argument("--runs-out", help="write the runs row (completeness gate) here")
    sp.add_argument("--gone-out", help="write adjudication candidates here")
    sp.add_argument("--full", action="store_true",
                    help="also fetch and write attributes (default: state only)")
    sp.add_argument("--retries", type=int, default=1,
                    help="extra passes over ids whose chunk got no answer")
    sp.set_defaults(fn=cmd_track)

    def searching(sp):
        sp.add_argument("--categories", nargs="+", default=algolia.WEARABLE,
                        help="category paths in scope, any level (default: clothing and "
                             "shoes, all demographies)")
        sp.add_argument("--brands", help="file of in-scope brand names, one per line; "
                                         "other brands and unbranded items are dropped")
        sp.add_argument("--max-leaves", type=int,
                        help="stop after this many leaf shapes (for sample runs)")
        floored(sp)

    sp = sub.add_parser("census", help="one-off seed of every live item (source=census)")
    common(sp)
    searching(sp)
    sp.set_defaults(fn=cmd_census)

    sp = sub.add_parser("new", help="items listed since a date (sweep_staging, source=new)")
    common(sp)
    searching(sp)
    sp.add_argument("--since", required=True,
                    help="YYYY-MM-DD, a Stockholm day; include a day of overlap")
    sp.set_defaults(fn=cmd_new)

    sp = sub.add_parser("adjudicate", help="Parse verdicts (adjudication_staging)")
    common(sp)
    sp.add_argument("--ids", required=True, help="adjudication candidates, one per line")
    sp.set_defaults(fn=cmd_adjudicate)

    sp = sub.add_parser("origins", help="Circle purchase prices (circle_origin_staging)")
    common(sp)
    sp.add_argument("--ids", required=True, help="Circle (p2p) item ids, one per line")
    sp.set_defaults(fn=cmd_origins)

    sp = sub.add_parser("brands", help="exact live listings per brand at or above the "
                                       "floor (brand_counts_staging)")
    common(sp)
    floored(sp)
    sp.add_argument("--categories", nargs="+", default=algolia.WEARABLE)
    sp.add_argument("--median-top", type=int, default=0,
                    help="also put the median ask of the top N brands in the summary "
                         "(one request each)")
    sp.set_defaults(fn=cmd_brands)

    sp = sub.add_parser("validate", help="check an NDJSON file against schema.sql")
    sp.add_argument("file")
    sp.add_argument("--table", required=True, choices=bq_schema.CONTRACT)
    sp.add_argument("--summary")
    sp.set_defaults(fn=cmd_validate)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
