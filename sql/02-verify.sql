-- NOTE (verified 2026-08-19): query 2 fails with
--   "permission denied for table donations"
-- not "permission denied for schema public". On PG15+ USAGE on schema public is
-- granted to PUBLIC by default, so the REVOKE below does not remove it. The
-- table-level denial is what matters; USAGE is deliberately NOT revoked from
-- PUBLIC because that would affect every other role, including app_readonly.
--
-- Grants were verified via catalog assertions instead of psql (not installed
-- on the VPS); see README.md.

-- Verification — run these AFTER connecting as claude_analytics
-- (psql from the VPS, or the container). Every one must behave as noted.

-- 1. PASS: the analytics surface is readable.
select count(*) from analytics.donations;

-- 2. MUST FAIL — "permission denied for schema public".
--    Proves the raw ledger, with its PII columns, is unreachable.
select count(*) from public.donations;

-- 3. MUST FAIL — column does not exist on the view.
--    Proves donor phone numbers are not reachable through the view.
select phone_number from analytics.customers;

-- 4. MUST FAIL — "cannot execute UPDATE in a read-only transaction".
update analytics.donations set amount = 0;

-- 5. MUST FAIL — raw blob tables are not exposed.
select count(*) from public.website_orders;

-- 6. Sanity: confirm the session settings actually applied.
show default_transaction_read_only;  -- expect: on
show statement_timeout;              -- expect: 20s

-- 7. What Claude can see — should list exactly the 6 analytics views.
select table_name from information_schema.tables
where table_schema = 'analytics' order by 1;
