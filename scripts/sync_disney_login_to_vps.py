#!/usr/bin/env python3
"""Push DISNEY_LOGIN_EMAIL/PASSWORD to the VPS .env over SSH (encrypted transport).

The assistant cannot read your clipboard; on macOS you can pipe it:

  pbpaste | python3 scripts/sync_disney_login_to_vps.py --password-stdin

Optional: after updating .env, run Playwright seed + one worker poll on the VPS:

    pbpaste | python3 scripts/sync_disney_login_to_vps.py --password-stdin --run-seed-and-poll

If Disney blocks automated login (MFA etc.), push credentials without --run-seed-and-poll,
then complete login over SSH with a TTY (see docstring in seed_disney_session.py).

Requires SSH key access (same as PRODUCT.md VPS instructions).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


DEFAULT_EMAIL = ""
DEFAULT_HOST = os.environ.get("DISNEY_VPS_HOST", "")
DEFAULT_KEY = os.environ.get("DISNEY_VPS_SSH_KEY", "~/.ssh/disney_dining_vps")
REMOTE_ENV_PATH = "/opt/disney-dining-bot/.env"


def ssh_base(expanded_path: str, host: str) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-i", expanded_path, host]


def merge_remote_env(host: str, key_path: str, email: str, password: str) -> None:
    import shlex

    key_expanded = os.path.expanduser(key_path)
    remote_program = (
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "data = json.load(sys.stdin)\n"
        "email = str(data['email']).strip()\n"
        "password = str(data['password'])\n"
        f"path = Path({repr(REMOTE_ENV_PATH)})\n"
        "lines_out = []\n"
        "if path.exists():\n"
        "    for line in path.read_text().splitlines():\n"
        "        if line.startswith('DISNEY_LOGIN_EMAIL=') or line.startswith('DISNEY_LOGIN_PASSWORD='):\n"
        "            continue\n"
        "        lines_out.append(line)\n"
        "lines_out.append('DISNEY_LOGIN_EMAIL=' + json.dumps(email))\n"
        "lines_out.append('DISNEY_LOGIN_PASSWORD=' + json.dumps(password))\n"
        "path.write_text(chr(10).join(lines_out) + chr(10))\n"
        "print('DISNEY_LOGIN_* lines merged into', path)\n"
    )
    remote_cmd = "python3 -c " + shlex.quote(remote_program)
    cmd = ssh_base(key_expanded, host) + [remote_cmd]
    payload = json.dumps({"email": email, "password": password}).encode()
    r = subprocess.run(cmd, input=payload, capture_output=True)
    if r.returncode != 0:
        sys.stderr.buffer.write(r.stderr)
        raise SystemExit(r.returncode or 1)
    sys.stdout.buffer.write(r.stdout)


def run_remote_shell(host: str, key_path: str, script: str) -> subprocess.CompletedProcess:
    import os
    import shlex

    key_expanded = os.path.expanduser(key_path)
    # OpenSSH passes argv after the hostname as separate remote words (not shell-quoted).
    # ssh host bash -c set -e; foo  runs bash with -c's argument literally "set", not "set -e; foo".
    remote_one = "exec bash --norc --noprofile -c " + shlex.quote(script)
    cmd = ssh_base(key_expanded, host) + [remote_one]
    return subprocess.run(cmd, capture_output=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--key", default=DEFAULT_KEY)
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read password from stdin (use with pbpaste on macOS)",
    )
    parser.add_argument(
        "--run-seed-and-poll",
        action="store_true",
        help="After merge, run xvfb seed_disney_session.py then disney_bot.py --once",
    )
    args = parser.parse_args()

    if not args.email or not args.host:
        parser.error("--email and --host (or DISNEY_VPS_HOST) are required")

    if args.password_stdin:
        password = sys.stdin.read().rstrip("\r\n")
    else:
        import getpass

        password = getpass.getpass("Disney password: ")
    if not password:
        print("error: empty password", file=sys.stderr)
        raise SystemExit(2)

    merge_remote_env(args.host, args.key, args.email, password)
    print("Remote .env updated.")

    if args.run_seed_and_poll:
        remote = (
            "set -e; cd /opt/disney-dining-bot || exit 1; . .venv/bin/activate; "
            "DISNEY_HEADLESS=false xvfb-run -a timeout 300 python3 seed_disney_session.py || true; "
            "xvfb-run -a python3 disney_bot.py --once"
        )
        print("Running seed + one poll on VPS (may take a few minutes)...")
        r = run_remote_shell(args.host, args.key, remote)
        raise SystemExit(r.returncode or 0)


if __name__ == "__main__":
    main()
