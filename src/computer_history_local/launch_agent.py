"""Keep the collector running when no terminal does.

Ticket #5: a foreground process in a terminal dies when the terminal
closes and never comes back. Mirrors `adhd_lifelog`'s `launch_agent.py` --
same shape, same reasoning -- rather than re-deriving it.

The part most likely to break is not launchd but macOS permissions. TCC
grants by *responsible process*; under launchd there is no Terminal, and
the Accessibility grant this Collector's window-title capture depends on
may not follow. That failure is silent, which makes `status()` worth
having even in a minimal form.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from .collector import DEFAULT_INTERVAL_SECONDS
from .store import DEFAULT_STORE, Store

LABEL = "local.computer-history-local.collector"
AGENTS_DIR = Path("~/Library/LaunchAgents")
LOG_DIR = Path("~/.local/share/computer-history-local")


def plist_path() -> Path:
    return (AGENTS_DIR / f"{LABEL}.plist").expanduser()


def build_plist(
    *,
    python: str | None = None,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    store: Path | None = None,
) -> dict:
    """The agent definition, as data so it can be asserted on in a test."""
    log_dir = LOG_DIR.expanduser()
    arguments = [
        python or sys.executable,
        "-m",
        "computer_history_local",
        "run",
        "--interval",
        str(interval),
    ]
    if store is not None:
        arguments += ["--store", str(store)]
    return {
        "Label": LABEL,
        "ProgramArguments": arguments,
        # AC: survives a logout/login cycle without manual restart.
        "RunAtLoad": True,
        "KeepAlive": True,
        # Never respawn faster than this -- a configuration mistake must not
        # become a spin loop that fills the log and the disk.
        "ThrottleInterval": 30,
        # AC: a status/log output exists so a failure doesn't fail silently.
        "StandardErrorPath": str(log_dir / "collector.err.log"),
        "StandardOutPath": str(log_dir / "collector.out.log"),
        # AC: subprocess calls don't rely on $PATH. launchd provides almost
        # nothing, and every OS call in this package already uses an
        # absolute path anyway; this is here so a future addition fails
        # loudly rather than depending on whatever a login shell exported.
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    }


def _launchctl(*arguments: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["/bin/launchctl", *arguments],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=20,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def install(
    *, interval: float = DEFAULT_INTERVAL_SECONDS, store: Path | None = None
) -> tuple[bool, str]:
    """Write the plist and load it. Idempotent: re-running replaces cleanly.

    AC: file paths are resolved here, at install time, and baked into the
    plist explicitly -- `store` is never left as `None` for the
    launchd-spawned process to compute `DEFAULT_STORE` fresh itself.
    """
    resolved_store = Path(store).expanduser() if store is not None else DEFAULT_STORE.expanduser()
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.expanduser().mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(build_plist(interval=interval, store=resolved_store)))

    # Unload first so a reinstall picks up new arguments. `bootout` fails
    # when nothing is loaded, which is not an error here.
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    code, output = _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    if code != 0:
        # Older macOS wants the deprecated verb.
        code, output = _launchctl("load", "-w", str(path))
    if code != 0:
        return False, f"launchctl failed: {output}"
    return True, str(path)


def uninstall() -> tuple[bool, str]:
    path = plist_path()
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    _launchctl("unload", str(path))
    if path.exists():
        path.unlink()
        return True, f"removed: {path}"
    return True, "not installed"


def is_loaded() -> bool:
    code, output = _launchctl("list")
    return code == 0 and LABEL in output


def status(*, store_path: Path | str = DEFAULT_STORE, now: datetime | None = None) -> str:
    """Minimal human-readable health check -- v1 scope, per the ticket."""
    now = now or datetime.now(UTC)
    lines = [
        f"loaded: {is_loaded()}",
        f"plist: {plist_path()} ({'exists' if plist_path().exists() else 'missing'})",
    ]
    with Store(store_path) as store:
        for kind in ("window", "idle", "browser", "system"):
            last = store.last_row(kind)
            if last is None:
                lines.append(f"{kind}: no rows yet")
                continue
            age = now - last.confirmed_until
            lines.append(f"{kind}: last row {age.total_seconds():.0f}s ago")

    log = LOG_DIR.expanduser() / "collector.err.log"
    if log.exists():
        try:
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
        except OSError:
            tail = []
        if tail:
            lines.append("recent errors:")
            lines.extend(f"  {line}" for line in tail)
    return "\n".join(lines)
