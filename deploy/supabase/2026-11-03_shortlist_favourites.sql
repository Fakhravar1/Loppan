-- Sellpy like count on the Fynd page: column, promote, filter and sort.
-- Run once in the Supabase SQL editor before the export change merges, because the
-- export sends `favourites` and would fail without it. Additive and safe to rerun.

alter table public.shortlist_staging add column if not exists favourites integer;
alter table public.shortlist         add column if not exists favourites integer;

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
    type_n_sales, expected_type_now_ore, pct_of_expected_type, discount_ore, discount_rank,
    favourites)
  select
    item_id, brand, category, item_type, size_code, condition, demography, p2p,
    history_complete, weight_g, price_ore, n_sales, sell_through, expected_now_ore,
    expected_peak_ore, peak_month, months_to_peak, pct_of_expected, gross_margin_ore,
    expected_profit_ore, signal, image_paths, as_of,
    type_n_sales, expected_type_now_ore, pct_of_expected_type, discount_ore, discount_rank,
    favourites
  from public.shortlist_staging;
  get diagnostics n = row_count;
  truncate public.shortlist_staging;
  return n;
end
$function$;

-- New filter key min_favs (part of m_rest, so the facet counts follow it too).
create or replace function public.shortlist_flags(f jsonb default '{}'::jsonb)
 returns table(r public.shortlist, d1 text, d2 text, d3 text, skey text, m_dept boolean, m_l2 boolean, m_leaf boolean, m_type boolean, m_size boolean, m_condition boolean, m_brand boolean, m_deal boolean, m_evidence boolean, m_signal boolean, m_rest boolean)
 language sql
 stable
 set search_path to ''
as $function$
  with p as (
    select
      f->>'dept' as dept,
      f->>'l2'   as l2,
      array(select jsonb_array_elements_text(coalesce(f->'leaf',       '[]'::jsonb))) as leaf,
      array(select jsonb_array_elements_text(coalesce(f->'types',      '[]'::jsonb))) as types,
      array(select jsonb_array_elements_text(coalesce(f->'sizes',      '[]'::jsonb))) as sizes,
      array(select jsonb_array_elements_text(coalesce(f->'conditions', '[]'::jsonb))) as conditions,
      array(select jsonb_array_elements_text(coalesce(f->'brands',     '[]'::jsonb))) as brands,
      (f->>'min_under')::numeric     as min_under,
      (f->>'min_sales')::int         as min_sales,
      f->>'signal'                   as signal,
      (f->>'min_margin_kr')::numeric as min_margin_kr,
      (f->>'max_price_kr')::numeric  as max_price_kr,
      (f->>'circle')::boolean        as circle,
      (f->>'min_favs')::int          as min_favs,
      nullif(f->>'q', '')            as q
  ),
  s as (
    select t as r,
           split_part(t.category, ' > ', 1) as d1,
           split_part(t.category, ' > ', 2) as d2,
           split_part(t.category, ' > ', 3) as d3,
           concat_ws('|', split_part(t.category, ' > ', 1), t.size_group, t.size_system, t.size_value) as skey
    from public.shortlist t
where not exists (select 1 from public.vinted_candidates c where c.item_id = t.item_id)
  )
  select s.r, s.d1, s.d2, s.d3, s.skey,
    (p.dept is null or s.d1 = p.dept),
    (p.l2 is null or s.d2 = p.l2),
    (cardinality(p.leaf) = 0       or s.d3 = any(p.leaf)),
    (cardinality(p.types) = 0      or (s.r).item_type = any(p.types)),
    (cardinality(p.sizes) = 0      or s.skey = any(p.sizes)),
    (cardinality(p.conditions) = 0 or (s.r).condition = any(p.conditions)),
    (cardinality(p.brands) = 0     or (s.r).brand = any(p.brands)),
    (p.min_under is null or (s.r).pct_of_expected <= 100 - p.min_under),
    (p.min_sales is null or (s.r).n_sales >= p.min_sales),
    (p.signal is null or (s.r).signal = p.signal),
    (    (p.min_margin_kr is null or (s.r).gross_margin_ore >= p.min_margin_kr * 100)
     and (p.max_price_kr  is null or (s.r).price_ore <= p.max_price_kr * 100)
     and (p.circle        is null or (s.r).p2p = p.circle)
     and (p.min_favs      is null or p.min_favs <= 0 or (s.r).favourites >= p.min_favs)
     and (p.q is null or (s.r).brand ilike '%' || p.q || '%' or (s.r).item_type ilike '%' || p.q || '%'))
  from s cross join p
$function$;

-- New sort 'likes': most liked first, unknown last.
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
    case when sort = 'likes'       then (t.r).favourites end desc nulls last,
    (t.r).gross_margin_ore desc, (t.r).item_id
  limit least(greatest(page_size, 1), 96)
  offset greatest(page_offset, 0)
$function$;
