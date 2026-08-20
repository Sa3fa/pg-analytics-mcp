"""Generate the schema block from the live database at boot.

Hand-written schema and enum lists go stale silently — the predecessor prompt
omitted the `boxy` platform/processor and the `daily` frequency, which meant any
query enumerating those values quietly dropped rows. Generating the list from
`information_schema` and `pg_enum` makes that class of bug impossible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .db import Database

log = logging.getLogger("analytics-mcp.introspect")

_COLUMNS_SQL = """
select c.table_name,
       c.column_name,
       c.ordinal_position,
       coalesce(nullif(c.udt_name, ''), c.data_type) as type_name,
       c.data_type
from information_schema.columns c
join information_schema.tables t
  on t.table_schema = c.table_schema and t.table_name = c.table_name
where c.table_schema = %(schema)s
order by c.table_name, c.ordinal_position
"""

# Only enums actually reachable through the exposed schema.
_ENUMS_SQL = """
select distinct t.typname as enum_name,
       (select string_agg(e.enumlabel, ', ' order by e.enumsortorder)
          from pg_enum e where e.enumtypid = t.oid) as values
from information_schema.columns c
join pg_type t on t.typname = c.udt_name
where c.table_schema = %(schema)s and t.typtype = 'e'
order by t.typname
"""

# reltuples is an estimate, which is what we want — cheap and good enough to
# tell the model "this table is large, bound your scans".
_ROWS_SQL = """
select c.relname as name, greatest(c.reltuples, 0)::bigint as est_rows,
       case c.relkind when 'v' then 'view' when 'm' then 'materialized view'
                      when 'r' then 'table' when 'p' then 'partitioned table'
                      else c.relkind::text end as kind
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = %(schema)s and c.relkind in ('r', 'v', 'm', 'p')
"""


def _human(n: int) -> str:
    if n >= 1_000_000:
        return f"~{n / 1_000_000:.1f}M rows"
    if n >= 1_000:
        return f"~{n // 1000}k rows"
    return f"~{n} rows"


@dataclass
class Schema:
    name: str
    objects: dict[str, list[str]] = field(default_factory=dict)
    enums: dict[str, str] = field(default_factory=dict)
    est_rows: dict[str, int] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)   # view / materialized view / table
    fingerprint: str = ""

    def block(self, detail: str = "full") -> str:
        """The schema section injected into tool descriptions.

        `compact` drops column lists — on a large schema the full listing costs
        thousands of tokens in every conversation, and list_views serves it on
        demand instead.
        """
        if detail == "none":
            return "  (call list_views for the schema)"
        if detail == "compact":
            lines = []
            for obj in self.objects:
                size = self.est_rows.get(obj)
                lines.append(f"  {obj}{f'   {_human(size)}' if size else ''}")
            out = "\n".join(lines)
            out += "\n  (columns omitted — call describe_view(name) or list_views)"
            if self.enums:
                out += "\n\nENUM VALUES (generated from the live database):\n"
                out += "\n".join(f"  {k}: {v}" for k, v in self.enums.items())
            return out
        lines: list[str] = []
        for obj, cols in self.objects.items():
            size = self.est_rows.get(obj)
            suffix = f"   {_human(size)}" if size else ""
            lines.append(f"  {obj}({', '.join(cols)}){suffix}")
        out = "\n".join(lines)
        if self.enums:
            out += "\n\nENUM VALUES (generated from the live database):\n"
            out += "\n".join(f"  {k}: {v}" for k, v in self.enums.items())
        return out

    def columns_of(self, obj: str) -> list[str] | None:
        return self.objects.get(obj)


async def introspect(db: Database, schema_name: str) -> Schema:
    schema = Schema(name=schema_name)

    rows, _ = await db.fetch(_COLUMNS_SQL, {"schema": schema_name}, cap=10_000)
    for r in rows:
        schema.objects.setdefault(r["table_name"], []).append(r["column_name"])

    rows, _ = await db.fetch(_ENUMS_SQL, {"schema": schema_name}, cap=200)
    for r in rows:
        if r["values"]:
            schema.enums[r["enum_name"]] = r["values"]

    rows, _ = await db.fetch(_ROWS_SQL, {"schema": schema_name}, cap=500)
    for r in rows:
        schema.est_rows[r["name"]] = int(r["est_rows"])
        schema.kinds[r["name"]] = r["kind"]

    # reltuples is meaningless for views, and an analytics schema is usually all
    # views — so fall back to a real count for anything that reported nothing.
    # Bounded: a handful of objects, and the pool's statement_timeout applies.
    for obj in schema.objects:
        if schema.est_rows.get(obj, 0) > 0:
            continue
        try:
            counted, _ = await db.fetch(f'select count(*) as n from "{obj}"', cap=1)
            if counted:
                schema.est_rows[obj] = int(counted[0]["n"])
        except Exception as exc:  # noqa: BLE001 — size hints are best-effort
            log.warning("row count for %s skipped: %s", obj, exc)

    import hashlib
    sig = "|".join(
        f"{o}:{','.join(c)}" for o, c in sorted(schema.objects.items())
    ) + "||" + "|".join(f"{k}={v}" for k, v in sorted(schema.enums.items()))
    schema.fingerprint = hashlib.sha256(sig.encode()).hexdigest()[:12]

    log.info(
        "introspected schema %s: %d objects, %d enums",
        schema_name,
        len(schema.objects),
        len(schema.enums),
    )
    if not schema.objects:
        log.warning("schema %s exposed no objects — check grants", schema_name)
    return schema
