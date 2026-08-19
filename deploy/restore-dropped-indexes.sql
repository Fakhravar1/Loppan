-- Restores the indexes dropped on 2026-08-17 to bring the database back under
-- Supabase's 500 MB limit. Run this only if something turns out to have needed one.
--
-- Why they went. Every one of these had near-zero scans across a 24-day stats window
-- (pg_stat_database.stats_reset = 2026-07-24). The five on `shortlist_daily` share a
-- deeper problem: they all lead with `as_of`, which was selective when the table was
-- "~500/day, 30-day window" and is not any more. The pool is ~133,000 rows carrying
-- 4 distinct `as_of` values, one of which holds 75% of them, and `pool_refresh.py`
-- rewrites the column for every row daily -- so each of these indexes was rebuilt in
-- full every day to serve a leading column the planner had no reason to use.
--
-- If a dashboard sort does turn out to need one, recreate it WITHOUT the `as_of`
-- prefix rather than pasting the line below verbatim. The prefix is the defect.

CREATE INDEX shortlist_disc_idx  ON public.shortlist_daily USING btree (as_of, discount_pct DESC);        -- 13.9 MB, 5 scans
CREATE INDEX shortlist_sell_idx  ON public.shortlist_daily USING btree (as_of, brand_sell_pct_day DESC);  -- 11.2 MB, 3 scans
CREATE INDEX shortlist_price_idx ON public.shortlist_daily USING btree (as_of, price_kr);                 --  4.5 MB, 0 scans
CREATE INDEX shortlist_peern_idx ON public.shortlist_daily USING btree (as_of, peer_n DESC);              --  4.4 MB, 5 scans
CREATE INDEX shortlist_brand_idx ON public.shortlist_daily USING btree (as_of, brand);                    --  4.2 MB, 24 scans

CREATE INDEX items_peer_pct_frozen_idx ON public.items USING btree (peer_pct_frozen)
    WHERE (peer_pct_frozen IS NOT NULL);                                                                  --  3.5 MB, 1 scan

CREATE INDEX circle_origins_original_id_idx ON public.circle_origins USING btree (original_id);           --  0.6 MB, 0 scans

-- Deliberately NOT dropped, for the record:
--   shortlist_sizekey_idx (481 scans) · shortlist_daily_size_idx (151) ·
--   shortlist_daily_area_idx (128) · shortlist_favs_idx (84)
--     the "my sizes" panel and the favourites sort genuinely use these.
--   items_live_idx (104) · peer_live_l2 (6) · peer_prices_pct_idx (207)
--     few scans, but each is once-per-pass structural work over 600k rows.
