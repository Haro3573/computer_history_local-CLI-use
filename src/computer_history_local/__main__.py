"""Run the collector, manage its LaunchAgent, or summarize into local memory.

    python -m computer_history_local run [--store PATH] [--interval SECONDS]
    python -m computer_history_local install [--store PATH] [--interval SECONDS]
    python -m computer_history_local uninstall
    python -m computer_history_local status [--store PATH]
    python -m computer_history_local summarize [--store PATH] [--memory-dir PATH]
        [--provider {fake,claude-cli}] [--send]

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
import sys
from pathlib import Path

from .collector import DEFAULT_INTERVAL_SECONDS, run_forever
from .launch_agent import install, status, uninstall
from .memory_pipeline import DEFAULT_MEMORY_DIR, summarize_once
from .pipeline_store import PipelineStore
from .providers.claude_cli import ClaudeCliProvider
from .providers.fake import FakeProvider
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
            print("dry run: pass --send to call the provider and write memories")
            return
        provider = FakeProvider() if args.provider == "fake" else ClaudeCliProvider()
        with Store(store_path) as store, PipelineStore(store_path) as pipeline_store:
            outcomes = summarize_once(store, pipeline_store, provider, memory_dir=memory_dir)
        if not outcomes:
            print("nothing to summarize")
        for outcome in outcomes:
            if outcome.written:
                print(f"wrote {outcome.day.isoformat()}")
            elif outcome.error is None:
                print(f"skipped {outcome.day.isoformat()}: no data captured")
            else:
                print(f"failed {outcome.day.isoformat()}: {outcome.error}")
        return

    # args.command == "run"
    store_path = args.store if args.store is not None else DEFAULT_STORE
    with Store(store_path) as store:
        run_forever(store, interval_seconds=args.interval)


if __name__ == "__main__":
    main()
