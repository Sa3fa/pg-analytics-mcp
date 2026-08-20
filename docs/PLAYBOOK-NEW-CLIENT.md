# Playbook — provisioning a read-only analytics MCP for a new client

Derived from a real production build. Every gotcha here cost real time.

**The rule that matters most: verify each layer before building the next.**
That build failed for hours because five layers were stacked before any
were tested, and the visible error never pointed at the broken one.

---

## 0. Parameters — decide these first

| Placeholder | Example | Notes |
| --- | --- | --- |
| `CLIENT` | `acme` | short slug |
| `ZONE` | `example.com` | must be a zone in the Cloudflare account |
| `ORIGIN_HOST` | `mcp-origin.example.com` | **use a fresh name, never one with history** |
| `PORTAL_HOST` | `portal.example.com` | separate hostname from the origin |
| `DB_ROLE` | `claude_analytics` | its own role, never reuse an app role |
| `PROJECT_REF` | `abcdefghijklmnopqrst` | Supabase project ref |
| `ADMIN_EMAIL` | `you@example.com` | the **Access** identity, not the Cloudflare dashboard login |

`ADMIN_EMAIL` is the address your identity provider issues. It is often *not*
the email on the Cloudflare account.

## Prerequisites

- Docker host that can reach the database (if the DB has IP allowlisting, this
  host's egress IP must be on it).
- A `cloudflared` tunnel already serving the zone.
- Cloudflare Zero Trust with an identity provider configured (one-time PIN is
  enough).
- A paid Claude plan (custom connectors are not on free).

---

## Phase 1 — Database: least privilege

Run as a superuser in the SQL editor. **The `create role` statement must be run
by a human** — it contains a password and agent harnesses correctly refuse it.

```sql
create role DB_ROLE login password 'GENERATE_A_STRONG_ONE';
```

Then the rest, which contains no secrets and can be applied as a migration:

```sql
alter role DB_ROLE set default_transaction_read_only = on;
alter role DB_ROLE set statement_timeout = '20s';
alter role DB_ROLE set idle_in_transaction_session_timeout = '60s';
alter role DB_ROLE set search_path = analytics;
alter role DB_ROLE connection limit 20;

create schema if not exists analytics;

revoke all on schema public from DB_ROLE;
revoke all on all tables in schema public from DB_ROLE;

-- One view per exposed table. Owned by the superuser and intentionally NOT
-- security_invoker, so they read base tables with owner rights, past RLS.
create or replace view analytics.example as
select id, safe_column, (pii_url is not null) as has_pii_url
from public.example;

grant usage on schema analytics to DB_ROLE;
grant select on all tables in schema analytics to DB_ROLE;
alter default privileges in schema analytics grant select on tables to DB_ROLE;
```

**Verify column names against `information_schema` before writing views** — one
wrong column fails the whole migration:

```sql
select table_name, string_agg(column_name, ', ' order by ordinal_position)
from information_schema.columns
where table_schema='public' and table_name in ('example','...')
group by table_name;
```

**PII discipline.** Drop names, phone numbers, free-text notes, raw gateway
payloads, and external references. Reduce URLs to booleans
(`receipt_link` → `has_receipt`) so KPIs survive without exposing the value.
Never expose raw order/webhook tables.

Never put the password in a migration — migration SQL is stored in
`supabase_migrations.schema_migrations` in plaintext.

### ✅ Gate 1

```sql
select has_schema_privilege('DB_ROLE','analytics','usage')        -- true
     , has_table_privilege('DB_ROLE','analytics.example','select') -- true
     , has_table_privilege('DB_ROLE','public.example','select')    -- FALSE
     , has_table_privilege('DB_ROLE','analytics.example','update');-- FALSE
```

Any `true` in the last two is a security bug. Stop and fix grants.

> On PG15+ query 3 fails with *"permission denied for table"*, not *"for
> schema public"* — `USAGE` on `public` is granted to `PUBLIC` by default.
> Don't revoke that; it affects every other role.

---

## Phase 2 — Container

Use the `analytics-mcp` server in this repo. It serves Streamable HTTP at `/mcp`
natively, so there is no bridge process and no per-session child to leak.

```bash
cp .env.example .env      # DATABASE_URI + deployment vars
$EDITOR config/<client>.yaml
docker compose up -d --build
```

`.env`:

```
DATABASE_URI=postgresql://ROLE.PROJECT_REF:PASSWORD@HOST:PORT/postgres?sslmode=require
MCP_CONTAINER_NAME=<client>-analytics-mcp
MCP_HOSTNAME=mcp-origin.<zone>
TRAEFIK_NETWORK=web
MCP_CONFIG=/app/config/<client>.yaml
MCP_LOCAL_PORT=8000
```

**Pooler trap:** on the Supavisor pooler the username must carry the project ref
after a dot. Getting it wrong looks exactly like a wrong password.

**Do not list columns or enum values in the YAML** — they are introspected from
the live database at boot. Write only business meaning and traps.

### ✅ Gate 2

```bash
docker compose up -d --build
docker logs acme-analytics-mcp --tail 20   # expect "Successfully connected to database"
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/healthz   # 200
```

---

## Phase 3 — Tunnel

**First, find out how the tunnel is managed:**

```bash
docker inspect cloudflared --format '{{json .Config.Cmd}}{{json .Mounts}}'
```

If it runs `tunnel run --token …` with no mounts, it is **remotely managed** —
routing lives in the dashboard and a local `config.yml` is ignored.

Also check its networks. If `cloudflared` is not on the host network, then
`http://localhost:8000` means *cloudflared itself*. Route via the reverse proxy
instead.

Zero Trust → Networks → Tunnels → your tunnel → **Public Hostnames** → Add:

| Field | Value |
| --- | --- |
| Subdomain | `mcp-origin` |
| Domain | `ZONE` |
| Type | `HTTP` |
| URL | `traefik:80` ← the proxy, **not** localhost |

DNS is created automatically and proxied, which portals require.

### ✅ Gate 3

```bash
TIP=$(docker inspect traefik --format '{{(index .NetworkSettings.Networks "web").IPAddress}}')
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: ORIGIN_HOST' "http://$TIP/healthz"  # 200
curl -s https://ORIGIN_HOST/healthz | head -c 40
```

Public response now is **error 1043** ("cannot find Access application"). That
is correct — it proves Require Access Protection is enforcing.

---

## Phase 4 — Access application on the origin

Access → Applications → Add → **Self-hosted**:

- Domain: `ORIGIN_HOST`, **path empty**
- Policy: **Allow**, Emails = `ADMIN_EMAIL` (never Bypass)
- Advanced → **Managed OAuth: ON**
- Allow localhost / loopback clients: **off**

Managed OAuth must be on: your MCP server implements no OAuth of its own, so
Access has to be the OAuth provider for it.

### ✅ Gate 4

```bash
curl -s -D- -o /dev/null https://ORIGIN_HOST/mcp | grep -iE '^HTTP|www-authenticate'
curl -s https://ORIGIN_HOST/.well-known/oauth-protected-resource/mcp
```

Expect **`401`** with `www-authenticate: Bearer realm="OAuth"`, and JSON naming
your team domain as the authorization server.

**A `302` means Managed OAuth is off** — that's a browser login flow, which an
MCP client cannot complete.

---

## Phase 5 — Portal

Access → AI Controls → **MCP server portals** → create:

- Domain: `PORTAL_HOST` (its own hostname — no tunnel entry, Cloudflare serves it)
- Policy: **Allow**, Emails = `ADMIN_EMAIL`
- Managed OAuth **ON**, Allowed redirect URIs:
  `https://claude.ai/*` and `https://claude.com/*`

There are **two independent OAuth relationships**. Each resource needs Managed
OAuth on, with the client's callback allowlisted:

| Client | Resource | Allowed redirect URIs on the resource |
| --- | --- | --- |
| Claude | `PORTAL_HOST` | `https://claude.ai/*`, `https://claude.com/*` |
| Cloudflare admin auth | `ORIGIN_HOST` | the `dash.cloudflare.com/...` callback (Phase 6) |

---

## Phase 6 — Register the MCP server

Access → AI Controls → MCP servers → **Add an MCP server**:

- URL: `https://ORIGIN_HOST/mcp` — **full URL with scheme and `/mcp` path**
- OAuth credentials: **Automatic**
- Use Cloudflare-hosted OAuth callback: **ON** (see below)
- Route outbound through Gateway: **OFF**
- Attach an **Allow** policy with `ADMIN_EMAIL`

Save it, then click **Authenticate now**. It will fail the first time and
report the redirect URI it used — a URL like:

```
https://dash.cloudflare.com/<account>/one/access-controls/ai-controls/mcp-server/oauth-callback/<server-id>
```

Add that exact URL to the **origin** app's Allowed redirect URIs (Phase 4),
wait ~1 minute for propagation, then click **Authenticate now** again.

**Turn the Cloudflare-hosted (shared) callback ON.** With it off, admin
authentication registers a client whose only redirect URI is the
`dash.cloudflare.com` one — so *end users* clicking the server in the portal
are rejected, because their flow sends the portal's callback instead. The
shared callback gives both flows one fixed URL and one client registration.
Allowlist all three URIs (shared, portal, dash) and leave them there: pruning
to one is what breaks per-user auth.

### ✅ Gate 6 — the DCR oracle

You can test the redirect-URI allowlist without touching the dashboard:

```bash
curl -s -X POST https://<team>.cloudflareaccess.com/cdn-cgi/access/oauth/registration \
  -H 'Content-Type: application/json' \
  -d '{"client_name":"probe","redirect_uris":["<URI to test>"],
       "grant_types":["authorization_code"],"response_types":["code"],
       "token_endpoint_auth_method":"none"}'
```

`client_id` returned = allowed. `invalid_client_metadata` = not allowed.
Propagation takes up to a minute, so retry before concluding it failed.

> **It only tests one of three layers.** This endpoint validates the
> **account/application allowlist** — what dynamic registration will *accept*.
> It does **not** tell you whether an authorization request will succeed.
> Authorization validates `redirect_uri` against the **already-registered
> client's** own `redirect_uris`, which were fixed when that client was
> created. A URI can pass this probe and still be rejected at authorization.
> Only running the real flow proves that path.

> Each probe creates a **real OAuth client that cannot be deleted** — Cloudflare
> issues no registration access token and `DELETE` returns 404. Probe
> sparingly.

Then check the server leaves `Waiting`, and confirm from the origin side:

```bash
docker logs acme-analytics-mcp | grep -c 'StreamableHttp → Child'
```

**A count of 0 means Cloudflare never reached you** — the failure is entirely
in Zero Trust config, not your server. Note `.well-known` discovery is
synthesised at Cloudflare's edge and never reaches the origin, so only
authenticated MCP calls show up here.

---

## Phase 7 — Connect Claude

Deep link straight to the dialog:

```
https://claude.ai/new?modal=add-custom-connector#settings/customize-connectors
```

Paste `https://PORTAL_HOST/mcp`, complete the Access login.

### ✅ Gate 7 — acceptance

Ask Claude:

1. "list the tables you can see" → exactly your `analytics` views
2. "total X by Y" → works
3. "show me <a PII column>" → **must fail**

---

## Troubleshooting — symptom to cause

| Symptom | Cause | Fix |
| --- | --- | --- |
| `The URL is invalid` | Bare hostname entered | Use full URL with scheme + `/mcp` |
| `error code: 1043` | Tunnel hostname has no Access application | Create the app (Phase 4) |
| Origin returns `302`, not `401` | Managed OAuth off on the origin app | Turn it on |
| `redirect_uri is not allowed by the account configuration` | Callback missing from allowed redirect URIs | Add it; wait ~1 min |
| `50003 Registration was not successful` | Wrong callback allowlisted | Turn the shared callback OFF; allowlist the `dash.cloudflare.com` one |
| `access.api.error.unknown_application` | Record points at a deleted Access app | Delete and recreate — editing keeps the stale binding |
| `No allowed servers available` | Server not authenticated, or no Allow policy on the **server** | Authenticate; policies are per-portal *and* per-server |
| Server stuck `Waiting`, 0 tools | Registered `/sse`, or origin unreachable | Use `/mcp`; check DNS actually resolves |
| Works ~9 calls then all fail, incl. `SELECT 1` | Connection leak (see below) | Restart; lower `--sessionTimeout` |
| `Redirect URI not allowed by application configuration` (after allowlisting it) | The **registered client** is stale — its `redirect_uris` were fixed at registration | Enable the shared callback and click **Authenticate now** to register a new client |
| `Invalid state: missing or invalid nonce` | Stale state from earlier failed attempts | Clear cookies for the portal and `*.cloudflareaccess.com`, retry once in a single uninterrupted pass |
| Admin works, end users get authorization errors | Admin and per-user flows use different callbacks | Shared callback ON, re-authenticate |
| `Error validating query` | Restricted-mode SQL validator rejects a valid read-only construct (e.g. `AT TIME ZONE`) | Use `--access-mode=unrestricted` — the role is the real boundary; verify with the write/PII tests below |

---

## Known issues to hand over

**Connection leak — architectural.** `supergateway` spawns one `postgres-mcp`
child *per MCP session*, each with its own pool, and they don't exit promptly.
Measured 23 children / 15 connections after light use. Symptom is a clean run
then a cliff, including bare `SELECT 1`.

```bash
docker exec acme-analytics-mcp sh -c "ps -eo pid,etime,args | grep '[p]ostgres-mcp'"
```
```sql
select count(*), max(now()-backend_start) from pg_stat_activity where usename='DB_ROLE';
```

Mitigated with `--sessionTimeout 60000`. **Do not fix by raising
`CONNECTION LIMIT`** — `max_connections` is small (90 on Supabase) and shared
with your app. A durable fix needs a bridge running a single shared child.

**Access mode.** Prefer `--access-mode=unrestricted`. Restricted mode adds a
SQL validator that rejects valid read-only constructs (`AT TIME ZONE`) while
adding nothing the role doesn't enforce more strictly. After switching, prove
the boundary still holds — all five must fail:

```sql
update analytics.example set col = 0 where false;  -- permission denied for view
select count(*) from public.example;               -- permission denied for table
select count(*) from public.raw_pii_table;         -- permission denied for table
create table analytics.t (id int);                 -- read-only transaction
select pii_column from analytics.example limit 1;  -- column does not exist
```

Restricted mode also supplies a 30s query cap; confirm the role's own
`statement_timeout` is lower before removing it:
`select current_setting('statement_timeout')`.

**Timezone.** The session timezone may already be the client's zone — check
`current_setting('TimeZone')` before writing conversions.

**Agent context.** Give the model a skill or project instructions describing
the `analytics` views, enum values (**verify against `pg_enum`, don't trust an
existing prompt**), and what the connector deliberately cannot answer.


---

## Appendix — retired architecture

Earlier deployments used `supergateway → enrich.py → postgres-mcp`. That is
superseded by the server in this repo. If you meet one in the wild, the known
defects are: a per-session child process that is never reaped (connection
exhaustion after roughly a dozen calls), no configuration surface, and
hand-written enum lists that go stale. Migrate rather than patch.
