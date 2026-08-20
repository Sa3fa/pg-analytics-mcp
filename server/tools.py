"""Tool registration: built-ins plus the config-defined query tools.

Tool descriptions are the one piece of context a model sees whenever a tool is
available — no skill loading, no project instructions, any client. So the
domain knowledge belongs here rather than in an external document. Half of each
description is authored in YAML (prose that needs judgement) and half is
generated from the live schema (facts that go stale).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

import psycopg
from mcp.server.mcpserver import MCPServer

from .config import Config, QueryToolCfg
from .db import Database, render_rows
from .introspect import Schema

log = logging.getLogger("analytics-mcp.tools")

_PY_NAMES = {"integer": "int", "number": "float", "string": "str", "boolean": "bool"}

_WRITE_PREFIXES = (
    "insert", "update", "delete", "truncate", "drop", "create",
    "alter", "grant", "revoke", "copy", "vacuum", "call",
)


def execute_sql_description(cfg: Config, schema: Schema) -> str:
    """Assemble the execute_sql description: authored prose + generated facts."""
    parts: list[str] = []
    if cfg.domain.summary.strip():
        parts.append(cfg.domain.summary.strip())

    parts.append(
        f"SCOPE — only schema `{schema.name}` is visible, and it is the search_path, so\n"
        "unqualified names resolve there. Objects available:\n\n"
        f"{schema.block(cfg.tools.execute_sql.schema_detail)}"
    )

    parts.append(
        "RULES\n"
        "1. SELECT only. Writes and DDL fail at the database-role level.\n"
        "2. NEVER invent table, column or enum values — the lists above are generated\n"
        "   from the live database, so trust them over any other source.\n"
        "3. Report only values this tool returns. Empty result = say so; never fabricate.\n"
        f"4. statement_timeout is {cfg.limits.statement_timeout_ms // 1000}s — bound large scans\n"
        f"   by date. Pass timeout_ms to raise it for one query (max "
        f"{cfg.limits.statement_timeout_max_ms // 1000}s).\n"
        f"5. At most {cfg.limits.max_rows} rows are returned. Every result reports\n"
        "   rows_returned and rows_total; when truncated, rows_total is a LOWER BOUND\n"
        "   and the real count is unknown. Either aggregate, or page with limit/offset.\n"
        "6. explain_query(sql) shows the plan without running the query — use it before\n"
        "   a scan you expect to be expensive."
    )

    if cfg.domain.traps.strip():
        parts.append(
            "TRAPS (verified against the live database — these produce wrong answers "
            f"if missed)\n{cfg.domain.traps.strip()}"
        )
    if cfg.domain.not_available.strip():
        parts.append(
            "NOT AVAILABLE — say so plainly rather than working around it:\n"
            f"{cfg.domain.not_available.strip()}"
        )
    if cfg.tools.execute_sql.extra.strip():
        parts.append(cfg.tools.execute_sql.extra.strip())

    return "\n\n".join(parts)


_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)


def _has_order_by(sql: str) -> bool:
    """Offset paging is unsound without one — see render_rows."""
    return bool(_ORDER_BY.search(sql))


def _paginate(sql: str, limit: int | None, offset: int) -> str:
    """Wrap a query for paging without parsing it."""
    if limit is None and not offset:
        return sql
    inner = sql.strip().rstrip(";")
    lim = f"limit {int(limit)}" if limit is not None else ""
    off = f"offset {int(offset)}" if offset else ""
    return f"select * from (\n{inner}\n) as _page {lim} {off}".strip()


def _schema_hint(exc: Exception, schema: Schema) -> str:
    """Turn a bare 'column does not exist' into something actionable.

    Postgres supplies a HINT only when a close match exists in scope; for a
    typo far from any real name it says nothing at all, which leaves the caller
    guessing. Append the columns of whichever known objects the query mentions.
    """
    state = getattr(exc, "sqlstate", None)
    if state not in ("42703", "42P01"):  # undefined_column, undefined_table
        return ""
    text = str(exc).lower()
    mentioned = [o for o in schema.objects if o.lower() in text]
    if not mentioned:
        return (
            "\n\nObjects available in this schema: "
            + ", ".join(sorted(schema.objects))
            + "\nCall describe_view(name) for its columns."
        )
    out = ["\n\nColumns actually available:"]
    for obj in mentioned[:4]:
        out.append(f"  {obj}({', '.join(schema.objects[obj])})")
    return "\n".join(out)


def _guard(sql: str) -> None:
    first = sql.strip().lstrip("(").split(None, 1)
    if first and first[0].lower() in _WRITE_PREFIXES:
        raise ValueError(
            f"This connector is read-only; refusing a '{first[0].lower()}' statement."
        )


def _make_query_fn(
    name: str, qcfg: QueryToolCfg, db: Database, cfg: Config
) -> Callable[..., Any]:  # noqa: C901
    """Build an async function whose real signature matches the configured params.

    The SDK derives each tool's JSON schema from the function signature, so the
    signature has to be genuine rather than **kwargs.
    """

    async def run(values: dict[str, Any]) -> str:
        bound: dict[str, Any] = {}
        for pname, pcfg in qcfg.params.items():
            try:
                bound[pname] = pcfg.coerce(values.get(pname))
            except ValueError as exc:
                raise ValueError(f"parameter '{pname}': {exc}") from exc
        rows, truncated = await db.fetch(qcfg.sql, bound or None)
        body = render_rows(rows, truncated, cfg.limits.max_rows)
        # Echo the inputs. A result pasted into a report tomorrow should be
        # reconstructible without the conversation that produced it.
        stamp = json.dumps(bound, default=str, ensure_ascii=False) if bound else "{}"
        return f"[tool={name} params={stamp}]\n{body}"

    args: list[str] = []
    for pname, pcfg in qcfg.params.items():
        ann = _PY_NAMES[pcfg.type]
        if pcfg.default is None:
            args.append(f"{pname}: {ann}")
        else:
            args.append(f"{pname}: {ann} = {pcfg.default!r}")

    src = (
        f"async def _tool({', '.join(args)}):\n"
        f"    return await _run({{{', '.join(f'{p!r}: {p}' for p in qcfg.params)}}})\n"
    )
    ns: dict[str, Any] = {"_run": run}
    exec(compile(src, f"<query:{name}>", "exec"), ns)  # noqa: S102 — operator-authored
    fn = ns["_tool"]
    fn.__name__ = name
    return fn


def register(
    server: MCPServer,
    cfg: Config,
    db: Database,
    schema: Schema,
    state: dict[str, Any] | None = None,
) -> list[str]:
    registered: list[str] = []
    state = state if state is not None else {}

    if cfg.tools.execute_sql.enabled:
        desc = execute_sql_description(cfg, schema)

        async def execute_sql(
            sql: str,
            limit: int | None = None,
            offset: int = 0,
            timeout_ms: int | None = None,
        ) -> str:
            if cfg.limits.select_only:
                _guard(sql)
            if timeout_ms is not None:
                timeout_ms = max(
                    1_000, min(int(timeout_ms), cfg.limits.statement_timeout_max_ms)
                )
            offset = max(0, int(offset))
            cap = cfg.limits.max_rows if limit is None else min(int(limit), cfg.limits.max_rows)
            try:
                # Fetch cap+1 so truncation is detectable. Without the +1 an
                # explicit limit would always look "complete", because the
                # wrapper would hide the very row that proves there are more.
                rows, truncated = await db.fetch(
                    _paginate(sql, None if limit is None else cap + 1, offset),
                    cap=cap,
                    timeout_ms=timeout_ms,
                )
            except psycopg.Error as exc:
                raise ValueError(f"{exc}{_schema_hint(exc, schema)}") from exc
            return render_rows(
                rows, truncated, cap, offset=offset, ordered=_has_order_by(sql)
            )

        server.add_tool(execute_sql, name="execute_sql", description=desc)
        registered.append("execute_sql")

    if cfg.tools.explain_query.enabled:

        async def explain_query(sql: str) -> str:
            """Show the plan without executing the query."""
            try:
                rows, _ = await db.fetch(f"explain (format text) {sql.strip().rstrip(';')}",
                                         cap=200)
            except psycopg.Error as exc:
                raise ValueError(f"{exc}{_schema_hint(exc, schema)}") from exc
            return "\n".join(str(list(r.values())[0]) for r in rows) or "(no plan)"

        server.add_tool(
            explain_query,
            name="explain_query",
            description=(
                "Return the Postgres query plan for a SELECT without running it "
                "(EXPLAIN, never ANALYZE — nothing is executed and no rows are read). "
                "Use it before a scan you expect to be expensive, or to check whether "
                "a filter uses an index rather than a sequential scan."
            ),
        )
        registered.append("explain_query")

    async def list_views() -> str:
        """List the objects this connector can read, with their columns."""
        kinds = ", ".join(
            f"{o}={schema.kinds.get(o, '?')}" for o in sorted(schema.objects)
        )
        header = [
            f"CONTRACT VERSION: {state.get('version', 'unknown')}",
            "  (changes whenever the config or live schema changes — if this "
            "differs from what you saw earlier, re-read the tool descriptions)",
        ]
        if cfg.server.changelog.strip():
            header.append(f"CHANGELOG: {cfg.server.changelog.strip()}")
        header.append(f"SERVER STARTED: {state.get('started', 'unknown')} "
                      "(when the schema below was captured)")
        # Measured now, not at boot. A cached value ages into a lie and can
        # never signal an ingestion stall, which is the only reason the field
        # exists.
        if cfg.database.freshness_query.strip():
            try:
                rows, _ = await db.fetch(cfg.database.freshness_query, cap=1)
                val = str(list(rows[0].values())[0]) if rows else "unknown"
                header.append(f"DATA FRESHNESS (measured now): {val}")
            except Exception as exc:  # noqa: BLE001 — advisory
                header.append(f"DATA FRESHNESS: probe failed ({exc})")
        else:
            header.append("DATA FRESHNESS: not configured — treat recency as unknown")
        header.append(f"OBJECT KINDS: {kinds}")
        return "\n".join(header) + "\n\n" + schema.block()

    server.add_tool(
        list_views,
        name="list_views",
        description=(
            f"List every object readable in schema `{schema.name}` with its columns, "
            "row estimates, and the enum values used by those columns. Generated from "
            "the live database at server start."
        ),
    )
    registered.append("list_views")

    async def describe_view(name: str) -> str:  # noqa: D401
        """Columns of a single object."""
        cols = schema.columns_of(name)
        if cols is None:
            known = ", ".join(sorted(schema.objects)) or "(none)"
            return f"No object named '{name}' in schema {schema.name}. Available: {known}"
        return f"{name}({', '.join(cols)})"

    server.add_tool(
        describe_view,
        name="describe_view",
        description=(
            "Return the exact columns of one object. Call this before writing SQL if "
            "you are not certain of a column name — never guess."
        ),
    )
    registered.append("describe_view")

    if cfg.tools.data_health.enabled and cfg.tools.data_health.checks:
        checks = cfg.tools.data_health.checks

        async def data_health() -> str:
            """Run every configured anomaly check; report only what fires."""
            out, fired = [], 0
            for cname, chk in checks.items():
                try:
                    rows, _ = await db.fetch(chk.sql, cap=20)
                except Exception as exc:  # noqa: BLE001 — one bad check must not hide the rest
                    out.append(f"[{cname}] CHECK FAILED TO RUN: {exc}")
                    continue
                if not rows:
                    continue
                fired += 1
                out.append(
                    f"[{chk.severity.upper()}] {cname} — {chk.description.strip()}\n"
                    + "\n".join(
                        "  " + json.dumps(dict(r), default=str, ensure_ascii=False)
                        for r in rows
                    )
                )
            if not out:
                return f"All {len(checks)} data-health checks pass — no anomalies detected."
            return (
                f"{fired} of {len(checks)} checks fired. These describe the DATA, not "
                "the server — report them to the user rather than working around them.\n\n"
                + "\n\n".join(out)
            )

        server.add_tool(
            data_health,
            name="data_health",
            description=(
                "Run the configured data-quality and business-anomaly checks and report "
                "anything wrong with the underlying DATA — stalled pipelines, overdue "
                "recurring charges, values that stopped arriving. Returns only checks "
                "that fired.\n\nCall this when a figure looks surprising, before "
                "concluding a trend is real, and whenever asked how healthy the data is. "
                "A number can be correct and still be the symptom of a broken process."
            ),
        )
        registered.append("data_health")

    for name, qcfg in cfg.tools.queries.items():
        fn = _make_query_fn(name, qcfg, db, cfg)
        server.add_tool(fn, name=name, description=qcfg.description.strip())
        registered.append(name)

    log.info("registered %d tools: %s", len(registered), ", ".join(registered))
    return registered
