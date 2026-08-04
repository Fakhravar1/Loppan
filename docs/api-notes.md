# Sellpy backend — what the API will and won't do

Established empirically 2026-08-04. Read this before writing a query; several
obvious-looking ones fail, and the failure mode is uninformative.

## Endpoints

| Layer | URL |
|---|---|
| Parse REST | `https://sellpy-parse-prod.herokuapp.com/parse` |
| GraphQL | `https://sellpy-parse-prod.herokuapp.com/graphql` (introspection disabled) |

Credentials are the browser SDK's `applicationId` + `javascriptKey`, served in
plain text in `https://www.sellpy.se/market/index.*.bundle.js`. Public-by-design
client keys. If they rotate, re-read them from the bundle.

No Cloudflare, no bot protection, no user-agent check. The site itself is a
client-side SPA — the HTML is a 6 kB empty shell, so there is nothing to scrape
there and everything to read here.

## The one failure mode you need to recognise

**Any query the server cannot serve in ~10 seconds returns a bare `HTTP 500`.**
It is not a malformed-request error. It nearly always means the field you
constrained or sorted on is unindexed. Timing confirms it: every failure below
took 10.2–10.4 s, every success was under 4 s.

`sellpy.find()` raises `QueryTooSlow` on 500 for exactly this reason.

## Class access

| Class | Read by id | Query by other fields |
|---|---|---|
| `Item` | ✅ | ❌ — returns `{"results":[]}` for any constraint except `objectId` |
| `MarketOffer` | ✅ | ✅ — but only certain shapes, see below |
| `ItemCategory`, `ItemType` | ✅ | — |
| `ItemReservation`, `MarketOffer` | ✅ | — |
| `ItemBrand`, `ItemBlob`, `CircleItem`, `Sale` | ❌ permission denied | — |

Consequence: **`Item` cannot be enumerated.** Discovery has to run through
`MarketOffer`, and `Item` is enrichment-by-id on top of it.

## MarketOffer query shapes

Base filter `{"region": "SE", "latest": true}`.

| Query | Result |
|---|---|
| base, `limit=1000`, no order | ✅ 0.5 s |
| base, `limit=1000`, `skip` up to 8000 | ✅ 0.5–1.0 s, **no overlap between pages** |
| base, `skip=10000` | ❌ timeout |
| base, `order=objectId`, `limit≤100` | ✅ 3.7 s |
| base, `order=objectId`, `limit≥200` | ❌ timeout |
| base, `order=createdAt` | ❌ timeout |
| base, `order=-pricing.amount` | ❌ timeout |
| base + `include=item`, `limit=100` | ✅ 3.7 s — returns the **full 54-field Item inline** |
| base + `include=item`, `limit≥200` | ❌ timeout |
| base + `pricing.amount` range, `limit=2` | ✅ |
| base + `pricing.amount` range, `limit=100` | ❌ timeout — **price is not indexed** |
| base + `objectId: {$gt: …}` keyset | ✅ alone, ❌ combined with `include` |
| `createdAt` range filter | ❌ timeout — **date is not indexed** |
| `first: true` | ❌ timeout |
| by `item` pointer (one item's full ladder) | ✅ fast, returns every historical step |

### What that means in practice

- **Price-band filtering must happen client-side.** Pull unfiltered pages, bucket
  locally. Server-side band filters look like they work at `limit=2` and then die
  at any useful page size.
- **Paginate with `skip` in 1000-row pages, unordered.** Verified 5,000 rows
  across five pages with zero duplicates. Deep skip (≥10000) times out, so a
  single unordered scan tops out around 9,000 rows per filter — take a fresh
  sample rather than trying to walk the whole catalogue.
- **`include=item` is worth it when you want ≤100 rows** with full detail in one
  call. Above that, enumerate cheaply and enrich by id.
- **Per-item history is cheap and unrestricted.** `ladder(item_id)` returns the
  complete markdown sequence with `createdAt` / `endedAt` per step, for items that
  sold months or years ago.

## Field notes

- **Filter `region`.** One price step exists once per market; a single step
  returned 11 rows across currencies. `SE` gives SEK.
- **`first` / `latest`** flag the opening ask and the final/current one.
- **`latest: true` includes items that already ended.** Split on `endedAt`:
  absent = still listed, present = sold or expired.
- **Sale date is the last offer's `endedAt`, not `Item.paidAt`.** `paidAt` is the
  seller payout — same day for consignment, but **21–24 days later for Circle**
  sales (measured, n=3).
- **`Item.itemStatus`** — `utlagd` listed, `betald` sold, `vilande` dormant
  (observed after a cancelled Circle sale).
- **Circle listings are ordinary `Item` records** carrying
  `p2pValueShare: {version: 1, customerShare: 0.8}`.
- **`sellabilityEstimate`** `{score, cutoff, isReliable, version}` — Sellpy's own
  sell-probability. **Key any analysis by `version`**: `"3"` and `"3-mla"` both
  seen in the wild, so the model has already rolled at least once and scores are
  not comparable across versions.
- **Rich metadata is sparse but excellent where present**: `productId` /
  `variantId` (manufacturer codes — exact comparables, no fuzzy matching),
  `season` (`["Höst","Vinter"]`), `defects` (structured, with location).

## Observed population shape

From an unbiased 5,000-offer sample (2026-08-04), latest offers, region SE:

| | median | p90 | p99 | max | ≥2000 kr |
|---|---|---|---|---|---|
| all | 60 kr | 260 | 900 | 3,920 | **0.06%** |
| still listed (n=478) | 65 kr | 360 | — | 3,920 | 1 item |
| ended (n=4,522) | 55 kr | 250 | — | 2,360 | 2 items |

**Sellpy's live catalogue is overwhelmingly cheap.** 96% of current asking prices
are under 500 kr. Any strategy that depends on a supply of high-value items has a
sourcing problem, and defining a "premium" band on *current* price will find
almost nothing — define it on the **opening ask** instead, since expensive items
are marked down into the cheap bands rather than staying expensive.

## Conduct

`robots.txt` allows `/item/*` and disallows the search paths. These are
undocumented endpoints being used outside a browser, so the constraint is
self-imposed: one request per second (`sellpy.MIN_INTERVAL_S`), no distributed
crawling, no redistribution of the data, one account. The exposure that matters
isn't the scraper breaking — it's the account.
