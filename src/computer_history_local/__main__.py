"""Run the collector, manage its LaunchAgent, or summarize into local memory.

    python -m computer_history_local run [--store PATH] [--interval SECONDS]
    python -m computer_history_local install [--store PATH] [--interval SECONDS]
    python -m computer_history_local uninstall
    python -m computer_history_local status [--store PATH]
    python -m computer_history_local summarize [--store PATH] [--memory-dir PATH]
        [--provider {fake,claude-cli}] [--send] [--reprocess YYYY-MM-DD]

`run` is what the LaunchAgent plist itself invokes
(`launch_agent.build_plist`'s `ProgramArguments`) -- an explicit subcommand,
not a bare no-subcommand default, so `--store`/`--interval` are each defined
in exactly one place. An earlier version defined them on both the top-level
parser and the subparsers to allow a bare invocation; argparse's subparser
action unconditionally overwrites the parent namespace with its own
defaults, so a flag given *before* the subcommand token and not repeated
after it was silently dropped -- exactly the silent-misconfiguration
failure mode this project's design otherwise guards against everywhere
else. Requiring an explicit subcommand removes the ambiguous position
instead of trying to detect or warn about it.

`--store`/`--interval` exist so paths resolved at install time can be
passed explicitly, rather than this process computing them fresh -- the
same reasoning `adhd_lifelog` learned the hard way about launchd's minimal
environment.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from .collector import DEFAULT_INTERVAL_SECONDS, run_forever
from .launch_agent import install, status, uninstall
from .memory_pipeline import DEFAULT_MEMORY_DIR, preview, summarize_once
from .pipeline_store import PipelineStore
from .providers.claude_cli import ClaudeCliProvider, DaySummaryState
from .providers.fake import FakeProvider
from .retrieval import ask_preview, gather_all_daily_memories, lookup
from .store import DEFAULT_STORE, Store


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="computer_history_local")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the collector loop")
    run_parser.add_argument("--store", type=Path, default=None)
    run_parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)

    install_parser = subparsers.add_parser("install", help="Write and load the LaunchAgent")
    install_parser.add_argument("--store", type=Path, default=None)
    install_parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)

    subparsers.add_parser("uninstall", help="Unload and remove the LaunchAgent")

    status_parser = subparsers.add_parser("status", help="Print collector health")
    status_parser.add_argument("--store", type=Path, default=None)

    summarize_parser = subparsers.add_parser(
        "summarize", help="Turn captured State samples into Daily memory files"
    )
    summarize_parser.add_argument("--store", type=Path, default=None)
    summarize_parser.add_argument("--memory-dir", type=Path, default=None)
    summarize_parser.add_argument("--provider", choices=("fake", "claude-cli"), default="fake")
    # Off by default -- Transfer's own consent gate (CONTEXT.md), the same
    # shape as `adhd_lifelog`'s `now --send`. Without it nothing is called
    # and nothing is written.
    summarize_parser.add_argument("--send", action="store_true")
    # Force-reprocess exactly this day, even if already covered -- never
    # touches the Watermark either way (ticket #16).
    summarize_parser.add_argument("--reprocess", type=date.fromisoformat, default=None)

    # Ticket #28: free, deterministic. One date is a single-day lookup, two
    # is an inclusive range -- the same date-parsing shape `--reprocess`
    # already established, not new flag syntax for the same kind of input.
    retrieve_parser = subparsers.add_parser(
        "retrieve", help="Print a Daily memory file, or a range of them"
    )
    retrieve_parser.add_argument("--store", type=Path, default=None)
    retrieve_parser.add_argument("start", type=date.fromisoformat)
    retrieve_parser.add_argument("end", type=date.fromisoformat, nargs="?", default=None)

    # Ticket #29: costs a real call once --send is passed. Off by default --
    # the same consent shape as `summarize --send`, but its own independent
    # gate (CONTEXT.md's Capture-vs-Transfer rule: never the same decision).
    ask_parser = subparsers.add_parser(
        "ask", help="Ask a free-text question across every Daily memory file"
    )
    ask_parser.add_argument("--store", type=Path, default=None)
    ask_parser.add_argument("question")
    ask_parser.add_argument("--provider", choices=("fake", "claude-cli"), default="fake")
    ask_parser.add_argument("--send", action="store_true")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if args.command == "install":
        ok, detail = install(interval=args.interval, store=args.store)
        print(f"installed: {detail}" if ok else f"install failed: {detail}")
        if not ok:
            sys.exit(1)
        return

    if args.command == "uninstall":
        _ok, detail = uninstall()
        print(detail)
        return

    if args.command == "status":
        store_path = args.store if args.store is not None else DEFAULT_STORE
        print(status(store_path=store_path))
        return

    if args.command == "summarize":
        store_path = args.store if args.store is not None else DEFAULT_STORE
        memory_dir = args.memory_dir if args.memory_dir is not None else DEFAULT_MEMORY_DIR
        if not args.send:
            # Free, no-network preview -- no provider call, no write. Uses
            # the exact same day-batching/coverage-check/splitting logic
            # `summarize_once` does, not a separate implementation that
            # could drift from what a real run would actually do.
            try:
                with Store(store_path) as store, PipelineStore(store_path) as pipeline_store:
                    previews = preview(store, pipeline_store, reprocess=args.reprocess)
            except sqlite3.OperationalError as exc:
                print(f"failed to open the store: {exc}")
                sys.exit(1)
            if not previews:
                print("nothing to summarize")
                return
            for day_preview in previews:
                label = day_preview.day.isoformat()
                if day_preview.error is not None:
                    print(f"{label}: {day_preview.error}")
                elif day_preview.already_covered:
                    print(f"{label}: already covered")
                elif not day_preview.chunks:
                    print(f"{label}: no data captured")
                else:
                    print(f"{label}: {len(day_preview.chunks)} chunk(s)")
                    for index, chunk in enumerate(day_preview.chunks, start=1):
                        start_local = chunk.start.astimezone()
                        end_local = chunk.end.astimezone()
                        print(f"--- chunk {index}: {start_local:%H:%M}–{end_local:%H:%M} ---")
                        print(chunk.text)
            print()
            print("pass --send to call the provider and write memories")
            return
        if args.reprocess is not None and args.provider == "fake":
            # --reprocess overwrites a real day's file+index (OR REPLACE)
            # and never touches the Watermark either way -- if that
            # overwrite lands placeholder text, no later plain run ever
            # revisits the day to notice, since it's still "already
            # covered." Silent, permanent data loss otherwise.
            print(
                "WARNING: --reprocess with --provider fake overwrites this day's real "
                "Daily memory with placeholder text, permanently -- a later plain run "
                "will never re-summarize it for real, since it's still 'already covered'."
            )
        provider = FakeProvider() if args.provider == "fake" else ClaudeCliProvider()
        try:
            with Store(store_path) as store, PipelineStore(store_path) as pipeline_store:
                outcomes = summarize_once(
                    store, pipeline_store, provider, memory_dir=memory_dir, reprocess=args.reprocess
                )
        except sqlite3.OperationalError as exc:
            # The Collector runs concurrently as a long-lived launchd
            # process against the same file (this project's normal
            # deployment shape) -- opening the stores themselves, not just
            # a later write, can hit lock contention.
            print(f"failed to open the store: {exc}")
            sys.exit(1)
        if not outcomes:
            print("nothing to summarize")
        for outcome in outcomes:
            if outcome.written:
                print(f"wrote {outcome.day.isoformat()}")
            elif outcome.error is None:
                print(f"skipped {outcome.day.isoformat()}: {outcome.skipped_reason}")
            else:
                print(f"failed {outcome.day.isoformat()}: {outcome.error}")
        return

    if args.command == "retrieve":
        store_path = args.store if args.store is not None else DEFAULT_STORE
        try:
            with PipelineStore(store_path) as pipeline_store:
                result = lookup(pipeline_store, args.start, args.end)
        except sqlite3.OperationalError as exc:
            print(f"failed to open the store: {exc}")
            sys.exit(1)
        if not result.dates:
            missing = ", ".join(d.isoformat() for d in result.missing)
            print(f"no Daily memory for: {missing}")
            sys.exit(1)
        print(result.text)
        if result.missing:
            missing = ", ".join(d.isoformat() for d in result.missing)
            print(f"\n(no Daily memory for: {missing})")
        return

    if args.command == "ask":
        store_path = args.store if args.store is not None else DEFAULT_STORE
        try:
            with PipelineStore(store_path) as pipeline_store:
                if not args.send:
                    # Free, no-network preview -- no provider call, same
                    # shape as `summarize`'s own preview (ticket #17).
                    ask_preview_result = ask_preview(pipeline_store)
                    if not ask_preview_result.dates:
                        print("no Daily memory exists yet")
                        sys.exit(1)
                    print(
                        f"{len(ask_preview_result.dates)} day(s), "
                        f"{ask_preview_result.total_chars} chars total:"
                    )
                    for day in ask_preview_result.dates:
                        print(f"  {day.isoformat()}")
                    print()
                    print("pass --send to call the provider and get an answer")
                    return
                day_contents = gather_all_daily_memories(pipeline_store)
        except sqlite3.OperationalError as exc:
            print(f"failed to open the store: {exc}")
            sys.exit(1)
        if not day_contents:
            print("no Daily memory exists yet")
            sys.exit(1)
        provider = FakeProvider() if args.provider == "fake" else ClaudeCliProvider()
        today = datetime.now(UTC).astimezone().date()
        result = provider.answer(args.question, day_contents, today=today)
        if result.state != DaySummaryState.COMPLETED:
            print(f"failed: {result.error_message}")
            sys.exit(1)
        print(result.answer)
        if result.cited_dates:
            print("\ncited: " + ", ".join(result.cited_dates))
        return

    # args.command == "run"
    store_path = args.store if args.store is not None else DEFAULT_STORE
    with Store(store_path) as store:
        run_forever(store, interval_seconds=args.interval)


if __name__ == "__main__":
    main()
