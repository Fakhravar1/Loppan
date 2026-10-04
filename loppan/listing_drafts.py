"""Plick listing drafts for items bought on the marketplace.

    python loppan/listing_drafts.py ID [ID ...]             # drafts + photos
    python loppan/listing_drafts.py ID ... --save           # and upsert to public.listings
    python loppan/listing_drafts.py ID ... --multiple 2.5   # price rule for non-shortlist items
    python loppan/listing_drafts.py ID ... --no-photos

Phase 1 of auto-posting: drafts only. Posting on Plick is app-only and is not here.

Each draft is built from the item's live search-index record (`algolia.get_objects`),
so nothing depends on what the database happened to keep. Category, size and
condition go through `plick_mapping.json`, which records for every entry whether the
Plick name was confirmed on plick.se or is a judgement call. Anything assumed,
converted or missing sets `needs_review` with a reason, so a human looks before it
goes out.

Price defaults: the shortlist's `expected_now_ore` rounded to 10 kr when the item is
on the shortlist, otherwise `--multiple` x the current ask (2 by default), rounded.

Photos land in %LOCALAPPDATA%\\loppan\\plick\\photos\\<item_id>\\01.jpg ...; the draft
JSON goes to ...\\plick\\drafts\\<item_id>.json and to stdout.

⚠️ Plick's own rules (plick.se/vanliga-fragor) say listing photos must be ones the
seller took. The owner holds the companies' permission to reuse the marketplace's
photos, but that is not the same thing as Plick's rule; every draft carries
`photo_source` so the posting step can decide.

Endpoint values come from the environment (see endpoints.py). A git-ignored `.env`
at the repo root is read if present, without overriding what is already set.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MAPPING_PATH = pathlib.Path(__file__).resolve().parent / "plick_mapping.json"
VENUE = "plick"
DOWNLOAD_TIMEOUT_S = 30
_mapping: dict | None = None


def load_dotenv(path: pathlib.Path = ROOT / ".env") -> None:
    """KEY=VALUE lines into os.environ, never overriding a value already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def mapping() -> dict:
    global _mapping
    if _mapping is None:
        _mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    return _mapping


def data_dir() -> pathlib.Path:
    base = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / ".local" / "share")
    return pathlib.Path(base) / "loppan" / "plick"


# --- mapping ---------------------------------------------------------------------

def map_condition(condition: str | None) -> tuple[str | None, list[str]]:
    table = mapping()["conditions"]
    plick = table["confirmed"].get(condition or "")
    if plick is None:
        return None, [f"condition {condition!r} has no Plick grade in the mapping"]
    note = table.get("review", {}).get(condition)
    return plick, [note] if note else []


def map_category(category: str | None, item_type: str | None) -> tuple[str, list[str]]:
    """Plick path for a source path like 'Kvinna > Kläder > Jackor & Ytterkläder'.

    The item type is tried first because it is the more specific of the two (Kappa
    has its own Plick category, Jackor & Ytterkläder does not). The source path's
    demography is dropped: Plick carries gender as a separate 'Passform' field.
    """
    cats = mapping()["categories"]
    for status in ("confirmed", "assumed"):
        path = cats["by_type"][status].get(item_type or "")
        if path:
            return path, [] if status == "confirmed" else [
                f"category assumed: item type {item_type!r} -> {path}"]
    parts = (category or "").split(" > ")
    leaf = " > ".join(parts[1:3])
    for status in ("confirmed", "assumed"):
        path = cats["by_source"][status].get(leaf)
        if path:
            return path, [] if status == "confirmed" else [
                f"category assumed: {leaf!r} -> {path}"]
    group = parts[1] if len(parts) > 1 else ""
    path = cats["fallback"].get(group) or cats["fallback"]["*"]
    return path, [f"category missing: {category!r} / {item_type!r} not mapped, "
                  f"fell back to {path}"]


def passform(demography: str | None) -> tuple[str | None, list[str]]:
    value = mapping()["passform"].get(demography or "")
    if value is None:
        return None, [f"passform missing: Plick shows Man/Unisex/Kvinna, item is "
                      f"{demography!r}"]
    return value, []


def _ranged(n: float, demo: str | None, table: dict, prefix: str, what: str,
            label: str) -> tuple[str | None, list[str]]:
    """A numeric size on Plick's open-topped scale: 35 ... 42, 43+ and so on."""
    if demo not in table:
        return None, [f"size missing: {what} {label} needs Man or Kvinna, item is {demo!r}"]
    low, top = table[demo]
    whole = int(n)
    reasons = [] if n == whole else [
        f"size assumed: half size {label} listed as {prefix}{whole} (the text keeps {label})"]
    if whole >= top:
        return f"{prefix}{top}+", reasons
    if whole < low:
        return None, [f"size missing: {what} {label} is below Plick's {prefix}{low}"]
    return f"{prefix}{whole}", reasons


