"""Command line tools.

    python -m chatbot setup-search          create or update the search index and indexer
    python -m chatbot indexer-status        show the last indexer run
    python -m chatbot ask "question"        ask a question from the terminal
    python -m chatbot serve [--port 8000]   run the web app locally

Uses the same environment variables as the web app (see .env.example). In Azure mode the
commands run as your `az login` identity, which the deploy scripts give the needed roles.
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m chatbot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup-search")
    sub.add_parser("indexer-status")
    ask = sub.add_parser("ask")
    ask.add_argument("question")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "serve":
        import uvicorn

        uvicorn.run("chatbot.main:app", host="127.0.0.1", port=args.port)
        return 0

    from .service import build_components

    components = build_components(settings)
    if args.command == "ask":
        result = components.chat.answer(args.question, [])
        print(result.answer)
        for c in result.citations:
            print(f"  [{c.id}] {c.title}")
        return 0
    if components.search is None:
        print("Search commands need CHATBOT_MODE=azure and the AZURE_* settings.", file=sys.stderr)
        return 1
    if args.command == "setup-search":
        from .search import ensure_search_resources

        ensure_search_resources(components.search)
        print(f"Index {settings.search_index} and indexer {settings.search_indexer} are in place.")
    else:
        print(json.dumps(components.search.indexer_status(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
