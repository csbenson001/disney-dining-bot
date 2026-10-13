"""
Disney JWT auth: parse the token blob cookie, detect expiry, auto-refresh.

The TPR-WDW-LBJS.WEB-PROD.token cookie is a base64-encoded JSON object that
contains both an access_token (Bearer JWT, 24-hr TTL) and a refresh_token
(~6-month TTL).  We decode it once, persist the tokens, and refresh whenever
the access token is within 60 minutes of expiry.
"""

import base64
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

import storage  # noqa: E402

CLIENT_ID = "TPR-WDW-LBJS.WEB-PROD"
REFRESH_URL = (
    f"https://auth.registerdisney.go.com/v4/client/{CLIENT_ID}/guest/refreshAuth"
)
REFRESH_AHEAD_SECS = 600  # refresh only when access token has <10 min remaining
MIN_USABLE_TOKEN_SECS = 60  # access token must have >60s left to be usable as a fallback


# ── token persistence ──────────────────────────────────────────────────────

def _decode_token_blob(blob: str) -> dict:
    """Decode Disney's token cookie blob into access/refresh token data."""
    blob = (blob or "").strip()
    if "=" in blob[:4]:
        blob = blob.split("=", 1)[1]
    # Newer Disney cookies are shaped like base64url(json + jose_header).signature.
    # The token JSON is still the first decoded object.
    if "." in blob:
        blob = blob.split(".", 1)[0]

    def decode_once(value: str, *, urlsafe: bool = False) -> bytes:
        padding = (4 - len(value) % 4) % 4
        padded = value + ("=" * padding)
        decoder = base64.urlsafe_b64decode if urlsafe else base64.b64decode
        return decoder(padded)

    for urlsafe in (False, True):
        try:
            decoded = decode_once(blob, urlsafe=urlsafe).decode("utf-8")
            data, _ = json.JSONDecoder().raw_decode(decoded)
            return data
        except Exception:
            pass

    # Some older Disney token blobs are double-encoded.
    decoded = decode_once(decode_once(blob).decode("utf-8")).decode("utf-8")
    data, _ = json.JSONDecoder().raw_decode(decoded)
    return data


def _jwt_exp(token: str) -> int:
    """Return the exp (Unix timestamp) from a JWT without verifying signature."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Not a JWT")
    payload = parts[1]
    padding = 4 - len(payload) % 4
    if padding != 4:
        payload += "=" * padding
    data = json.loads(base64.b64decode(payload).decode("utf-8"))
    return int(data["exp"])


def load_tokens() -> dict:
    """
    Return {"access_token": ..., "refresh_token": ...}.

    Priority:
      1. tokens.json via storage (gist in cloud, file locally)
      2. DISNEY_ACCESS_TOKEN env var  (raw Bearer JWT — no refresh token)
      3. DISNEY_TOKEN_BLOB env var    (base64 cookie blob — includes refresh token)
    """
    saved = storage.read_json("tokens.json")
    if saved and saved.get("access_token"):
        return saved

    # Direct access token (grabbed from browser network intercept)
    direct = os.environ.get("DISNEY_ACCESS_TOKEN", "").strip()
    if direct:
        # Strip "BEARER " prefix if present
        if direct.upper().startswith("BEARER "):
            direct = direct[7:]
        tokens = {"access_token": direct, "refresh_token": ""}
        save_tokens(tokens)
        return tokens

    # Full token blob (base64 cookie — contains refresh token too)
    blob = os.environ.get("DISNEY_TOKEN_BLOB", "").strip()
    if not blob:
        raise RuntimeError(
            "No tokens found.  Set DISNEY_ACCESS_TOKEN in .env with the Bearer "
            "token captured from your browser's network requests to disneyworld.disney.go.com."
        )
    data = _decode_token_blob(blob)
    access = (
        data.get("access_token")
        or data.get("accessToken")
        or data.get("swid_token")
        or ""
    )
    refresh = (
        data.get("refresh_token")
        or data.get("refreshToken")
        or ""
    )
    if not access:
        raise RuntimeError(
            f"Could not find access_token in token blob.  Keys present: {list(data.keys())}"
        )
    tokens = {"access_token": access, "refresh_token": refresh}
    save_tokens(tokens)
    return tokens


def save_tokens(tokens: dict) -> None:
    storage.write_json("tokens.json", tokens)


# ── refresh ────────────────────────────────────────────────────────────────

def _do_refresh(tokens: dict) -> dict:
    """POST to Disney's refreshAuth endpoint and return updated tokens dict."""
    try:
        from curl_cffi import requests as cffi_requests
        resp = cffi_requests.post(
            REFRESH_URL,
            headers={
                "Authorization": f"BEARER {tokens['access_token']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"refreshToken": tokens["refresh_token"]},
            impersonate="chrome120",
            timeout=15,
        )
    except ImportError:
        import requests
        resp = requests.post(
            REFRESH_URL,
            headers={
                "Authorization": f"BEARER {tokens['access_token']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
            json={"refreshToken": tokens["refresh_token"]},
            timeout=15,
        )
    if resp.status_code != 200:
        body_preview = (resp.text or "")[:600]
        print(
            f"[auth] refresh non-200: HTTP {resp.status_code} from {REFRESH_URL}; "
            f"body: {body_preview!r}"
        )
        raise RuntimeError(
            f"Token refresh failed: HTTP {resp.status_code}\n{body_preview}"
        )
    body = resp.json()

    # Disney has returned several different shapes over the years — handle both
    new_access = (
        body.get("access_token")
        or body.get("accessToken")
        or (body.get("data") or {}).get("access_token")
        or ""
    )
    new_refresh = (
        body.get("refresh_token")
        or body.get("refreshToken")
        or (body.get("data") or {}).get("refresh_token")
        or tokens["refresh_token"]  # keep old one if not rotated
    )
    if not new_access:
        raise RuntimeError(
            f"Refresh response had no access_token.  Keys: {list(body.keys())}"
        )
    return {"access_token": new_access, "refresh_token": new_refresh}


def get_valid_token() -> str:
    """
    Return a Bearer access token that is valid for at least REFRESH_AHEAD_SECS.
    Refreshes and persists automatically if needed.
    """
    tokens = load_tokens()
    try:
        exp = _jwt_exp(tokens["access_token"])
        remaining = exp - time.time()
    except Exception:
        remaining = 0  # can't decode exp → force refresh

    if remaining < REFRESH_AHEAD_SECS:
        print(f"[auth] Token expires in {int(remaining)}s — refreshing …")
        try:
            tokens = _do_refresh(tokens)
            save_tokens(tokens)
            print("[auth] Token refreshed and saved.")
        except Exception as exc:
            if remaining > MIN_USABLE_TOKEN_SECS:
                print(
                    f"[auth] Token refresh failed ({type(exc).__name__}: {exc}); "
                    f"continuing with existing access token ({int(remaining)}s remaining)."
                )
            else:
                raise

    return tokens["access_token"]


# ── CLI helper ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tok = get_valid_token()
    exp = _jwt_exp(tok)
    remaining_min = int((exp - time.time()) / 60)
    print(f"Access token valid.  Expires in ~{remaining_min} minutes.")
    print(f"Token prefix: {tok[:40]}…")