def map_size(size_code: str | None, demography: str | None
             ) -> tuple[str | None, str | None, list[str]]:
    """(Plick size, label for the title and text, reasons) for a code like WMN-INT-M.

    The label keeps the item's own size ("38", "W30", "37.5") because that is what is
    on the garment; the Plick size is the nearest value Plick's size picker offers.
    """
    if not size_code or size_code in ("NO SIZE", "ONE SIZE"):
        return None, None, [f"size missing: item has {size_code or 'no size'}"]
    parts = size_code.split("-", 2)
    if len(parts) != 3:
        return None, None, [f"size missing: unrecognised code {size_code!r}"]
    group, system, value = parts
    sizes = mapping()["sizes"]
    first = value.split("/")[0]
    combined = [f"size assumed: range {value} listed as its first value"] if "/" in value else []

    if group in ("WMN", "MEN"):
        demo = "Kvinna" if group == "WMN" else "Man"
        if system == "INT":
            plick = sizes["letter"].get(first)
            if plick is None:
                return None, value, [f"size missing: letter size {value!r} not in mapping"]
            return plick, value, combined
        if system == "EU":
            plick = sizes["eu_clothing"][demo].get(first)
            if plick is None:
                return None, value, [f"size missing: EU size {value} not in mapping"]
            return plick, value, combined + [f"size assumed: EU {value} converted to {plick}"]
    try:
        n = float(first)
    except ValueError:
        return None, value, [f"size missing: {size_code!r} is not numeric"]
    if group == "SHOES" and system == "EU":
        plick, reasons = _ranged(n, demography, sizes["shoes"], "", "shoe size", value)
        return plick, value, combined + reasons
    if group == "PANTS" and system == "INCH":
        plick, reasons = _ranged(n, demography, sizes["waist"], "W", "waist", f"W{value}")
        return plick, f"W{value}", combined + reasons
    return None, value, [f"size missing: {group} sizes have no Plick equivalent"]


# --- text and price --------------------------------------------------------------

def round_to(kr: float, step: int = 10) -> int:
    """Nearest multiple of `step`, halves up. round() would bank-round 965 to 960."""
    return int(kr / step + 0.5) * step


def price_kr(ask_ore: int | None, expected_now_ore: int | None, multiple: float = 2.0,
             step: int = 10, use_shortlist: bool = True) -> tuple[int | None, str]:
    """(price in kr, basis). Shortlist expectation when there is one, else a multiple."""
    if use_shortlist and expected_now_ore:
        return round_to(expected_now_ore / 100, step), "shortlist expected_now_ore"
    if not ask_ore:
        return None, "no ask price"
    return round_to(multiple * ask_ore / 100, step), f"{multiple:g} x current ask"


def _type_word(item_type: str | None) -> str:
    if not item_type:
        return ""
    if item_type in mapping().get("title_keep_case", []):
        return item_type
    return item_type[:1].lower() + item_type[1:]


def make_title(brand: str | None, item_type: str | None, size_label: str | None,
               limit: int | None = None) -> str:
    """'Ganni kappa strl L', cut to Plick's title limit by dropping detail, not words."""
    limit = limit or mapping()["limits"]["title_max"]
    size = ""
    if size_label:
        size = size_label if size_label.startswith("W") else f"strl {size_label}"
    words = (brand or "").split()
    tail = [w for w in (_type_word(item_type), size) if w]
    title = " ".join(words + tail)
    if len(title) > limit and size.startswith("strl "):
        tail[-1] = size = size_label
        title = " ".join(words + tail)
    while len(title) > limit and len(words) > 1:
        words.pop()
        title = " ".join(words + tail)
    title = title[:limit].rstrip()
    return title[:1].upper() + title[1:]


def _listing(values) -> str:
    if isinstance(values, str):
        values = [values]
    values = [v for v in (values or []) if v]
    return ", ".join([values[0]] + [v.lower() for v in values[1:]]) if values else ""


