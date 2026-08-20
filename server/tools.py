"""Tool registration: built-ins plus the config-defined query tools.

Tool descriptions are the one piece of context a model sees whenever a tool is
available — no skill loading, no project instructions, any client. So the
domain knowledge belongs here rather than in an external document. Half of each
description is authored in YAML (prose that needs judgement) and half is
generated from the live schema (facts that go stale).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

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
        f"unqualified names resolve there. Objects available:\n\n{schema.block()}"
    )

    parts.append(
        "RULES\n"
        "1. SELECT only. Writes and DDL fail at the database-role level.\n"
        "2. NEVER invent table, column or enum values — the lists above are generated\n"
        "   from the live database, so trust them over any other source.\n"
        "3. Report only values this tool returns. Empty result = say so; never fabricate.\n"
        f"4. statement_timeout is {cfg.limits.statement_timeout_ms // 1000}s — bound large scans by date.\n"
        f"5. At most {cfg.limits.max_rows} rows are returned; output says so when truncated.\n"
        "   Prefer aggregates over dumping rows."
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


def _guard(sql: str) -> None:
    first = sql.strip().lstrip("(").split(None, 1)
    if first and first[0].lower() in _WRITE_PREFIXES:
        raise ValueError(
            f"This connector is read-only; refusing a '{first[0].lower()}' statement."
        )


def _make_query_fn(
    name: str, qcfg: QueryToolCfg, db: Database, cfg: Config
) -> Callable[..., Any]:
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
        return render_rows(rows, truncated, cfg.limits.max_rows)

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


def register(server: MCPServer, cfg: Config, db: Database, schema: Schema) -> list[str]:
    registered: list[str] = []

    if cfg.tools.execute_sql.enabled:
        desc = execute_sql_description(cfg, schema)

        async def execute_sql(sql: str) -> str:
            if cfg.limits.select_only:
                _guard(sql)
            rows, truncated = await db.fetch(sql)
            return render_rows(rows, truncated, cfg.limits.max_rows)

        server.add_tool(execute_sql, name="execute_sql", description=desc)
        registered.append("execute_sql")

    async def list_views() -> str:
        """List the objects this connector can read, with their columns."""
        return schema.block()

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

    async def describe_view(name: str) -> str:
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

    for name, qcfg in cfg.tools.queries.items():
        fn = _make_query_fn(name, qcfg, db, cfg)
        server.add_tool(fn, name=name, description=qcfg.description.strip())
        registered.append(name)

    log.info("registered %d tools: %s", len(registered), ", ".join(registered))
    return registered
