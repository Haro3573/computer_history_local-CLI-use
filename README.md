# computer_history_local

A local, OpenAI/ChatGPT-independent equivalent of ChatGPT's Computer History:
broad macOS activity capture, summarized into local memory, retrievable
later. Built for one person, this Mac, cloud APIs first and local models
last.

See [CONTEXT.md](CONTEXT.md) for the domain glossary and architecture,
[docs/adr/](docs/adr/) for why specific decisions were made, and
[docs/later.md](docs/later.md) for ideas considered and deferred.

## Requirements

- macOS
- Python 3.11+
- [Claude Code](https://claude.com/claude-code)'s `claude` CLI, installed
  and logged into a Claude subscription — `summarize --send` and `ask
  --send` shell out to it (`claude -p`) rather than a metered API key, the
  same reasoning `adhd_lifelog` uses. Nothing else needs a network call or
  a new permission: `pmset` (sleep/wake) and window/idle capture are both
  built into macOS.

## Install

```bash
git clone <this repo>
cd computer_history_local
pip install -e .
python -m computer_history_local install   # writes + loads a LaunchAgent
```

`install` starts the Collector running unattended in the background
(window, idle, browser, and sleep/wake — see `CONTEXT.md`'s `State
sample`/`System event` entries), and keeps it running across reboots.
Check it's actually capturing:

```bash
python -m computer_history_local status
```

## Usage

Everything below is `python -m computer_history_local <command>`. Data
lives under `~/.local/share/computer-history-local/` — `state.sqlite3`
(captured activity + the `Memory Pipeline`'s own index/watermark) and
`memories/*.md` (one file per day, once summarized).

**Turn captured activity into a daily summary** (the `Memory Pipeline`,
manually triggered — nothing does this automatically yet):

```bash
python -m computer_history_local summarize                                   # free preview, no network call
python -m computer_history_local summarize --provider claude-cli --send      # spends real Claude usage, writes files
```

`--provider` has no default (`ADR-0008`) — it's required with `--send`, but
never read on a plain preview run above. `fake` is a network-free stand-in
provider for testing the wiring, not something you want against real data.

**Look up a day, or a range, for free** (`Retrieval`'s deterministic half —
no network call, no cost):

```bash
python -m computer_history_local retrieve 2026-08-17
python -m computer_history_local retrieve 2026-08-15 2026-08-17
```

**Ask a free-text question across every day summarized so far**
(`Retrieval`'s paid half — reads every `Daily memory` file that exists):

```bash
python -m computer_history_local ask "what was I doing last week"                                       # free preview: which days, how many chars
python -m computer_history_local ask "what was I doing last week" --provider claude-cli --send            # spends real Claude usage, answers
```

`--send` is the only thing that ever sends anything off this Mac — every
command works, and nothing costs money or leaves the machine, without it.

## Uninstall

```bash
python -m computer_history_local uninstall
```

Stops and removes the Collector's LaunchAgent. Captured data and generated
memories under `~/.local/share/computer-history-local/` are left in place.

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q
```

Issues and design decisions are tracked as GitHub issues, including
Wayfinder maps (`wayfinder:map` label) for larger architecture questions —
see `docs/agents/issue-tracker.md`.