def make_description(meta: dict, condition: str | None, size_label: str | None) -> str:
    """Swedish body: what it is, condition, materials, colours, defects, one neutral line."""
    head = " ".join(w for w in ((meta.get("brand") or ""), _type_word(meta.get("type"))) if w)
    if meta.get("model"):
        head += f", modell {meta['model']}"
    lines = [head[:1].upper() + head[1:] + "." if head else ""]
    if size_label:
        lines.append(f"Storlek: {size_label}.")
    lines.append(f"Skick: {condition or meta.get('condition') or 'se bilderna'}.")
    if meta.get("material"):
        lines.append(f"Material: {_listing(meta['material'])}.")
    if meta.get("color"):
        lines.append(f"Färg: {_listing(meta['color'])}.")
    defects = [d for d in (meta.get("defects") or []) if isinstance(d, dict)]
    if defects:
        parts = []
        for d in defects:
            what = (d.get("type") or "defekt").lower()
            where = (d.get("location") or "").lower()
            parts.append(f"{what} ({where})" if where else what)
        lines.append(f"Defekter: ja, {'; '.join(parts)}. Se bilderna.")
    else:
        lines.append("Defekter: inga noterade.")
    lines.append("Fråga gärna om du undrar något.")
    text = "\n".join(line for line in lines if line)
    return text[:mapping()["limits"]["description_max"]]


def _category_path(doc: dict) -> str | None:
    cats = doc.get("categories") or {}
    return next((cats[f"lvl{i}"][0] for i in (2, 1, 0) if cats.get(f"lvl{i}")), None)


# --- the draft -------------------------------------------------------------------

