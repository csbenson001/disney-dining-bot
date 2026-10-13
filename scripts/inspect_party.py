#!/usr/bin/env python3
"""
Inspect Disney Friends & Family list and party linkages for WDW Ride Coach.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

tokens_path = Path(__file__).parent.parent / "tokens.json"
if not tokens_path.exists():
    print(f"Error: {tokens_path} not found.")
    sys.exit(1)

tokens = json.loads(tokens_path.read_text())
access_token = tokens["access_token"]
swid = tokens.get("swid") or "{6C27C4FE-D20C-4DEE-B27B-026DB6C9458E}"
clean_swid = swid.replace("{", "%7B").replace("}", "%7D")
token_cookie = tokens.get("token_cookie", "")

headers = {
    "Authorization": f"BEARER {access_token}",
    "Accept": "application/json;apiversion=1",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "X-Conversation-Id": "~WDPRO-SERVICE.GUEST-PORTAL-WEB.PROD",
    "Cookie": f"TPR-WDW-LBJS.WEB-PROD.token={token_cookie}",
}

endpoints = [
    ("Guest Profile", f"https://disneyworld.disney.go.com/api/wdpro/guest-service/guests/{clean_swid}/profile"),
    ("Friends & Family", f"https://disneyworld.disney.go.com/api/wdpro/guest-service/guests/{clean_swid}/friends-and-family"),
    ("Party Service", "https://disneyworld.disney.go.com/api/wdpro/party-service/public/parties"),
    ("My Plans", f"https://disneyworld.disney.go.com/api/wdpro/plans-service/guests/{clean_swid}/plans"),
    ("Entitlements / Tickets", f"https://disneyworld.disney.go.com/api/wdpro/entitlements-service/guests/{clean_swid}/entitlements"),
]

print(f"=== Inspecting Linked Disney Party for {tokens.get('account_email')} ===")
print(f"SWID: {swid}\n")

for label, url in endpoints:
    print(f"--- Fetching: {label} ---")
    print(f"URL: {url}")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            body = resp.read().decode("utf-8")
            print(f"Status: {status} OK")
            try:
                data = json.loads(body)
                print(f"Response Preview (Keys: {list(data.keys())}):")
                print(json.dumps(data, indent=2)[:600] + "...\n")
            except Exception:
                print(f"Raw Body: {body[:300]}...\n")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.reason}")
        try:
            err_body = e.read().decode("utf-8")
            print(f"Error Details: {err_body[:300]}")
        except Exception:
            pass
        print()
    except Exception as exc:
        print(f"Error: {exc}\n")

