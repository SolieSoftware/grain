"""Thin adapter. Everything it does, the library already did."""
from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine

from .api import Grain
from .spec import QuerySpec


def _default(obj: object) -> str:
    if isinstance(obj, Decimal):
        return str(obj)
    return str(obj)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grain")
    parser.add_argument("command", choices=["describe", "explain", "query"])
    parser.add_argument("--spec", help="QuerySpec as JSON")
    parser.add_argument("--object", help="object name, for describe")
    parser.add_argument("--domain", default=None, help="path to a domain pack directory")
    args = parser.parse_args(argv)

    url = os.environ.get("GRAIN_DATABASE_URL")
    if not url:
        print("GRAIN_DATABASE_URL is not set", file=sys.stderr)
        return 2

    if args.domain:
        domain_dir = Path(args.domain)
    else:
        from grain.domains.chinook import CHINOOK_DIR
        domain_dir = CHINOOK_DIR

    g = Grain.load(domain_dir, create_engine(url, future=True))

    if args.command == "describe":
        print(json.dumps(g.describe(args.object), indent=2, default=_default))
        return 0

    if not args.spec:
        print("--spec is required", file=sys.stderr)
        return 2
    spec = QuerySpec.model_validate_json(args.spec)

    if args.command == "explain":
        print(json.dumps(g.explain(spec), indent=2, default=_default))
        return 0

    result = g.query(spec)
    print(json.dumps({
        "columns": result.columns,
        "rows": result.rows,
        "compiled_sql": result.compiled_sql,
        "rewrites": [r.__dict__ for r in result.rewrites],
        "additive": result.additive,
        "non_additive_reason": result.non_additive_reason,
        "limit_reached": result.limit_reached,
    }, indent=2, default=_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
