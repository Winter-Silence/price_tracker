import os
import subprocess
import shutil
from utils.logger import logger

XvfbProcess = list  # type alias


def ensure_xvfb(display: str = ":99") -> XvfbProcess | None:
    """Start Xvfb virtual display and optionally a window manager.

    Returns list of subprocesses [xvfb, wm?] or None on failure.
    """
    if not shutil.which("Xvfb"):
        logger.warning("Xvfb not found — browser windows may appear or fail")
        return None

    try:
        subprocess.run(["pkill", "-f", f"Xvfb {display}"],
                       stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    except Exception:
        pass

    xvfb = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1920x1080x24", "-ac"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = display
    logger.info("Started Xvfb on %s (pid=%d)", display, xvfb.pid)

    # Window manager — required by Ozon (document.hasFocus, WebGL)
    wm = None
    wm_bin = shutil.which("fluxbox")
    if wm_bin:
        wm = subprocess.Popen(
            [wm_bin],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info("Started fluxbox on %s (pid=%d)", display, wm.pid)
    else:
        logger.info("fluxbox not installed — Ozon may show captcha under xvfb")

    return [xvfb, wm] if wm else [xvfb]


def stop_xvfb(procs: XvfbProcess | None):
    """Stop xvfb and associated processes."""
    if procs is None:
        return
    for p in reversed(procs):
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception as exc:
                logger.error("Error stopping pid %d: %s", p.pid, exc)
    logger.info("Xvfb stopped")
