"""One shared connection pool for the whole process.

This is the fix for the leak in the previous architecture: supergateway forked a
`postgres-mcp` child per MCP session, each opening its own pool, and never
reaped them. Here the pool is created once in the ASGI lifespan and shared by
every request, so connection count is bounded by `limits.pool_max` regardless of
how many sessions or tool calls occur.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import Config

log = logging.getLogger("analytics-mcp.db")


class Database:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._pool: AsyncConnectionPool | None = None

    async def open(self) -> None:
        cfg = self._cfg

        async def configure(conn: AsyncConnection) -> None:
            # Order matters: read_only and autocommit can only be changed while
            # no transaction is open, so they must come before any execute().
            # Autocommit also means pooled connections never sit
            # "idle in transaction" between tool calls.
            await conn.set_autocommit(True)
            await conn.set_read_only(True)
            # Belt and braces: the role already carries these, but a connection
            # from a differently-configured role should still be bounded.
            await conn.execute(
                f"set statement_timeout = {int(cfg.limits.statement_timeout_ms)}"
            )
            await conn.execute(f"set search_path = {cfg.database.schema_name}")

        self._pool = AsyncConnectionPool(
            conninfo=cfg.database.uri,
            min_size=cfg.limits.pool_min,
            max_size=cfg.limits.pool_max,
            configure=configure,
            # application_name makes connections attributable in
            # pg_stat_activity — essential when several MCP servers share a role.
            kwargs={
                "row_factory": dict_row,
                "application_name": f"mcp:{cfg.server.name}",
            },
            open=False,
            name="analytics",
        )
        await self._pool.open(wait=True, timeout=30)
        log.info(
            "pool open (min=%d max=%d schema=%s)",
            cfg.limits.pool_min,
            cfg.limits.pool_max,
            cfg.database.schema_name,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def fetch(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        cap: int | None = None,
        timeout_ms: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Run a query. Returns (rows, truncated).

        `truncated` is True when more rows exist than `cap` — we fetch cap+1 so
        the caller can say so honestly instead of silently returning a partial
        answer that looks complete.
        """
        if self._pool is None:
            raise RuntimeError("pool is not open")
        cap = self._cfg.limits.max_rows if cap is None else cap
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                if timeout_ms is not None:
                    await cur.execute(f"set statement_timeout = {int(timeout_ms)}")
                try:
                    await cur.execute(sql, params or None)
                    if cur.description is None:
                        return [], False
                    rows = await cur.fetchmany(cap + 1)
                finally:
                    if timeout_ms is not None:
                        # Connections are pooled and reused — never leave a
                        # caller's override applied to the next query.
                        await cur.execute(
                            "set statement_timeout = "
                            f"{int(self._cfg.limits.statement_timeout_ms)}"
                        )
        truncated = len(rows) > cap
        return rows[:cap], truncated

    async def connection_count(self) -> int:
        rows, _ = await self.fetch(
            "select count(*) as n from pg_stat_activity "
            "where usename = current_user"
        )
        return int(rows[0]["n"]) if rows else 0


def _jsonable(obj: Any) -> Any:
    """Postgres types the model should see as plain values, not repr strings."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return obj.total_seconds()
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, (bytes, memoryview)):
        return f"<{len(bytes(obj))} bytes>"
    return str(obj)


def render_rows(
    rows: list[dict[str, Any]],
    truncated: bool,
    cap: int,
    *,
    offset: int = 0,
    ordered: bool | None = None,
) -> str:
    """Format rows for the model, and be explicit about what was withheld."""
    if not rows:
        return f"No rows returned. (rows_returned=0, offset={offset})"
    body = "\n".join(
        json.dumps(dict(r), default=_jsonable, ensure_ascii=False) for r in rows
    )
    n = len(rows)
    if truncated:
        # Offset paging is only sound when the query has a total ORDER BY:
        # without one Postgres may return rows in a different order per call,
        # so pages can silently skip or duplicate rows.
        if ordered:
            advice = f"page with offset={offset + n}"
        else:
            advice = (
                "add a total ORDER BY before paging — without one, offset paging "
                "may silently skip or duplicate rows"
            )
        body += (
            f"\n\n[rows_returned={n}, offset={offset}, rows_total>={offset + n + 1} "
            f"— TRUNCATED at the {cap}-row cap. The real total is unknown and may be "
            f"far larger. Either aggregate instead, or {advice}. "
            "Do not present this as a complete result.]"
        )
    else:
        total = offset + n
        body += f"\n\n[rows_returned={n}, offset={offset}, rows_total={total} — complete]"
    return body
