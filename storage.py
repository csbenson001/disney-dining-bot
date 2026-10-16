"""
State storage abstraction.

Cloud (GITHUB_GIST_ID + GITHUB_TOKEN set): reads/writes a private GitHub Gist.
Local dev (env vars absent): reads/writes files in the project directory.

Files managed: tokens.json, config.yaml, bot_state.json
"""

import json
import os
from pathlib import Path
from typing import Any, Optional

import requests as _req

_ROOT = Path(__file__).parent


def _gist_id() -> str:
    return os.environ.get("GITHUB_GIST_ID", "")


def _gh_token() -> str:
    return os.environ.get("GITHUB_TOKEN", "")


def _use_gist() -> bool:
    return bool(_gist_id() and _gh_token())


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_gh_token()}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }


def read_text(filename: str, default: str = "") -> str:
    if _use_gist():
        r = _req.get(
            f"https://api.github.com/gists/{_gist_id()}",
            headers=_headers(),
            timeout=10,
        )
        r.raise_for_status()
        entry = r.json().get("files", {}).get(filename, {})
        return entry.get("content", default) or default
    p = _ROOT / filename
    if not p.exists():
        return default
    return p.read_text()


def write_text(filename: str, content: str) -> None:
    if _use_gist():
        import time
        for attempt in range(3):
            r = _req.patch(
                f"https://api.github.com/gists/{_gist_id()}",
                headers=_headers(),
                json={"files": {filename: {"content": content}}},
                timeout=10,
            )
            if r.status_code == 409:
                time.sleep(2 + attempt * 2)
                continue
            r.raise_for_status()
            return
        r.raise_for_status()
    else:
        (_ROOT / filename).write_text(content)


def _supabase_config() -> tuple:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    return url, key


def read_json(filename: str, default: Any = None) -> Any:
    # Check Supabase app_config for tokens.json first
    if filename == "tokens.json":
        url, key = _supabase_config()
        if url and key:
            try:
                headers = {"apikey": key, "Authorization": f"Bearer {key}"}
                r = _req.get(f"{url}/rest/v1/app_config?key=eq.coach_tokens", headers=headers, timeout=5)
                if r.status_code == 200:
                    rows = r.json()
                    if rows and rows[0].get("value"):
                        val = rows[0]["value"]
                        return json.loads(val) if isinstance(val, str) else val
            except Exception:
                pass

    text = read_text(filename)
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def write_json(filename: str, data: Any) -> None:
    # Also persist tokens.json to Supabase app_config
    if filename == "tokens.json":
        url, key = _supabase_config()
        if url and key:
            try:
                headers = {
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                }
                payload = {"key": "coach_tokens", "value": data}
                _req.post(f"{url}/rest/v1/app_config", headers=headers, json=payload, timeout=5)
            except Exception:
                pass

    write_text(filename, json.dumps(data, indent=2))


def atomic_yaml_write(filename: str, content: str) -> None:
    """Write YAML; local uses atomic file replace, gist uses patch."""
    if _use_gist():
        write_text(filename, content)
    else:
        import tempfile
        p = _ROOT / filename
        with tempfile.NamedTemporaryFile(
            "w", dir=_ROOT, suffix=".tmp", delete=False
        ) as f:
            f.write(content)
            tmp = Path(f.name)
        os.replace(tmp, p)
