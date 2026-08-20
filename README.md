# pg-analytics-mcp

A **config-driven, read-only Postgres MCP server for Claude**. Expose a
Postgres schema to Claude over Streamable HTTP, with schema and enum values
introspected from the live database at boot, and everything client-specific in
a single YAML file.

Designed to run **behind Cloudflare Access** on a `cloudflared` → reverse-proxy
stack (a full provisioning playbook is included), but the server itself has no
Cloudflare dependency and runs anywhere.

**Client-agnostic.** Nothing under `server/` knows about any particular client.
To serve a new one: copy the repo, write a config file, set `.env`.

## Why this exists

The predecessor stacked three processes to work around a vendor package:

```
supergateway  →  enrich.py  →  postgres-mcp  →  Postgres
```

`postgres-mcp` speaks only stdio/SSE (Cloudflare requires Streamable HTTP), has
no configuration surface at all, and `supergateway` forked a child per MCP
session that was never reaped — measured **23 children / 15 connections** against
a role limit of 20, which surfaced as "works for ~9 calls then everything fails,
including `SELECT 1`".

This server is **one process** with **one shared pool**. Measured: 1 process
after 30 tool calls.

## Architecture

```
Claude → portal.<zone>          Cloudflare MCP Server Portal (OAuth)
       → mcp-origin.<zone>      Access app + Managed OAuth
       → cloudflared            tunnel
       → traefik                Host-header routing
       → this container         uvicorn, Streamable HTTP at /mcp
       → Postgres               read-only role → analytics.* views
```

The security boundary is the **database role**, not this server.

## Quick start

```bash
cp .env.example .env      # set DATABASE_URI + the deployment vars
$EDITOR config/example.yaml   # domain prose for this client
docker compose up -d --build

curl -s localhost:8000/healthz        # ok
curl -s localhost:8000/introspection  # what the server decided at boot
```

Then follow `docs/PLAYBOOK-NEW-CLIENT.md` for the Cloudflare side.

## Configuration

`.env` — host-specific, the only thing that changes between VPSes:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URI` | Read-only role. On the Supavisor pooler the username **must** carry `.PROJECT_REF`. |
| `MCP_CONTAINER_NAME` | Container, image tag, and traefik router name |
| `MCP_HOSTNAME` | Public hostname; auto-added to the transport-security allowlist |
| `TRAEFIK_NETWORK` | External docker network traefik watches |
| `MCP_CONFIG` | Path to the client YAML inside the image |
| `MCP_LOCAL_PORT` | Host-side publish port (default 8000) |

`config/<client>.yaml` — the domain. **Do not list columns or enum values
here**: they are introspected from the live database at boot, so they cannot go
stale. Write only what introspection cannot know — business meaning and traps.

## Tools

Built-in:

- **`execute_sql(sql)`** — raw read-only SQL. Its description is assembled at
  boot from your authored prose *plus* the generated schema and enum lists.
- **`list_views()`** — every readable object with columns, row counts, enums.
- **`describe_view(name)`** — columns of one object.

Config-defined: every entry under `tools.queries` becomes a real MCP tool with
typed parameters. Parameters bind via psycopg named placeholders — never string
interpolation — and `min`/`max` are enforced before binding.

```yaml
tools:
  queries:
    monthly_trend:
      description: |
        Donations per month. The most recent month is PARTIAL.
      params:
        months: {type: integer, default: 6, min: 1, max: 36}
      sql: |
        select ... where donated_at >= date_trunc('month', now())
                                     - make_interval(months => %(months)s - 1)
```

This is the bit that closes the gap with n8n: adding a tool is prose + SQL, not
Python.

## Why descriptions live here

Tool descriptions are the one context a model sees **whenever the tool is
available** — every client, every conversation, no skill loading and no project
instructions. Domain knowledge kept in an external document is knowledge the
model often does not have.

Half of each description is authored (judgement), half generated (facts). The
generated half is why the `boxy` platform/processor and `daily` frequency can no
longer go missing the way they did in the hand-written prompt that preceded this.

## Operations

```bash
curl -s localhost:8000/introspection | python3 -m json.tool   # objects, enums, tools, limits
docker top <container>                                        # must stay at 1 process
docker compose up -d --build                                  # after a config edit
```

A config or schema change needs a restart — introspection is cached for the
process lifetime, deliberately, so behaviour cannot drift mid-run.

## The five boundary tests

Re-run after any change to views, grants, or config. **All five must fail:**

```sql
update donations set amount = 0 where false;   -- permission denied for view
select count(*) from public.donations;         -- permission denied for table
select count(*) from public.website_orders;    -- permission denied for table
create table analytics.t (id int);             -- read-only transaction
select phone_number from customers limit 1;    -- column does not exist
```

`limits.select_only` exists but defaults **off**: the role is the boundary, and
a SQL validator on top blocks valid read-only constructs for no gain — that is
why `postgres-mcp`'s restricted mode was abandoned.

## Gotchas paid for in blood

- **Compose label keys are not variable-substituted.** Labels must be list-form
  (`- "traefik...=value"`), or you get a router literally named
  `${MCP_CONTAINER_NAME}` and traefik 404s.
- **DNS-rebinding protection is on by default** in the MCP SDK. The forwarded
  `Host` behind a proxy must be allowed; `MCP_HOSTNAME` and `MCP_LOCAL_PORT` are
  added automatically.
- **Mounting the MCP app under your own Starlette replaces its lifespan.** The
  session manager must be started explicitly (`server.session_manager.run()`) or
  every request 500s with "Task group is not initialized".
- **`set_read_only` / `set_autocommit` must precede any `execute()`** on a
  connection, or the pool fails with "connection in transaction status INTRANS".
- **`pg_class.reltuples` is meaningless for views**, so row estimates fall back
  to a bounded `count(*)` at boot.
- **Supavisor rewrites `application_name`** to "Supavisor", so per-client
  connection attribution through the pooler is not possible.
- **MCP SDK 2.0 renamed `FastMCP` to `MCPServer`** and moved it out of
  `mcp.server.fastmcp`. `requirements.txt` is a full lock for that reason.

## License

MIT — see [LICENSE](LICENSE).
