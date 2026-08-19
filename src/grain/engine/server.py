"""MCP adapter — the transport, and nothing else. Any logic that appears here
has to be re-implemented in every other harness, and will drift."""
from __future__ import annotations

import os
from pathlib import Path

# The installed SDK (mcp==2.0.0) renamed the high-level server class from
# `FastMCP` (mcp.server.fastmcp) to `MCPServer` (mcp.server.mcpserver). The
# decorator-based API (`.tool()`, `.run()`) is unchanged, so this adapter is
# otherwise identical to one written against `FastMCP`.
from mcp.server.mcpserver import MCPServer
from sqlalchemy import create_engine

from .api import Grain
from .errors import GrainError
from .spec import QuerySpec

mcp = MCPServer("grain")


def _grain() -> Grain:
    url = os.environ["GRAIN_DATABASE_URL"]
    domain = os.environ.get("GRAIN_DOMAIN")
    if domain:
        domain_dir = Path(domain)
    else:
        from grain.domains.chinook import CHINOOK_DIR
        domain_dir = CHINOOK_DIR
    return Grain.load(domain_dir, create_engine(url, future=True))


@mcp.tool()
def describe_ontology(object: str | None = None) -> dict:
    """List the object types, links and metrics available, with the rules that
    govern how metrics may be aggregated."""
    return _grain().describe(object)


@mcp.tool()
def explain(spec: dict) -> dict:
    """Compile a QuerySpec to SQL and report the grain analysis. Executes nothing."""
    try:
        return _grain().explain(QuerySpec.model_validate(spec))
    except GrainError as exc:
        return {"error": type(exc).__name__, "message": str(exc),
                "alternatives": exc.alternatives}


@mcp.tool()
def query(spec: dict) -> dict:
    """Run a QuerySpec. Returns rows plus the compiled SQL, any rewrites the
    engine applied, and whether the result is additive."""
    try:
        result = _grain().query(QuerySpec.model_validate(spec))
    except GrainError as exc:
        return {"error": type(exc).__name__, "message": str(exc),
                "alternatives": exc.alternatives}
    return {
        "columns": result.columns,
        "rows": [[str(v) for v in row] for row in result.rows],
        "compiled_sql": result.compiled_sql,
        "rewrites": [r.__dict__ for r in result.rewrites],
        "additive": result.additive,
        "non_additive_reason": result.non_additive_reason,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
