"""Fabric tasks for deploying Price Tracker to a remote server.

Usage:
    fab deploy            # push local commits + deploy branch to server
    fab deploy --branch <name>
    fab deploy --no-push  # don't push locally, only pull on server
    fab status            # show systemd services status
    fab logs              # tail bot logs (default: 50 lines)
    fab logs --lines 200
    fab restart|stop|start
    fab rollback          # git reset --hard HEAD~1 then restart (use with care)
    fab rollback --steps 3
    fab setup             # initial install (runs scripts/deploy.sh on server)

Configuration is read from ./.deploy.env (see .deploy.env.example).
"""
from __future__ import annotations

import time
from pathlib import Path

from fabric import Connection
from invoke import Exit, task

DEPLOY_ENV_FILE = ".deploy.env"
DEFAULT_USER = "yury"
DEFAULT_PATH = f"/home/{DEFAULT_USER}/price_tracker"
DEFAULT_BRANCH = "main"
DEFAULT_LINES = 50


def _load_env() -> dict[str, str]:
    """Load .deploy.env (KEY=VALUE) into a dict."""
    env: dict[str, str] = {}
    env_path = Path(DEPLOY_ENV_FILE)
    if not env_path.exists():
        raise Exit(f"❌ {DEPLOY_ENV_FILE} not found. Copy .deploy.env.example "
                   f"to {DEPLOY_ENV_FILE} and fill in the values.")
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _conn() -> Connection:
    env = _load_env()
    host = env.get("DEPLOY_HOST")
    if not host:
        raise Exit("❌ DEPLOY_HOST is not set in .deploy.env")
    user = env.get("DEPLOY_USER", DEFAULT_USER)
    port = int(env.get("DEPLOY_PORT", "22"))
    kwargs: dict = {"user": user, "port": port}
    if env.get("DEPLOY_SSH_KEY"):
        kwargs["connect_kwargs"] = {"key_filename": env["DEPLOY_SSH_KEY"]}
    return Connection(host, **kwargs)


def _remote_path() -> str:
    return _load_env().get("DEPLOY_PATH", DEFAULT_PATH)


def _branch(c) -> str:
    return _load_env().get("DEPLOY_BRANCH", DEFAULT_BRANCH)


# ----- deploy --------------------------------------------------------------

@task
def deploy(c, branch=None, no_push=False):
    """Push local commits then pull on server, recreate venv, restart bot.

    Examples:
        fab deploy
        fab deploy --branch dev
        fab deploy --no-push
    """
    target_branch = branch or _branch(c)
    print(f"🚀 Deploying branch '{target_branch}'...")

    # 1. Local: ensure clean tree + push
    if not no_push:
        print("📦 Local: checking git state...")
        # fail if there are uncommitted changes that would interfere with push
        status = c.run("git status --porcelain", hide=True, warn=True)
        if status.ok and status.stdout.strip() and not _is_pushable_status(status.stdout):
            raise Exit("❌ Working tree has uncommitted changes. "
                       "Commit/stash first or use --no-push.")

        # fetch remote to check if we are ahead
        c.run("git push origin HEAD", warn=True)

    # 2. Connect to remote
    conn = _conn()
    remote_path = _remote_path()
    print(f"🔌 Connected to {conn.host} as {conn.user}")

    with conn.cd(remote_path):

        # 3. Git pull (reset --hard = pure mirror of remote branch)
        print("⬇️  Syncing repo...")
        conn.run("git fetch origin --prune", pty=True)
        conn.run(f"git reset --hard origin/{target_branch}", pty=True)
        conn.run("git clean -fd", warn=True)  # remove untracked (e.g., pycache)
        conn.run(f"git checkout {target_branch}", pty=True)
        current = conn.run("git rev-parse --short HEAD", hide=True).stdout.strip()
        print(f"📍 Now at: {current}")

        # 4. Update Python via asdf (in case .tool-versions changed)
        print("🐍 Updating Python toolchain...")
        conn.run('. "$HOME/.asdf/asdf.sh" && asdf install', pty=True,
                 warn=True)

        # 5. Recreate venv
        print("🔧 Recreating venv...")
        conn.run('rm -rf venv', pty=True)
        conn.run('. "$HOME/.asdf/asdf.sh" && asdf exec python -m venv venv',
                 pty=True)
        conn.run("./venv/bin/pip install --upgrade pip", pty=True)
        conn.run("./venv/bin/pip install -r requirements.txt", pty=True)

        # 6. Sanity check for .env
        env_check = conn.run("test -f .env", warn=True, hide=True)
        if not env_check.ok:
            raise Exit(f"❌ {remote_path}/.env missing on server. "
                       "Create it from .env.example before deploying again.")

        # 7. Migrations are applied automatically by main.py at startup;
        #    just restart the bot.
        print("♻️  Restarting systemd service...")
        conn.run("sudo systemctl restart price-tracker.service", pty=True)

        # 8. Wait & verify
        time.sleep(3)
        active = conn.run(
            "systemctl is-active price-tracker.service", hide=True
        ).stdout.strip()
        print(f"✅ Service status: {active}")

        if active != "active":
            print("⚠️  Service not active! Showing recent logs:")
            conn.run("journalctl -u price-tracker.service -n 30 --no-pager",
                     pty=True)
            raise Exit("❌ Deploy finished but service is not active.")

        # 9. Show last log lines for visual confirmation
        print("📜 Recent logs:")
        conn.run("journalctl -u price-tracker.service -n 20 --no-pager", pty=True)

    print(f"🎉 Deploy of {current} complete!")


