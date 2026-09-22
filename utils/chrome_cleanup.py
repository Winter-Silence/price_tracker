"""Profile-scoped Chrome tree cleanup (stdlib only).

nodriver's ``Browser.aclose()`` only closes the CDP connection and terminates
the *main* bot Chrome PID.  A real Chrome binary spawns a whole tree of
child processes (renderer / GPU / network / utility / zygote), and every
descendant inherits the same ``--user-data-dir=<profile>`` marker on its
command line.  Killing just the main PID orphans those descendants, which
stay alive (holding the profile dir and RSS) and accumulate across poll
cycles — observed as ~7 GB of stuck ``chrome`` processes on the server.

This module sweeps the ENTIRE tree by scanning ``/proc/*/cmdline`` and
matching on the exact bot profile marker.  Matching on the profile marker
targets only the bot's own Chrome (and its descendants), never the system
Chrome, never the user's interactive Chrome, and never the bot's own Python
process (which does not carry the marker).  ``psutil`` is deliberately NOT
used — the project runs on stdlib only.
"""

import asyncio
import os
import signal
import time
from typing import Iterator

from utils.logger import logger


def _iter_proc_cmdlines() -> Iterator[tuple[int, str]]:
    """Yield (pid:int, cmdline:str) for every readable /proc entry."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                raw = f.read()
        except OSError:
            continue
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        yield int(entry), cmdline


def _profile_markers(profile_dir) -> tuple[str, ...]:
    """Return candidate markers that identify this profile in a cmdline.

    The bot passes the profile path as ``--user-data-dir=<profile_dir>``,
    and existing lock-matching code (base.py ``_process_is_our_chrome``)
    compares against the RAW string (often relative, e.g. ``./chrome_profile``).
    We cover absolute, raw, and basename forms so the sweep never misses.
    """
    raw = str(profile_dir)
    markers = {
        os.path.abspath(raw),
        raw,
        os.path.basename(os.path.abspath(raw)),
    }
    return tuple(m for m in markers if m and m != "/")


def _matches_marker(cmdline: str, markers: tuple[str, ...]) -> bool:
    return any(m in cmdline for m in markers)


def _safe_kill(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        logger.debug("No permission to signal chrome pid %d", pid)


def sweep_chrome_tree(profile_dir) -> int:
    """Terminate the ENTIRE Chrome tree owning this profile dir.

    Graceful SIGTERM first (lets Chrome flush cookies / session state to the
    persistent profile), then SIGKILL for survivors.  Returns the number of
    matching processes found in /proc (0 if none).
    """
    markers = _profile_markers(profile_dir)
    pids: list[int] = []
    for pid, cmdline in _iter_proc_cmdlines():
        if "chrome" in cmdline and _matches_marker(cmdline, markers):
            pids.append(pid)

    if not pids:
        return 0

    logger.info(
        "Chrome sweep for profile %s: %d matching process(es)",
        profile_dir, len(pids),
    )

    # Graceful pass — descendants flush cookies/profile state on SIGTERM.
    for pid in pids:
        _safe_kill(pid, signal.SIGTERM)
    time.sleep(2.0)

    # Force pass — anything still alive gets SIGKILL.
    for pid in pids:
        _safe_kill(pid, signal.SIGKILL)
    time.sleep(0.5)

    logger.debug("Chrome sweep done for %s (%d pids)", profile_dir, len(pids))
    return len(pids)


async def async_sweep_chrome_tree(profile_dir) -> int:
    """Async wrapper: run the blocking /proc sweep off the event loop."""
    return await asyncio.to_thread(sweep_chrome_tree, profile_dir)
