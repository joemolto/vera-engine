-- One md5 per schema category for the public schema (plus storage buckets).
-- Run on two databases; matching hashes mean matching structure.
select 'columns' as part, count(*) as n, md5(string_agg(x, '|' order by x)) as hash from (
  select format('%s.%s %s d=%s nn=%s id=%s', c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
    pg_get_expr(d.adbin, d.adrelid), a.attnotnull, a.attidentity) x
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
  join pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
  left join pg_attrdef d on d.adrelid = c.oid and d.adnum = a.attnum
  where n.nspname = 'public' and c.relkind = 'r') t
union all
select 'constraints', count(*), md5(string_agg(x, '|' order by x)) from (
  select format('%s.%s %s', t.relname, con.conname, pg_get_constraintdef(con.oid)) x
  from pg_constraint con join pg_class t on t.oid = con.conrelid join pg_namespace n on n.oid = t.relnamespace
  where n.nspname = 'public' and con.contype <> 'n') t
union all
select 'indexes', count(*), md5(string_agg(x, '|' order by x)) from (
  select pg_get_indexdef(i.indexrelid) x
  from pg_index i join pg_class t on t.oid = i.indrelid join pg_namespace n on n.oid = t.relnamespace
  where n.nspname = 'public') t
union all
select 'functions', count(*), md5(string_agg(x, '|' order by x)) from (
  select regexp_replace(pg_get_functiondef(p.oid), '\s+', ' ', 'g') x
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
  where n.nspname = 'public' and not exists (select 1 from pg_depend d where d.objid = p.oid and d.deptype = 'e')) t
union all
select 'function_acl', count(*), md5(string_agg(x, '|' order by x)) from (
  select p.proname || ':' || coalesce(array_to_string(p.proacl::text[], ' '), '') x
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
  where n.nspname = 'public' and not exists (select 1 from pg_depend d where d.objid = p.oid and d.deptype = 'e')) t
union all
select 'triggers', count(*), md5(string_agg(x, '|' order by x)) from (
  select pg_get_triggerdef(tg.oid) x from pg_trigger tg join pg_class c on c.oid = tg.tgrelid
  join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public' and not tg.tgisinternal
  union all select 'event:' || evtname || ':' || evtfoid::regproc::text from pg_event_trigger where evtname = 'ensure_rls') t
union all
select 'rls_tables', count(*), md5(string_agg(x, '|' order by x)) from (
  select c.relname x from pg_class c join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relkind = 'r' and c.relrowsecurity) t
union all
select 'policies', count(*), md5(string_agg(x, '|' order by x)) from (
  select format('%s.%s %s %s %s u=%s c=%s', tablename, policyname, permissive, cmd, roles, qual, with_check) x
  from pg_policies where schemaname = 'public') t
union all
select 'buckets', count(*), md5(string_agg(x, '|' order by x)) from (
  select format('%s %s %s %s', id, public, file_size_limit, allowed_mime_types) x from storage.buckets) t
order by 1;