def _is_pushable_status(status_output: str) -> bool:
    """Return True if the porcelain status is OK to push with (all staged)."""
    lines = [l for l in status_output.splitlines() if l.strip()]
    return all(l.startswith(("A ", "M  ", "D ", "R ", "C ", "??")) for l in lines)


# ----- rollback ------------------------------------------------------------

@task
def rollback(c, steps=1):
    """Hard-reset server to HEAD~N and restart bot. Use with care.

    Examples:
        fab rollback
        fab rollback --steps 3
    """
    n = int(steps)
    if n < 1:
        raise Exit("❌ steps must be >= 1")
    print(f"⏪ Rolling back {n} commit(s) on server...")

    conn = _conn()
    remote_path = _remote_path()
    with conn.cd(remote_path):
        conn.run(f"git reset --hard HEAD~{n}", pty=True)
        current = conn.run("git rev-parse --short HEAD", hide=True).stdout.strip()
        print(f"📍 Now at: {current}")

        conn.run("sudo systemctl restart price-tracker.service", pty=True)
        time.sleep(3)
        active = conn.run(
            "systemctl is-active price-tracker.service", hide=True
        ).stdout.strip()
        print(f"✅ Service status: {active}")

        print("📜 Recent logs:")
        conn.run("journalctl -u price-tracker.service -n 20 --no-pager", pty=True)

    print(f"🎉 Rolled back to {current}.")


# ----- service control -----------------------------------------------------

@task
def status(c):
    """Show systemd status of all 3 services."""
    conn = _conn()
    for svc in ("price-tracker-xvfb", "price-tracker-fluxbox", "price-tracker"):
        print(f"--- {svc} ---")
        conn.run(f"systemctl status {svc}.service --no-pager || true", pty=True)
        print()


@task
def logs(c, lines=DEFAULT_LINES):
    """Tail price-tracker logs.

    Examples:
        fab logs
        fab logs --lines 200
    """
    n = int(lines)
    conn = _conn()
    conn.run(f"journalctl -u price-tracker.service -n {n} --no-pager", pty=True)


@task
def restart(c):
    """Restart the bot service."""
    conn = _conn()
    conn.run("sudo systemctl restart price-tracker.service", pty=True)
    active = conn.run(
        "systemctl is-active price-tracker.service", hide=True
    ).stdout.strip()
    print(f"✅ Service status: {active}")


@task
def stop(c):
    """Stop the bot service."""
    conn = _conn()
    conn.run("sudo systemctl stop price-tracker.service", pty=True)
    print("🛑 Service stopped.")


@task
def start(c):
    """Start the bot service."""
    conn = _conn()
    conn.run("sudo systemctl start price-tracker.service", pty=True)
    active = conn.run(
        "systemctl is-active price-tracker.service", hide=True
    ).stdout.strip()
    print(f"✅ Service status: {active}")


# ----- initial setup -------------------------------------------------------

@task
def setup(c):
    """Run scripts/deploy.sh on the server (initial install).

    Requires the repo to already be cloned on the server.
    """
    conn = _conn()
    remote_path = _remote_path()
    print(f"🛠️  Running initial setup on {conn.host}...")
    with conn.cd(remote_path):
        conn.run("bash scripts/deploy.sh", pty=True)
    print("✅ Setup done. Try `fab status`.")


# ----- sanity check --------------------------------------------------------

@task
def check(c):
    """Sanity check: load config & try connecting."""
    env = _load_env()
    print(f"DEPLOY_HOST  = {env.get('DEPLOY_HOST', '')}")
    print(f"DEPLOY_USER  = {env.get('DEPLOY_USER', DEFAULT_USER)}")
    print(f"DEPLOY_PATH  = {env.get('DEPLOY_PATH', DEFAULT_PATH)}")
    print(f"DEPLOY_BRANCH= {env.get('DEPLOY_BRANCH', DEFAULT_BRANCH)}")
    print(f"DEPLOY_PORT  = {env.get('DEPLOY_PORT', '22')}")
    if env.get("DEPLOY_SSH_KEY"):
        print(f"DEPLOY_SSH_KEY = {env['DEPLOY_SSH_KEY']}")
    print()
    conn = _conn()
    print(f"🔌 Connecting to {conn.host}:{conn.port} as {conn.user}...")
    out = conn.run("hostname && whoami && uname -a", pty=True)
    if out.ok:
        print("✅ Connection OK.")
    else:
        raise Exit("❌ Connection failed.")
