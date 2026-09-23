"""Run with: python3 -m response_history.cli --provider codex --file FILE count|list|copy."""
import argparse
import os
import sys
from pathlib import Path
from response_history.clipboard import copy_verified
from response_history.model import TranscriptError
from response_history.selection import select
from response_history.session import resolve
from response_history.transcript import load_turns, preview  # noqa: F401  (preview re-exported)


def main(argv=None, transport=copy_verified) -> int:
    parser = argparse.ArgumentParser(description="Local response history")
    parser.add_argument("--provider", choices=("codex", "claude", "auto"), required=True)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--session")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--stdout", action="store_true", help="explicitly print the selected raw payload")
    parser.add_argument("action", choices=("copy", "list", "count"))
    parser.add_argument("selector", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.stdout and args.action != "copy":
            raise ValueError("--stdout requires copy")
        session = args.session
        if args.file is None and session is None:
            session = os.environ.get("CODEX_THREAD_ID") if args.provider == "codex" else os.environ.get("CLAUDE_SESSION_ID") if args.provider == "claude" else None
        if args.file is None and not session:
            raise ValueError("Explicit file or current session ID required")
        path = args.file or resolve(args.provider, session)
        complete = load_turns(args.provider, path, session)
        if not complete and args.action != "count":
            if args.action == "copy":
                print("No responses yet.", file=sys.stderr)
                return 1
            if not args.quiet:
                print("No responses yet.")
            return 0
        if args.action == "count":
            if args.selector:
                raise ValueError("count does not accept a selector")
            if not args.quiet:
                print(len(complete))
            return 0
        if args.action == "list":
            limit = 10
            if args.selector:
                if not args.selector.isdecimal() or int(args.selector) < 1:
                    raise ValueError("list requires a positive count")
                limit = int(args.selector)
            if not args.quiet:
                width = max(3, len(str(len(complete))) + 1)
                print(f"{'No.':<{width}}  Preview")
                print(f"{'-' * width}  -------")
                for n, turn in list(enumerate(complete, 1))[-limit:]:
                    cell = preview(turn.text)
                    if len(cell) > 68:
                        cell = cell[:67] + "…"
                    print(f"{f'#{n}':<{width}}  {cell}")
            return 0
        indexes = select(args.selector, len(complete))
        payload = "\n\n".join(complete[i - 1].text for i in indexes)
        if args.stdout:
            sys.stdout.write(payload)
            return 0
        result = transport(payload)
        if result.status != "verified":
            print(f"Copy {result.status}", file=sys.stderr)
            return 3
        if not args.quiet:
            print("Copied " + ", ".join(f"#{i}" for i in indexes))
        return 0
    except (OSError, UnicodeError, ValueError, TranscriptError) as exc:
        # Do not echo exception details: filenames and transcript bodies may be private.
        print(f"Response history error: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
