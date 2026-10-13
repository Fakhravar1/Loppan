-- Supabase side of the item-type price and the discount ranking (PR #17).
-- Run once in the Supabase SQL editor before #17 merges, because the
-- export sends these columns and would fail without them. Additive and safe to rerun.

alter table public.shortlist_staging
  add column if not exists type_n_sales          integer,
  add column if not exists expected_type_now_ore integer,
  add column if not exists pct_of_expected_type  numeric,
  add column if not exists discount_ore          integer,
  add column if not exists discount_rank         integer;

alter table public.shortlist
  add column if not exists type_n_sales          integer,
  add column if not exists expected_type_now_ore integer,
  add column if not exists pct_of_expected_type  numeric,
  add column if not exists discount_ore          integer,
  add column if not exists discount_rank         integer;

create or replace function public.promote_shortlist()
 returns integer
 language plpgsql
 set search_path to ''
as $function$
declare
  n integer;
begin
  -- Refuse an empty swap: an export that staged nothing must not blank the dashboard.
  if not exists (select 1 from public.shortlist_staging) then
    raise exception 'shortlist_staging is empty: refusing to replace the shortlist with nothing';
  end if;
  truncate public.shortlist;
  insert into public.shortlist (
    item_id, brand, category, item_type, size_code, condition, demography, p2p,
    history_complete, weight_g, price_ore, n_sales, sell_through, expected_now_ore,
    expected_peak_ore, peak_month, months_to_peak, pct_of_expected, gross_margin_ore,
    expected_profit_ore, signal, image_paths, as_of,
    type_n_sales, expected_type_now_ore, pct_of_expected_type, discount_ore, discount_rank)
  select
    item_id, brand, category, item_type, size_code, condition, demography, p2p,
    history_complete, weight_g, price_ore, n_sales, sell_through, expected_now_ore,
    expected_peak_ore, peak_month, months_to_peak, pct_of_expected, gross_margin_ore,
    expected_profit_ore, signal, image_paths, as_of,
    type_n_sales, expected_type_now_ore, pct_of_expected_type, discount_ore, discount_rank
  from public.shortlist_staging;
  get diagnostics n = row_count;
  truncate public.shortlist_staging;
  return n;
end
$function$;

-- New sort 'discount': BigQuery's discount_rank ('now' first, then kronor of discount).
-- Until the first export with ranks it falls through to the old margin order.
create or replace function public.shortlist_search(f jsonb default '{}'::jsonb, sort text default 'recommended'::text, page_offset integer default 0, page_size integer default 48)
 returns setof public.shortlist
 language sql
 stable
 set search_path to ''
as $function$
  select (t.r).*
  from (
    select x.r,
           row_number() over (partition by (x.r).category order by (x.r).gross_margin_ore desc) as rk
    from public.shortlist_flags(f) x
    where x.m_dept and x.m_l2 and x.m_leaf and x.m_type and x.m_size and x.m_condition
      and x.m_brand and x.m_deal and x.m_evidence and x.m_signal and x.m_rest
  ) t
  order by
    case when sort = 'discount'    then (t.r).discount_rank end asc nulls last,
    case when sort = 'recommended' then t.rk end asc nulls last,
    case when sort = 'deal'        then (t.r).pct_of_expected end asc nulls last,
    case when sort = 'price_asc'   then (t.r).price_ore end asc nulls last,
    case when sort = 'price_desc'  then (t.r).price_ore end desc nulls last,
    case when sort = 'evidence'    then (t.r).n_sales end desc nulls last,
    (t.r).gross_margin_ore desc, (t.r).item_id
  limit least(greatest(page_size, 1), 96)
  offset greatest(page_offset, 0)
$function$;
