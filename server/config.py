"""Config loading and validation.

Everything client-specific lives in one YAML file. Nothing in this package
knows about any particular client.
"""

from __future__ import annotations

import os
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

PARAM_TYPES: dict[str, type] = {
    "integer": int,
    "number": float,
    "string": str,
    "boolean": bool,
}


class ServerCfg(BaseModel):
    name: str
    title: str | None = None
    instructions: str = ""
    # The SDK enables DNS-rebinding protection by default, which rejects any
    # Host header it does not recognise. Behind a reverse proxy the forwarded
    # Host is the public hostname, so it must be allowed explicitly.
    # MCP_HOSTNAME from the environment is appended automatically, so a new
    # deployment usually needs no change here.
    allowed_hosts: list[str] = Field(
        default_factory=lambda: [
            "localhost",
            "127.0.0.1",
            "localhost:8000",
            "127.0.0.1:8000",
        ]
    )
    allowed_origins: list[str] = Field(default_factory=list)


class DatabaseCfg(BaseModel):
    schema_name: str = Field(alias="schema")
    uri_env: str = "DATABASE_URI"

    model_config = {"populate_by_name": True}

    @property
    def uri(self) -> str:
        uri = os.environ.get(self.uri_env, "").strip()
        if not uri:
            raise RuntimeError(f"{self.uri_env} is not set")
        return uri


class LimitsCfg(BaseModel):
    max_rows: int = 500
    statement_timeout_ms: int = 20_000
    # Callers may raise the timeout per query up to this ceiling.
    statement_timeout_max_ms: int = 120_000
    pool_min: int = 1
    pool_max: int = 4
    # The database role is the security boundary (read-only transactions, SELECT
    # on views only). This guard is a convenience, not a control, and is off by
    # default — a SQL validator is exactly what we removed from postgres-mcp.
    select_only: bool = False


class DomainCfg(BaseModel):
    summary: str = ""
    traps: str = ""
    not_available: str = ""
    # Each entry is SQL that MUST fail. Proves the not_available prose is true
    # rather than merely asserted. Run via GET /selftest.
    not_available_assertions: list[str] = Field(default_factory=list)


class ParamCfg(BaseModel):
    type: Literal["integer", "number", "string", "boolean"] = "string"
    default: Any = None
    min: float | None = None
    max: float | None = None
    description: str | None = None

    @property
    def py_type(self) -> type:
        return PARAM_TYPES[self.type]

    def coerce(self, value: Any) -> Any:
        """Validate one argument against this parameter's declared bounds."""
        if value is None:
            value = self.default
        if value is None:
            raise ValueError("missing required parameter")
        value = self.py_type(value)
        if self.min is not None and value < self.min:
            raise ValueError(f"must be >= {self.min}")
        if self.max is not None and value > self.max:
            raise ValueError(f"must be <= {self.max}")
        return value


class QueryToolCfg(BaseModel):
    description: str
    sql: str
    params: dict[str, ParamCfg] = Field(default_factory=dict)

    @field_validator("sql")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("sql must not be empty")
        return v


class ExecuteSqlCfg(BaseModel):
    enabled: bool = True
    extra: str = ""
    # How much generated schema to embed in the tool description.
    #   full    — objects, columns, row counts, enums (best for small schemas)
    #   compact — object names + row counts + enums only
    #   none    — omit; the model must call list_views first
    # On a 60-table schema `full` costs thousands of tokens in every
    # conversation, so large deployments should use compact.
    schema_detail: Literal["full", "compact", "none"] = "full"


class ExplainCfg(BaseModel):
    enabled: bool = True


class ToolsCfg(BaseModel):
    execute_sql: ExecuteSqlCfg = Field(default_factory=ExecuteSqlCfg)
    explain_query: ExplainCfg = Field(default_factory=ExplainCfg)
    queries: dict[str, QueryToolCfg] = Field(default_factory=dict)


class Config(BaseModel):
    server: ServerCfg
    database: DatabaseCfg
    limits: LimitsCfg = Field(default_factory=LimitsCfg)
    domain: DomainCfg = Field(default_factory=DomainCfg)
    tools: ToolsCfg = Field(default_factory=ToolsCfg)


def load(path: str | None = None) -> Config:
    path = path or os.environ.get("MCP_CONFIG", "config/example.yaml")
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Config.model_validate(raw)
