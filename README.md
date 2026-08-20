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

- **`execute_sql(sql, limit=, offset=, timeout_ms=)`** — raw read-only SQL. Its
  description is assembled at boot from your authored prose *plus* the generated
  schema and enum lists. Every result reports `rows_returned` and `rows_total`;
  when truncated, `rows_total` is stated as a **lower bound** rather than
  silently returning a partial answer that looks complete. `timeout_ms` raises
  the statement timeout for one query, clamped to
  `limits.statement_timeout_max_ms` and reset afterwards so it cannot leak onto
  a pooled connection.
- **`explain_query(sql)`** — the plan, without executing anything. `EXPLAIN`,
  never `ANALYZE`.
- **`list_views()`** — every readable object with columns, row counts, enums.
- **`describe_view(name)`** — columns of one object.

**Errors are made actionable.** On `undefined_column` / `undefined_table` the
server appends the real column list for whichever known objects the query
mentioned. Postgres supplies a `HINT` only when a close match exists; for a typo
far from any real name it says nothing, and that silence is what leaves a caller
guessing.

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

## Large schemas

`tools.execute_sql.schema_detail` controls how much generated schema rides in
the tool description, which is loaded into **every** conversation:

| Value | Contents | Use when |
| --- | --- | --- |
| `full` (default) | objects, columns, row counts, enums | small schemas — best accuracy |
| `compact` | object names, row counts, enums | large schemas; columns via `describe_view` |
| `none` | nothing | the model must call `list_views` first |

Six views cost ~1,000 tokens, which is a cheap insurance premium against
hallucinated column names. Sixty tables would cost ten times that in every
conversation — switch to `compact` there.

## Operations

```bash
curl -s localhost:8000/selftest      | python3 -m json.tool   # privacy boundary assertions
curl -s localhost:8000/introspection | python3 -m json.tool   # objects, enums, tools, limits
docker top <container>                                        # must stay at 1 process
docker compose up -d --build                                  # after a config edit
```

A config or schema change needs a restart — introspection is cached for the
process lifetime, deliberately, so behaviour cannot drift mid-run.

## Proving the privacy boundary

`domain.not_available_assertions` is a list of statements that **must fail**.
`GET /selftest` runs them and returns HTTP 500 if any succeeds:

```json
{"pass": true, "checked": 13,
 "assertions": [{"sql": "select display_name from customers limit 1",
                 "result": "column \"display_name\" does not exist", "pass": true}]}
```

Documentation claiming a column is unreachable is only a claim. This turns it
into a test — run it in CI, or after any change to views or grants. A statement
that *succeeds* is a security defect, not a documentation one.

## The five boundary tests

Re-run after any change to views, grants, or config. **All five must fail:**

```sql
update customers set city = 'x' where false;   -- permission denied for view
update donations set amount = 0 where false;   -- cannot update view (joined, so
                                               --   not auto-updatable — a second,
                                               --   independent guard)
select count(*) from public.donations;         -- permission denied for table
select count(*) from public.website_orders;    -- permission denied for table
create table analytics.t (id int);             -- read-only transaction
select phone_number from customers limit 1;    -- column does not exist
```

Two guards refuse writes and which one fires depends on the view: simple views
hit the role's missing grant, views carrying the free-text allowlist joins are
rejected earlier as non-updatable. Assert that a write is **refused**, not that
it produced a particular message.

## Regression suite

```bash
python3 tests/regression.py                    # against localhost:8000
python3 tests/regression.py --url http://host:8010 --slow
```

32 behavioural assertions covering the contract surface, SQL capability,
truncation honesty, paging soundness, the write and PII boundary, derived-tool
stability and the timeout override. Exit code 1 on any regression.

Every one of these encodes something that was established by hand and that a
later change could silently undo — the truncation format, the ORDER BY caveat,
the spike threshold's independence from window size. Run it after any change to
the server, the views, or the grants.

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
- **Cloudflare caches the tool snapshot per MCP server entry.** Resync,
  re-authentication and reconnecting all fail to clear it, and reconnecting can
  hand a client an *older* snapshot than it already had. Deleting and re-adding
  the server entry is the only fix. This is why `list_views` announces a
  contract version — compare it with `GET /introspection` on the origin.
- **MCP SDK 2.0 renamed `FastMCP` to `MCPServer`** and moved it out of
  `mcp.server.fastmcp`. `requirements.txt` is a full lock for that reason.

## License

MIT — see [LICENSE](LICENSE).
