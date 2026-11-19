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
from typing import Iterable, Iterator

from utils.logger import logger


def _iter_proc_cmdlines() -> Iterator[tuple[int, list[str]]]:
    """Yield (pid, argv) for every readable /proc entry.

    Linux exposes each argv item as a separate NUL-terminated value. Keeping
    those boundaries intact is essential: profile paths may contain spaces and
    ``/price`` must not match ``/price-other``.
    """
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                raw = f.read()
        except OSError:
            continue
        args = [
            item.decode("utf-8", errors="replace")
            for item in raw.split(b"\0")
            if item
        ]
        yield int(entry), args


def profile_marker_set(
    profile_dir, legacy_markers: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return every exact path token that may identify this profile.

    The absolute path is authoritative. The raw/normalized relative value and
    the path relative to the current project directory keep cleanup compatible
    with Chrome processes launched by older bot versions. ``legacy_markers``
    adds explicitly configured relative tokens such as ``chrome_profile_avito``.
    """
    raw = os.path.expanduser(str(profile_dir))
    absolute = os.path.realpath(raw)
    candidates = {
        raw,
        os.path.normpath(raw),
        absolute,
        os.path.abspath(raw),
    }
    try:
        relative = os.path.relpath(absolute, os.getcwd())
        if relative != os.pardir and not relative.startswith(os.pardir + os.sep):
            candidates.update((relative, os.path.normpath(relative)))
    except ValueError:
        pass
    candidates.update(str(marker) for marker in legacy_markers if marker)
    return tuple(sorted(
        marker for marker in candidates
        if marker and marker not in (os.sep, ".")
    ))


def _looks_like_chrome(args: Iterable[str]) -> bool:
    executable = os.path.basename(next(iter(args), "")).lower()
    return "chrome" in executable or "chromium" in executable


def _matches_marker(args: Iterable[str], markers: tuple[str, ...]) -> bool:
    expected = set(markers)
    for index, arg in enumerate(args):
        if arg.startswith("--user-data-dir="):
            if arg.split("=", 1)[1] in expected:
                return True
        elif arg == "--user-data-dir" and index + 1 < len(args):
            if args[index + 1] in expected:
                return True
    return False


def profile_matches_args(
    args: Iterable[str], profile_dir, legacy_markers: Iterable[str] = (),
) -> bool:
    """Whether Chrome argv uses exactly this ``--user-data-dir`` argument."""
    args = list(args)
    return _looks_like_chrome(args) and _matches_marker(
        args, profile_marker_set(profile_dir, legacy_markers))


def _safe_kill(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        logger.debug("No permission to signal chrome pid %d", pid)


def sweep_chrome_tree(
    profile_dir, legacy_markers: Iterable[str] = (),
) -> int:
    """Terminate the ENTIRE Chrome tree owning this profile dir.

    Graceful SIGTERM first (lets Chrome flush cookies / session state to the
    persistent profile), then SIGKILL for survivors.  Returns the number of
    matching processes found in /proc (0 if none).
    """
    pids: list[int] = []
    for pid, args in _iter_proc_cmdlines():
        if profile_matches_args(args, profile_dir, legacy_markers):
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


async def async_sweep_chrome_tree(
    profile_dir, legacy_markers: Iterable[str] = (),
) -> int:
    """Async wrapper: run the blocking /proc sweep off the event loop."""
    return await asyncio.to_thread(
        sweep_chrome_tree, profile_dir, tuple(legacy_markers))
