#!/usr/bin/env python3
"""
Inspect Friends & Family list using headed Chrome with .coach_profile.
"""

from pathlib import Path
import json
import os
import time
from playwright.sync_api import sync_playwright

profile_dir = str(Path(__file__).parent.parent / ".coach_profile")
screenshots_dir = Path(__file__).parent.parent / "screenshots"

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        profile_dir,
        headless=False,
        channel="chrome",
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        viewport={"width": 1280, "height": 900},
    )
    page = context.pages[0] if context.pages else context.new_page()

    # Intercept API calls
    captured_data = []
    def log_response(resp):
        try:
            url = resp.url
            if any(k in url for k in ["family", "friends", "party", "plans", "guest-service", "profile"]):
                if resp.status == 200 and "json" in resp.headers.get("content-type", ""):
                    captured_data.append({"url": url, "data": resp.json()})
        except Exception:
            pass

    page.on("response", log_response)

    print("Navigating to Space 220 Lounge warmup...")
    page.goto("https://disneyworld.disney.go.com/dine-res/restaurant/space-220-lounge/", wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(3000)

    print("Navigating to Family & Friends list...")
    page.goto("https://disneyworld.disney.go.com/profile/family-friends/", wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(6000)

    page.screenshot(path=str(screenshots_dir / "friends_list_headed.png"))

    if captured_data:
        configured_path = os.environ.get("DISNEY_FRIENDS_DATA_PATH", "").strip()
        if not configured_path:
            raise RuntimeError("Set DISNEY_FRIENDS_DATA_PATH to a protected path outside the repository")
        out = Path(configured_path).expanduser().resolve()
        repository_root = Path(__file__).parent.parent.resolve()
        if out == repository_root or repository_root in out.parents:
            raise RuntimeError("DISNEY_FRIENDS_DATA_PATH must be outside the repository")
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        out.write_text(json.dumps(captured_data, indent=2))
        out.chmod(0o600)
        print(f"\n[Saved {len(captured_data)} captured API responses to protected runtime storage]")

    context.close()
    print("Done!")
