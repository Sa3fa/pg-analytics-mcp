# Build notes — where the handoff plan met reality

Built 2026-08-19. The handoff (`HANDOFF-VPS.md`) was directionally right —
DB role, PII-free views, container behind the tunnel, portal for OAuth — but
four things differed materially.

## 1. The tunnel is remotely managed, and traefik is the hop

Handoff assumed `/etc/cloudflared/config.yml` with an `ingress:` list.
Actual: `cloudflared tunnel --no-autoupdate run --token <TOKEN>` in a
container, no mounts. Routing lives in the dashboard; a local config file
would be ignored.

cloudflared is on `web_cloudflare` only, so `http://localhost:8000` is
cloudflared itself. Services are reached through traefik (on both
`web_cloudflare` and `web`) by Host header — the same way the main application is
served. Public hostname target is therefore `traefik:80`.

## 2. Transport: SSE is dead for Cloudflare MCP upstreams

`postgres-mcp` supports only `stdio` and `sse`. Cloudflare Access MCP servers
require **Streamable HTTP**; legacy `GET /sse` returns `410 Gone`. The handoff
predicted this as a risk and named `mcp-proxy` as the fallback.

What was tried:

- `supergateway --sse <url> --outputTransport streamableHttp` → **not supported**
  ("sse→streamableHttp not supported").
- `uvx postgres-mcp` as a stdio child → crashes: `ModuleNotFoundError: No module
  named 'mcp.server.fastmcp'`. Later MCP SDKs removed it.
- **Working:** `supergateway --stdio "postgres-mcp --access-mode=restricted"
  --outputTransport streamableHttp`, with `postgres-mcp==0.3.0` and `mcp==1.6.0`
  pinned at image build time (`Dockerfile`). Those are the versions the upstream
  `crystaldba/postgres-mcp` image shipped.

## 3. The database work was done via MCP, not the SQL editor

The handoff assumed no Supabase access. A project-scoped Supabase MCP server
was added instead, so the schema/views/grants were applied directly.

Role creation was **not** — a `create role … password …` statement is blocked
by the harness classifier, and rightly so. That one statement was run by hand.
Views and grants went in as migrations; the role password deliberately never
entered migration history, which would have stored it in
`supabase_migrations.schema_migrations` in plaintext.

Column names were verified against `information_schema` before creating views —
several handoff assumptions would otherwise have failed the migration.

## 4. Access configuration: the long tail

Roughly in the order they bit:

| Symptom | Cause |
| --- | --- |
| `The URL is invalid` | Bare hostname entered; needs scheme + path |
| `redirect_uri is not allowed by the account configuration` | Managed OAuth "Allowed redirect URIs" empty account-wide |
| Server stuck `Waiting`, 0 tools, no origin traffic | Registered `/sse`; Cloudflare needs `/mcp` |
| `error code: 1043` | Tunnel hostname with no Access application (Require Access Protection is on) |
| Origin `302` instead of `401` | Managed OAuth off on the origin app — 302 is a browser flow an MCP client can't complete |
| `access.api.error.unknown_application` | MCP server record bound to a deleted Access application |
| `50003 Registration was not successful` | Shared-callback toggle on; its URL is `oauth-callbacks.cloudflareaccess.com/...`, not the team domain, and wasn't allowlisted |
| `No allowed servers available` | Server not yet authenticated → hidden from portal regardless of policy |

**Resolution:** fresh hostname (`mcp-origin.example.com`) with no history,
shared-callback toggle **off**, portal created first so its
`/servers-callback` could be allowlisted on the origin app, then admin
Authenticate.

## Debugging technique worth reusing

Cloudflare's DCR endpoint is an oracle for the redirect-URI allowlist. Posting
a client registration tells you whether a given URI is permitted, without
touching the dashboard:

```bash
curl -s -X POST https://<team>.cloudflareaccess.com/cdn-cgi/access/oauth/registration \
  -H 'Content-Type: application/json' \
  -d '{"client_name":"probe","redirect_uris":["https://example.com/cb"],
       "grant_types":["authorization_code"],"response_types":["code"],
       "token_endpoint_auth_method":"none"}'
```

`client_id` back = allowed. `invalid_client_metadata` = not allowed. This
caught the `oauth-callbacks.cloudflareaccess.com` mismatch and confirmed
propagation delays (~1 min) that otherwise look like hard failures.

Each probe creates a real OAuth client — delete them afterwards.

Second oracle: **container logs are ground truth**. For most of the day
Cloudflare never sent a single request to the origin, which ruled out every
theory about the server's own configuration. Note that `.well-known` discovery
is synthesised at Cloudflare's edge and never reaches the origin, so absence of
traffic only proves no *authenticated* MCP call was made.

## 5. Post-launch: connection exhaustion

First real traffic surfaced `FATAL: too many connections for role
"claude_analytics"` plus 30s query timeouts.

Cause: `supergateway` spawns one `postgres-mcp` child per MCP session; each
child opens its own connection pool. The handoff's `CONNECTION LIMIT 5` was
sized for a single long-lived server process, which is not the deployed shape.

Fix: `connection limit 20` on the role, and `--sessionTimeout 300000` on
supergateway so idle sessions are torn down and their connections released.

Worth knowing: the timeout message says 30 seconds and comes from
postgres-mcp's restricted mode, which is separate from the role's
`statement_timeout = 20s`. Both apply; the lower one wins for actual query
execution, but the 30s ceiling covers time spent waiting for a connection.