def build_draft(doc: dict, image_paths: list[str] | None = None,
                photos: list[str] | None = None, expected_now_ore: int | None = None,
                multiple: float = 2.0, use_shortlist: bool = True) -> dict:
    """One Plick draft from a search-index record. Pure: no network, no disk."""
    meta = doc.get("metadata") or {}
    category_src = _category_path(doc)
    size_code = meta.get("size") or next(iter(doc.get("sizes") or []), None)
    demography = meta.get("demography")
    ask_ore = (doc.get("price_SE") or {}).get("amount")
    reasons: list[str] = []

    category, r = map_category(category_src, meta.get("type"))
    reasons += r
    size, size_label, r = map_size(size_code, demography)
    reasons += r
    condition, r = map_condition(meta.get("condition"))
    reasons += r
    fit, r = passform(demography)
    reasons += r
    price, basis = price_kr(ask_ore, expected_now_ore, multiple, use_shortlist=use_shortlist)
    if price is None:
        reasons.append("price missing: no ask and no shortlist expectation")
    elif ask_ore and price * 100 <= ask_ore:
        reasons.append(f"price {price} kr is at or below the current ask")
    if not meta.get("brand"):
        reasons.append("brand missing")
    if not image_paths:
        reasons.append("no images on the item")
    elif photos is not None and len(photos) < len(image_paths):
        reasons.append(f"only {len(photos)} of {len(image_paths)} photos downloaded")

    return {
        "item_id": doc.get("objectID") or doc.get("id"),
        "venue": VENUE,
        "title": make_title(meta.get("brand"), meta.get("type"), size_label),
        "description": make_description(meta, condition, size_label),
        "category": category,
        "passform": fit,
        "brand": meta.get("brand"),
        "colour": _listing(meta.get("color")) or None,
        "size": size,
        "condition": condition,
        "price_kr": price,
        "price_basis": basis,
        "photos": photos or [],
        "image_paths": image_paths or [],
        "photo_source": "marketplace",
        "needs_review": bool(reasons),
        "review_reasons": reasons,
        "source": {"category": category_src, "item_type": meta.get("type"),
                   "size_code": size_code, "condition": meta.get("condition"),
                   "ask_kr": ask_ore / 100 if ask_ore else None},
        "drafted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


# --- network and storage ---------------------------------------------------------

def _ext(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def download_photos(item_id: str, paths: list[str], refresh: bool = False) -> list[str]:
    """Every image, in order, as 01.jpg, 02.jpg ... Already-downloaded files are kept.

    Errors print the exception type only: the URL carries the image host, which
    stays out of logs for the same reason it stays out of the source.
    """
    from loppan import endpoints
    host = endpoints.image_hosts()[0]
    out_dir = data_dir() / "photos" / item_id
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for i, path in enumerate(paths, 1):
        if path.startswith(("http://", "https://")):
            print(f"  {item_id} photo {i}: not on a known image host, skipped", file=sys.stderr)
            continue
        have = sorted(out_dir.glob(f"{i:02d}.*"))
        if have and not refresh:
            saved.append(str(have[0]))
            continue
        req = urllib.request.Request(host + urllib.parse.quote(path, safe="/%-._~"),
                                     headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as resp:
                data = resp.read()
        except Exception as exc:  # noqa: BLE001 -- one bad photo must not stop the rest
            print(f"  {item_id} photo {i}: {type(exc).__name__}", file=sys.stderr)
            continue
        target = out_dir / f"{i:02d}{_ext(data)}"
        target.write_bytes(data)
        saved.append(str(target))
    return saved


def _in_list(ids: list[str]) -> str:
    return "in.(" + ",".join(urllib.parse.quote(i, safe="") for i in ids) + ")"


def shortlist_expected(ids: list[str]) -> dict[str, int]:
    """item_id -> expected_now_ore from the newest shortlist day that has the item."""
    from loppan import db
    rows = db.query(f"shortlist?select=item_id,expected_now_ore,as_of"
                    f"&item_id={_in_list(ids)}&order=as_of.desc")
    out: dict[str, int] = {}
    for r in rows:
        if r.get("expected_now_ore") and r["item_id"] not in out:
            out[r["item_id"]] = r["expected_now_ore"]
    return out


def save(drafts: list[dict]) -> tuple[int, list[str]]:
    """Upsert as status='draft'. Rows already past draft are left alone, not reset.

    `note`, `listing_url` and the timestamps are deliberately absent from the payload:
    a merge-duplicates upsert only touches the columns it is given, so a note written
    by hand survives a re-draft.
    """
    from loppan import db
    ids = [d["item_id"] for d in drafts]
    existing = db.query(f"listings?select=item_id,status&venue=eq.{VENUE}&item_id={_in_list(ids)}")
    locked = sorted(r["item_id"] for r in existing if r["status"] != "draft")
    rows = [{"item_id": d["item_id"], "venue": VENUE, "status": "draft",
             "price_listed_ore": d["price_kr"] * 100 if d["price_kr"] else None,
             "draft": d} for d in drafts if d["item_id"] not in locked]
    return db.upsert("listings", rows, on_conflict="item_id,venue"), locked


def _item_id(value: str) -> str:
    if not value.isascii() or not value.isalnum():
        raise argparse.ArgumentTypeError(f"not an item id: {value!r}")
    return value


def main() -> None:
    ap = argparse.ArgumentParser(description="Plick listing drafts for marketplace items.")
    ap.add_argument("ids", nargs="+", type=_item_id, help="item ids")
    ap.add_argument("--save", action="store_true", help="upsert drafts to public.listings")
    ap.add_argument("--multiple", type=float, default=2.0,
                    help="price = this x current ask when the shortlist has no expectation")
    ap.add_argument("--no-shortlist", action="store_true",
                    help="ignore the shortlist expectation and always use --multiple")
    ap.add_argument("--no-photos", action="store_true", help="skip downloading photos")
    ap.add_argument("--refresh-photos", action="store_true", help="re-download photos")
    args = ap.parse_args()

    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8")
    from loppan import algolia, db, search

    expected: dict[str, int] = {}
    if not args.no_shortlist:
        if db.configured():
            expected = shortlist_expected(args.ids)
        else:
            print("LOPPAN_SUPABASE_KEY not set: shortlist not read, pricing by --multiple",
                  file=sys.stderr)

    drafts_dir = data_dir() / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    drafts = []
    for item_id, doc in zip(args.ids, algolia.get_objects(args.ids)):
        if doc is None:
            print(f"{item_id}: not in the search index (sold or removed), no draft",
                  file=sys.stderr)
            continue
        paths = search.image_paths(doc.get("images"))
        photos = None if args.no_photos else download_photos(item_id, paths,
                                                             args.refresh_photos)
        draft = build_draft(doc, paths, photos, expected.get(item_id),
                            args.multiple, use_shortlist=not args.no_shortlist)
        text = json.dumps(draft, ensure_ascii=False, indent=2)
        (drafts_dir / f"{item_id}.json").write_text(text, encoding="utf-8")
        print(text)
        drafts.append(draft)

    review = sum(d["needs_review"] for d in drafts)
    print(f"{len(drafts)} drafts, {review} need review, written to {drafts_dir}",
          file=sys.stderr)
    if args.save and drafts:
        if not db.configured():
            sys.exit("LOPPAN_SUPABASE_KEY is not set; nothing saved")
        written, locked = save(drafts)
        print(f"saved {written} rows to public.listings as draft", file=sys.stderr)
        if locked:
            print(f"left alone (already past draft): {', '.join(locked)}", file=sys.stderr)


if __name__ == "__main__":
    main()
