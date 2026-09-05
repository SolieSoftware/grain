"""`grain chat` -- an interactive REPL over an `AgentSession`."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine

from ..engine.api import Grain
from ..plan import engine_names
from .session import DEFAULT_MODEL, AgentSession

BANNER = """grain chat -- ask questions in plain English. Ctrl-D or 'exit' to quit.
Engine: {engine} | Model: {model}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grain chat")
    parser.add_argument("--domain", default=None, help="path to a domain pack directory")
    parser.add_argument("--engine", default="subquery", choices=sorted(engine_names()),
                        help="which query engine to use (default: subquery)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Claude model id")
    parser.add_argument("--show-spec", action="store_true",
                        help="print each QuerySpec the agent produced")
    parser.add_argument("--no-strict", action="store_true",
                        help="send the full JSON Schema including min/max bounds, "
                             "instead of the subset strict tool use accepts")
    args = parser.parse_args(argv)

    url = os.environ.get("GRAIN_DATABASE_URL")
    if not url:
        print("GRAIN_DATABASE_URL is not set", file=sys.stderr)
        return 2
    # Checked here rather than at the first API call so the failure arrives
    # before the user has typed a question.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2

    if args.domain:
        domain_dir = Path(args.domain)
    else:
        from grain.domains.chinook import CHINOOK_DIR
        domain_dir = CHINOOK_DIR

    db = create_engine(url, future=True)
    grain = Grain.load(domain_dir, db, engine_name=args.engine)
    try:
        session = AgentSession(grain, model=args.model,
                               strict=not args.no_strict)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(BANNER.format(engine=args.engine, model=args.model))
    if args.no_strict:
        print("strict tool use OFF — full schema sent, bounds included\n")
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            return 0

        try:
            turn = session.ask(question)
        except Exception as exc:  # noqa: BLE001 -- rendered, not swallowed
            print(_explain(exc), file=sys.stderr)
            continue

        if args.show_spec:
            for spec in turn.specs:
                print(f"  spec: {spec}")
        for err in turn.errors:
            print(f"  refused: {err.splitlines()[0]}")
        print(f"\n{turn.answer}\n")
        print(
            f"  [{turn.input_tokens} in / {turn.output_tokens} out"
            f"{f' / {turn.cache_read_tokens} cached' if turn.cache_read_tokens else ''}]\n"
        )


def _explain(exc: Exception) -> str:
    """One message per failure kind, rather than one broad catch: a wrong key,
    a rate limit and a dead network need different things from the user."""
    try:
        import anthropic
    except ModuleNotFoundError:  # pragma: no cover
        return f"error: {exc}"

    if isinstance(exc, anthropic.AuthenticationError):
        return "error: ANTHROPIC_API_KEY was rejected. Check the key."
    if isinstance(exc, anthropic.RateLimitError):
        retry = exc.response.headers.get("retry-after", "a moment")
        return f"error: rate limited. Try again in {retry}s."
    if isinstance(exc, anthropic.APIConnectionError):
        return "error: could not reach the API. Check the network."
    if isinstance(exc, anthropic.APIStatusError):
        return f"error: API returned {exc.status_code}: {exc.message}"
    return f"error: {exc}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