## 6. Post-launch: connection leak and a validator gotcha

### supergateway spawns one postgres-mcp child per MCP session

Observed after real use: **23 `postgres-mcp` children**, oldest 32 minutes,
one per `initialize`. Each child opens its own psycopg pool and never exits,
so connections accumulate until:

```
FATAL: too many connections for role "claude_analytics"     -- role limit
FATAL: (EMAXCONNSESSION) max clients reached in session mode -- Supavisor
```

Symptom from the client side: a clean run of ~9 calls, then everything fails,
including a bare `SELECT 1`. It looks like a load problem but is a leak — the
cliff is the connection cap, not query cost.

**Do not fix this by raising `CONNECTION LIMIT`.** The database's
`max_connections` is 90 total and the main application needs its share; a leaking role
would simply consume more before failing. Fix the leak.

Mitigation applied: `--sessionTimeout 60000` (was 300000) so idle sessions are
reaped in a minute. Verify with:

```bash
docker exec analytics-mcp sh -c "ps -eo pid,etime,args | grep '[p]ostgres-mcp'"
```

```sql
select count(*), max(now()-backend_start) from pg_stat_activity
where usename = 'claude_analytics';
```

A durable fix would be a bridge that runs a **single** shared stdio child
rather than one per session. `sparfenyuk/mcp-proxy` is the candidate; its
process model is not documented, so it needs testing before swapping.

### The restricted-mode validator rejects `AT TIME ZONE`

```sql
select now() at time zone 'Asia/Baghdad'   -- Error validating query
select to_char(now(), 'YYYY-MM')           -- fine
select date_trunc('month', donated_at)     -- fine
```

`to_char` is **not** blocked — an easy misdiagnosis, since queries containing
both fail and `to_char` is the more unusual-looking construct.

It also isn't needed: the session timezone is already `Asia/Baghdad`, so
`date_trunc('month', donated_at)` is already Baghdad-local. The skill and
connector context were telling the agent to use `AT TIME ZONE` on every time
query — corrected.

## 7. Tool descriptions are where domain knowledge belongs

Comparing this connector against another MCP server on the same host was instructive.
Hala exposes **2** tools to this server's 9, yet performs better, because its
`execute_sql` description carries the domain inline: currency and typical
magnitudes, timezone rules, "exclude status='void'", always LIMIT, and a
CRITICAL TRAPS section listing real column-name typos and enum values that
actually occur.

Tool descriptions are the one context a model always sees when a tool is
available — no skill loading, no project instructions, any client. Our
domain knowledge lived only in a skill, which is why a fresh session
misdiagnosed the validator, mislabeled month buckets, and reported a donation
trend shifted by a month.

`enrich.py` closes that gap. It also hides three tools that cannot work under a
read-only analytics role; notably `analyze_workload_indexes` does not merely
fail, it tells the model to install `hypopg`, a superuser extension, which sends
agents down a dead end.

Deliberately **not** installing hypopg or pg_stat_statements: those tools would
analyse `public` tables this role cannot read, and any index they recommended
would have to be created by someone else. Index tuning belongs to whoever owns
the base tables, not to a read-only analytics connector.

## 8. Three separate redirect-URI layers

Per-user portal access failed with `invalid_request: Redirect URI not allowed
by application configuration` even though the URI was in the application's
allowlist. Three distinct things were being confused:

1. **Application allowlist** — what dynamic client registration will *accept*.
2. **Client registration** — the `redirect_uris` fixed on a specific client
   when it was created. Authorization validates against **this**.
3. **Access policy** — who may authorize at all.

The client had been registered during admin authentication with a single
`dash.cloudflare.com` callback. Adding the portal callback to the application
allowlist could not retroactively change it, so every end-user authorization
was rejected while admin authentication kept working.

Fix: enable the Cloudflare-hosted (shared) callback and re-authenticate, which
registers a **new** client with one URL that both flows use.

A follow-on `Invalid state: missing or invalid nonce` was leftover state from
the earlier failed attempts — each attempt issues a new nonce, and returning
with a stale one fails. Cleared by one clean pass with fresh cookies.

**Diagnostic caveat:** the DCR probe in the playbook tests layer 1 only. It
reported the portal callback as allowed while authorization rejected it. Useful
tool, wrong layer — confirm the real flow before concluding.

## 9. Cloudflare caches the tool snapshot, and nothing clears it in place

Adding `explain_query` took four attempts to become visible, none of which were
server-side problems. Throughout, the origin served 11 tools with the full
parameter set while Cloudflare served 10 with `execute_sql(sql)` only.

Tried and failed: Zero Trust resync; re-authenticating the server; several
disconnect/reconnect cycles of the connector; waiting for the documented
~2-hour automatic sync.

One reconnect actively regressed the client — a session that could call
`timeout_ms` lost the parameter afterwards, because reconnecting re-fetches
Cloudflare's stored snapshot and that copy was older than the one the client
already held.

**Deleting and re-adding the MCP server entry fixed it immediately.** The entry
ID changes as a side effect, which is a useful signal that a client is on the
new registration.

The dashboard also rendered stale descriptions with a "Modified" badge and no
way to accept the change, which reads like a review gate but is not one — there
is no accept action. It is simply displaying the cache.

**Consequence for the design:** this is why `list_views` leads with a contract
version fingerprinting the config file and live schema. When the platform's own
UI cannot be trusted to show what a client will receive, the server has to
announce its own identity, and the client has to be able to compare it against
`GET /introspection` on the origin.
