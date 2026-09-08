# computer_history_local

A local, OpenAI/ChatGPT-independent equivalent of ChatGPT's Computer
History: broad macOS activity capture, summarized into local memory,
retrievable later. Built for one person, this Mac.

See [ARCHITECTURE.md](ARCHITECTURE.md) for how this works end to end
(diagrams included) and the full detail on what's collected — readable on
its own, without installing or running anything.

## What this collects

Four channels, all written to one local `state.sqlite3`: frontmost app +
window title (redacted for secrets before it's ever written), idle/away
(a boolean, no duration), browser URL (Chrome/Safari/Arc/Brave, origin +
path only, no query/fragment/credentials), and sleep/wake (from macOS's
own `pmset` log). **Never captured, at any point**: keystrokes, clicks, or
screen/screenshot content.

Everything above stays on this Mac until `summarize --send` or
`ask --send` sends it to a cloud model — the only two commands that ever
leave the machine, and both require an explicit `--provider`.

## Requirements

- macOS, Python 3.11+
- [Claude Code](https://claude.com/claude-code)'s `claude` CLI, installed
  and logged into a Claude subscription — `summarize --send` and
  `ask --send` shell out to it (`claude -p`) rather than a metered API key.
- Optional: [Ollama](https://ollama.com), running, with `qwen3.5:4b`
  pulled, for `process-sessions` (summarizes AI session history into a
  paragraph, locally — nothing here reaches the network).

## Install

```bash
git clone https://github.com/Haro3573/computer_history_local-CLI-use.git
cd computer_history_local-CLI-use
pip install -e .
python -m computer_history_local install   # writes + loads a LaunchAgent
python -m computer_history_local status    # confirm it's capturing
```

## Usage

Everything below is `python -m computer_history_local <command>`. Data
lives under `~/.local/share/computer-history-local/`.

```bash
# Turn captured activity into a daily summary
summarize                                    # free preview, no network call
summarize --provider claude-cli --send       # spends real Claude usage, writes files

# Fold Claude Code/Codex session history into local files, locally
process-sessions

# Look up a day, or a range, for free
retrieve 2026-08-17
retrieve 2026-08-15 2026-08-17

# Ask a free-text question across everything summarized so far
ask "what was I doing last week"                                 # free preview
ask "what was I doing last week" --provider claude-cli --send    # spends real Claude usage

# Stop the Collector; captured data and generated memories are left in place
uninstall
```

`--send` is the only thing that ever sends anything off this Mac. The one
exception to "nothing costs money without `--send`" being free of
consequence: once you do pass it, AI session content goes along
unredacted — worth knowing before running `ask --send` over a day where
you pasted a real credential into a Claude Code or Codex session.
