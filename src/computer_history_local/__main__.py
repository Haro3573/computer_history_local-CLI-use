"""Run the collector, manage its LaunchAgent, or summarize into local memory.

    python -m computer_history_local run [--store PATH] [--interval SECONDS]
    python -m computer_history_local install [--store PATH] [--interval SECONDS]
    python -m computer_history_local uninstall
    python -m computer_history_local status [--store PATH]
    python -m computer_history_local summarize [--store PATH] [--memory-dir PATH]
        [--provider {fake,claude-cli}] [--send] [--reprocess YYYY-MM-DD]
    python -m computer_history_local process-sessions [--store PATH] [--sessions-dir PATH]

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
from datetime import date
from pathlib import Path

from .collector import DEFAULT_INTERVAL_SECONDS, run_forever
from .launch_agent import install, status, uninstall
from .memory_pipeline import DEFAULT_MEMORY_DIR, preview, summarize_once
from .pipeline_store import PipelineStore
from .providers import PROVIDER_CHOICES, provider_for
from .retrieval import MemoryFileMissingError, ask_once, ask_preview, lookup
from .session_pipeline import DEFAULT_SESSIONS_DIR, process_sessions_once
from .session_store import SessionStore
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
    # No default -- see ADR-0008. Only matters once --send is passed (the
    # free preview path below never reads args.provider at all), so this
    # stays optional in argparse's eyes; main() enforces it's set before
    # a --send run ever reaches a provider.
    summarize_parser.add_argument("--provider", choices=PROVIDER_CHOICES, default=None)
    # Off by default -- Transfer's own consent gate (CONTEXT.md), the same
    # shape as `adhd_lifelog`'s `now --send`. Without it nothing is called
    # and nothing is written.
    summarize_parser.add_argument("--send", action="store_true")
    # Force-reprocess exactly this day, even if already covered -- never
    # touches the Watermark either way (ticket #16).
    summarize_parser.add_argument("--reprocess", type=date.fromisoformat, default=None)

    # Ticket #30: no --provider, no --send -- reading AI session files off
    # disk and reducing them has no consent gate (ADR-0009) and never
    # leaves the machine either way.
    process_sessions_parser = subparsers.add_parser(
        "process-sessions", help="Fold new AI session turns into sessions/*.md files"
    )
    process_sessions_parser.add_argument("--store", type=Path, default=None)
    process_sessions_parser.add_argument("--sessions-dir", type=Path, default=None)

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
    # No default -- see ADR-0008, same reasoning as `summarize`'s --provider.
    ask_parser.add_argument("--provider", choices=PROVIDER_CHOICES, default=None)
    ask_parser.add_argument("--send", action="store_true")

    return parser.parse_args(argv)


def _provider_or_exit(name: str | None):
    """`provider_for(name)`, or refuse -- ADR-0008: no default provider once
    `--send` is in play, since a forgotten `--provider` used to silently
    resolve to `fake` and advance the Watermark past a day that was never
    really summarized. Shared by `summarize --send` and `ask --send`."""
    if name is None:
        print(f"--provider is required with --send (choices: {', '.join(PROVIDER_CHOICES)})")
        sys.exit(1)
    return provider_for(name)


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
        provider = _provider_or_exit(args.provider)
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

    if args.command == "process-sessions":
        store_path = args.store if args.store is not None else DEFAULT_STORE
        sessions_dir = args.sessions_dir if args.sessions_dir is not None else DEFAULT_SESSIONS_DIR
        try:
            with SessionStore(store_path) as session_store:
                result = process_sessions_once(session_store, sessions_dir=sessions_dir)
        except sqlite3.OperationalError as exc:
            print(f"failed to open the store: {exc}")
            sys.exit(1)
        print(
            f"{result.files_processed} file(s) processed, "
            f"{result.files_skipped_live} live session(s) skipped, "
            f"{result.new_turn_count} new turn(s)"
        )
        print("days updated: " + ", ".join(result.days_written) if result.days_written else "nothing new")
        for error in result.errors:
            print(f"error: {error}")
        return

    if args.command == "retrieve":
        store_path = args.store if args.store is not None else DEFAULT_STORE
        try:
            with PipelineStore(store_path) as pipeline_store, SessionStore(store_path) as session_store:
                result = lookup(pipeline_store, args.start, args.end, session_store=session_store)
        except sqlite3.OperationalError as exc:
            print(f"failed to open the store: {exc}")
            sys.exit(1)
        except MemoryFileMissingError as exc:
            print(str(exc))
            sys.exit(1)
        if not result.dates:
            missing = ", ".join(d.isoformat() for d in result.missing)
            print(f"no Daily memory for: {missing}")
            sys.exit(1)
        print(result.text)
        if result.missing:
            missing = ", ".join(d.isoformat() for d in result.missing)
            print(f"\n(no Daily memory for: {missing})")
        if result.live_session_excluded:
            print("\n(current session omitted -- already in your context)")
        return

    if args.command == "ask":
        store_path = args.store if args.store is not None else DEFAULT_STORE
        if not args.send:
            try:
                with PipelineStore(store_path) as pipeline_store, SessionStore(store_path) as session_store:
                    # Free, no-network preview -- no provider call, same
                    # shape as `summarize`'s own preview (ticket #17).
                    ask_preview_result = ask_preview(pipeline_store, session_store)
            except sqlite3.OperationalError as exc:
                print(f"failed to open the store: {exc}")
                sys.exit(1)
            except MemoryFileMissingError as exc:
                print(str(exc))
                sys.exit(1)
            if not ask_preview_result.dates:
                print("no Daily memory exists yet")
                sys.exit(1)
            print(
                f"{len(ask_preview_result.dates)} day(s), "
                f"{ask_preview_result.total_chars} chars total:"
            )
            for day in ask_preview_result.dates:
                print(f"  {day.isoformat()}")
            if ask_preview_result.live_session_excluded:
                print("\n(current session omitted -- already in your context)")
            print()
            print("pass --send to call the provider and get an answer")
            return

        provider = _provider_or_exit(args.provider)
        try:
            with PipelineStore(store_path) as pipeline_store, SessionStore(store_path) as session_store:
                result = ask_once(pipeline_store, provider, args.question, session_store=session_store)
        except sqlite3.OperationalError as exc:
            print(f"failed to open the store: {exc}")
            sys.exit(1)
        except MemoryFileMissingError as exc:
            print(str(exc))
            sys.exit(1)
        if not result.answered:
            print("no Daily memory exists yet" if result.empty_corpus else f"failed: {result.error}")
            sys.exit(1)
        print(result.answer)
        if result.cited_dates:
            print("\ncited: " + ", ".join(result.cited_dates))
        if result.live_session_excluded:
            print("\n(current session omitted -- already in your context)")
        return

    # args.command == "run"
    store_path = args.store if args.store is not None else DEFAULT_STORE
    with Store(store_path) as store:
        run_forever(store, interval_seconds=args.interval)


if __name__ == "__main__":
    main()
