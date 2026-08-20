-- Example: read-only, PII-free analytics surface for the MCP server.
-- Run as a superuser in your Postgres / Supabase SQL editor. Idempotent.
-- Idempotent: safe to re-run.

-- =============================================================
-- 1. The role Claude connects as
-- =============================================================
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'claude_analytics') then
    create role claude_analytics login password 'REPLACE_ME';
  end if;
end $$;

-- Defence in depth: even if the MCP server's own read-only guard is
-- bypassed, the session itself cannot write.
alter role claude_analytics set default_transaction_read_only = on;
alter role claude_analytics set statement_timeout = '20s';
alter role claude_analytics set idle_in_transaction_session_timeout = '60s';
alter role claude_analytics set search_path = analytics;
alter role claude_analytics connection limit 5;

-- =============================================================
-- 2. Analytics schema — the ONLY thing the role can see
-- =============================================================
create schema if not exists analytics;

-- Claude gets no access to `public` at all. Views below are owned by
-- `postgres`, so they read the base tables with the owner's rights
-- (and past RLS). Do NOT add security_invoker=true — that breaks this.
revoke all on schema public from claude_analytics;
revoke all on all tables in schema public from claude_analytics;

-- =============================================================
-- 3. Views: analytics columns only, donor PII dropped
-- =============================================================

-- donations: drops raw, whatsapp, notes, memo (free text / gateway blobs
-- that carry donor names and phone numbers). receipt_link becomes a
-- boolean because the KPI only needs its existence, not the URL.
create or replace view analytics.donations as
select
  id,
  order_num,
  customer_id,
  treasure_id,
  recurring_subscription_id,
  amount,
  fee,
  payment_processor,
  donation_platform,
  city,
  donation_channel,
  (receipt_link is not null) as has_receipt,
  whatsapp_accepted,
  donated_at,
  created_at
from public.donations;

-- customers: drops display_name, phone_number, provider_ref.
create or replace view analytics.customers as
select
  id,
  is_anonymous,
  city,
  platforms,
  first_donation_at,
  last_donation_at,
  lifetime_amount,
  donation_count,
  created_at
from public.customers;

-- recurring_subscriptions: drops cancellation_reason (donor free text).
create or replace view analytics.recurring_subscriptions as
select
  id,
  customer_id,
  amount,
  frequency,
  day_of_month,
  day_of_week,
  day_of_year,
  status,
  processor,
  initial_donation_id,
  started_at,
  next_charge_at,
  canceled_at,
  created_at
from public.recurring_subscriptions;

-- No donor PII in these three.
create or replace view analytics.donation_items as
select id, donation_id, product_name, unit_price, quantity
from public.donation_items;

create or replace view analytics.treasures as
select id, name, subtitle, is_active, goal, created_at
from public.treasures;

create or replace view analytics.monthly_stats as
select id, hospital, year, month, total_kids, total_radiology,
       total_chemo, total_checks, iraq_cities, submitted_at, created_at
from public.monthly_stats;

-- Deliberately NOT exposed: website_orders, opay_website_orders
-- (raw gateway blobs full of PII), and every auth/storage schema.

-- =============================================================
-- 4. Grants
-- =============================================================
grant usage on schema analytics to claude_analytics;
grant select on all tables in schema analytics to claude_analytics;
alter default privileges in schema analytics
  grant select on tables to claude_analytics;
