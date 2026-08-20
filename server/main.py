"""ASGI entrypoint.

One process, one shared pool, Streamable HTTP served natively at /mcp — no
bridge process, which is why there is no per-session child to leak.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import pathlib
from collections.abc import AsyncIterator

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from . import config as config_mod
from .db import Database
from .introspect import Schema, introspect
from .tools import register

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
)
log = logging.getLogger("analytics-mcp")

CFG = config_mod.load()
DB = Database(CFG)
STATE: dict[str, object] = {"ready": False, "tools": [], "schema": None}

server = MCPServer(
    name=CFG.server.name,
    title=CFG.server.title or CFG.server.name,
    instructions=CFG.server.instructions.strip() or None,
)


def _allowed_hosts() -> list[str]:
    """Config list plus the deployment hostname, so a new VPS needs no edit."""
    hosts = list(CFG.server.allowed_hosts)
    public = os.environ.get("MCP_HOSTNAME", "").strip()
    if public:
        hosts += [public, f"{public}:80", f"{public}:443"]
    container = os.environ.get("MCP_CONTAINER_NAME", "").strip()
    if container:
        hosts += [container, f"{container}:8000"]
    # The host-side publish port, so VPS-shell verification works without
    # spoofing a Host header.
    local_port = os.environ.get("MCP_LOCAL_PORT", "").strip()
    if local_port:
        hosts += [f"localhost:{local_port}", f"127.0.0.1:{local_port}"]
    return sorted(set(h for h in hosts if h))


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    await DB.open()
    schema: Schema = await introspect(DB, CFG.database.schema_name)
    STATE["schema"] = schema
    STATE["tools"] = register(server, CFG, DB, schema, STATE)
    STATE["version"] = _contract_version(schema.fingerprint)
    STATE["freshness"] = await _freshness()

    # Prove the documented privacy boundary before serving a single request.
    # A claim in a description is only a claim until something executes it.
    if CFG.domain.not_available_assertions:
        ok, results = await _run_assertions()
        failed = [r["sql"] for r in results if not r["pass"]]
        if ok:
            log.info("startup assertions: %d/%d pass", len(results), len(results))
        else:
            log.error(
                "PRIVACY BOUNDARY BROKEN — these should have failed but succeeded: %s",
                "; ".join(failed),
            )
            if CFG.limits.assertions_fail_closed:
                raise RuntimeError(
                    "refusing to serve: not_available assertions succeeded: "
                    + "; ".join(failed)
                )
    STATE["ready"] = True
    log.info("ready — MCP on /mcp, allowed hosts: %s", ", ".join(_allowed_hosts()))
    try:
        # Mounting the MCP app under our own Starlette replaces its lifespan,
        # so its session manager task group must be started explicitly here or
        # every request fails with "Task group is not initialized".
        async with server.session_manager.run():
            yield
    finally:
        STATE["ready"] = False
        await DB.close()
        log.info("pool closed")


async def healthz(request: Request) -> PlainTextResponse:
    ok = bool(STATE["ready"])
    return PlainTextResponse("ok" if ok else "starting", status_code=200 if ok else 503)


async def introspection(request: Request) -> JSONResponse:
    """Operational visibility: what the server decided at boot."""
    schema = STATE.get("schema")
    return JSONResponse(
        {
            "ready": STATE["ready"],
            "server": CFG.server.name,
            "schema": CFG.database.schema_name,
            "objects": sorted(getattr(schema, "objects", {}) or {}),
            "enums": sorted(getattr(schema, "enums", {}) or {}),
            "tools": STATE["tools"],
            "version": STATE.get("version"),
            "data_freshness": STATE.get("freshness"),
            "object_kinds": getattr(schema, "kinds", {}),
            "limits": CFG.limits.model_dump(),
        }
    )


async def _run_assertions() -> tuple[bool, list[dict]]:
    """Each statement MUST fail. A success means the privacy boundary is broken."""
    results, ok = [], True
    for sql in CFG.domain.not_available_assertions:
        try:
            await DB.fetch(sql, cap=1)
            results.append({"sql": sql, "result": "SUCCEEDED", "pass": False})
            ok = False
        except Exception as exc:  # noqa: BLE001 — failing is the pass condition
            results.append(
                {"sql": sql, "result": str(exc).splitlines()[0][:160], "pass": True}
            )
    return ok, results


async def _freshness() -> str | None:
    if not CFG.database.freshness_query.strip():
        return None
    try:
        rows, _ = await DB.fetch(CFG.database.freshness_query, cap=1)
        if rows:
            return str(list(rows[0].values())[0])
    except Exception as exc:  # noqa: BLE001 — freshness is advisory
        log.warning("freshness probe failed: %s", exc)
    return None


def _contract_version(schema_fingerprint: str) -> str:
    """Declared version + a fingerprint of config and live schema.

    Moves whenever the contract actually changes, even if nobody bumps the
    declared version — which is what makes it trustworthy to a client.
    """
    try:
        cfg_bytes = pathlib.Path(os.environ.get("MCP_CONFIG", "")).read_bytes()
    except Exception:  # noqa: BLE001
        cfg_bytes = b""
    h = hashlib.sha256(cfg_bytes).hexdigest()[:8]
    return f"{CFG.server.version}+cfg.{h}+schema.{schema_fingerprint}"


async def selftest(request: Request) -> JSONResponse:
    """Prove the NOT-AVAILABLE claims instead of merely asserting them.

    Each configured statement MUST fail. A statement that succeeds means the
    documented privacy boundary is wrong — which is a security defect, not a
    documentation one.
    """
    ok, results = await _run_assertions()
    return JSONResponse(
        {"pass": ok, "checked": len(results), "assertions": results},
        status_code=200 if ok else 500,
    )


mcp_app = server.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,  # no per-session state; the shared pool holds what matters
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=_allowed_hosts(),
        allowed_origins=CFG.server.allowed_origins or _allowed_hosts(),
    ),
)

# Order matters: the operational routes must be matched before the catch-all
# mount, which is what serves both /mcp and /mcp/.
app = Starlette(
    routes=[
        Route("/healthz", healthz, methods=["GET"]),
        Route("/introspection", introspection, methods=["GET"]),
        Route("/selftest", selftest, methods=["GET"]),
        Mount("/", app=mcp_app),
    ],
    lifespan=lifespan,
)
