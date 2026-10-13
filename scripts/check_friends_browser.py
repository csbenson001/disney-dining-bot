#!/usr/bin/env python3
"""
Navigate to Disney's Family & Friends page using the authenticated .coach_profile.
Captures the list of linked friends, status, and tickets/party sharing settings.
"""

import json
from pathlib import Path
from playwright.sync_api import sync_playwright

profile_dir = str(Path(__file__).parent.parent / ".coach_profile")
screenshots_dir = Path(__file__).parent.parent / "screenshots"

print(f"Opening browser using profile: {profile_dir}...")

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        profile_dir,
        headless=True,
        channel="chrome",
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        viewport={"width": 1280, "height": 900},
    )
    page = context.pages[0] if context.pages else context.new_page()

    api_responses = []

    def log_api(response):
        try:
            url = response.url
            if any(k in url for k in ["family", "friends", "party", "plans", "profile", "guest-service"]):
                if response.status == 200 and "json" in response.headers.get("content-type", ""):
                    data = response.json()
                    api_responses.append({"url": url, "data": data})
        except Exception:
            pass

    page.on("response", log_api)

    target_url = "https://disneyworld.disney.go.com/profile/family-friends/"
    print(f"Navigating to {target_url}...")
    page.goto(target_url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(5000)

    # Dismiss any cookie banners
    for sel in ["#onetrust-accept-btn-handler", "button:has-text('Accept')"]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1000):
                btn.click()
        except Exception:
            pass

    page.wait_for_timeout(3000)
    page.screenshot(path=str(screenshots_dir / "family_friends_page.png"))
    print(f"Screenshot saved to {screenshots_dir / 'family_friends_page.png'}")

    # Extract all text and elements from the friends list
    body_text = page.locator("body").inner_text()
    print("\n=== Page Content Summary ===")
    lines = [l.strip() for l in body_text.splitlines() if l.strip()]
    
    # Filter for names and plan sharing indicators
    print(f"Total lines extracted: {len(lines)}")
    for line in lines[:45]:
        print("  |", line)

    # Save any captured API JSON
    if api_responses:
        out_file = Path(__file__).parent.parent / "friends_api_dump.json"
        out_file.write_text(json.dumps(api_responses, indent=2))
        print(f"\n[Captured {len(api_responses)} API responses -> saved to {out_file}]")
        for resp in api_responses:
            print("  API URL:", resp["url"])

    context.close()
    print("Done!")

