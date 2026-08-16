"""Ask macOS what's in front, once, and turn the answer into a Sample.

Built on `osascript` rather than PyObjC, mirroring `adhd_lifelog`'s
`mac_sampler.py`: a subprocess needs nothing installed and can be deleted by
removing one file. Everything macOS-specific lives here, behind `sample()`,
so redaction and merging are testable on any machine.

Permissions: app name needs nothing; the window title needs Accessibility
(System Settings -> Privacy & Security -> Accessibility). Without it the app
name still arrives and the title is None -- a smaller record, not a failed
one.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime

_OSASCRIPT = "/usr/bin/osascript"

_FRONTMOST = """
tell application "System Events"
    set frontApp to first application process whose frontmost is true
    set appName to name of frontApp
    try
        set winTitle to name of front window of frontApp
    on error
        set winTitle to ""
    end try
end tell
return appName & "\\n" & winTitle
"""


@dataclass(frozen=True)
class Sample:
    app: str | None
    title: str | None
    at: datetime
    errors: tuple[str, ...] = ()


def _run(command: list[str], *, timeout: float = 5.0) -> tuple[str | None, str | None]:
    """Return (stdout, error). Never raises -- a sampler that dies takes the
    day's record with it, which is worse than one that logs a blank."""
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        return None, (completed.stderr or "").strip()[:200] or f"exit {completed.returncode}"
    return completed.stdout.strip(), None


def sample(*, now: datetime | None = None) -> Sample:
    """One look at the machine. macOS only; returns blanks elsewhere."""
    now = now or datetime.now(UTC)
    errors: list[str] = []

    output, error = _run([_OSASCRIPT, "-e", _FRONTMOST])
    if error:
        errors.append(f"frontmost: {error}")

    app = title = None
    if output:
        parts = output.split("\n", 1)
        app = parts[0].strip() or None
        title = (parts[1].strip() if len(parts) > 1 else "") or None

    return Sample(app=app, title=title, at=now, errors=tuple(errors))
