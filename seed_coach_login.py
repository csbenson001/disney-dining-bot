#!/usr/bin/env python3
"""
Dedicated session seeder for WDW Ride Coach (wdwridecoach@gmail.com).
Logs into Disney, captures auth cookies/JWT tokens, and inspects linked party.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Load env from current directory or parent
dotenv_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=dotenv_path)

TOKEN_COOKIE_NAME = "TPR-WDW-LBJS.WEB-PROD.token"
SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def _decode_token_blob(blob: str) -> dict:
    blob = (blob or "").strip()
    if "=" in blob[:4]:
        blob = blob.split("=", 1)[1]
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
    decoded = decode_once(decode_once(blob).decode("utf-8")).decode("utf-8")
    data, _ = json.JSONDecoder().raw_decode(decoded)
    return data


def _find_field(page, selectors, timeout_ms=3000):
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if loc.is_visible(timeout=timeout_ms):
                    return frame, loc
            except Exception:
                continue
    return None, None


def _dismiss_overlays(page):
    selectors = [
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
        "button:has-text('Continue')",
        "button:has-text('Agree')",
        "button[id*='accept' i]",
    ]
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if loc.is_visible(timeout=800):
                    loc.click(timeout=800)
                    page.wait_for_timeout(400)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Seed Disney session for Coach account")
    parser.add_argument("--headless", action="store_true", default=False, help="Run browser in headless mode")
    parser.add_argument("--email", default=os.environ.get("DISNEY_LOGIN_EMAIL", "wdwridecoach@gmail.com"))
    parser.add_argument("--password", default=os.environ.get("DISNEY_LOGIN_PASSWORD", "Christo123!"))
    parser.add_argument("--profile-dir", default=str(Path(__file__).parent / ".coach_profile"))
    parser.add_argument("--timeout-sec", type=int, default=120, help="Max seconds to wait for login completion")
    parser.add_argument("--otp", default=None, help="6-digit verification code sent to email")
    args = parser.parse_args()

    print(f"=== WDW Ride Coach Disney Authenticator ===")
    print(f"Target Account: {args.email}")
    print(f"Headless Mode: {args.headless}")
    print(f"Profile Dir: {args.profile_dir}")

    captured_tokens = {}

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                args.profile_dir,
                headless=args.headless,
                channel="chrome",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
                viewport={"width": 1280, "height": 850},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
            )
        except Exception as e:
            print(f"Chrome launch with channel failed ({e}), using default chromium...")
            context = p.chromium.launch_persistent_context(
                args.profile_dir,
                headless=args.headless,
                args=["--no-sandbox"],
                viewport={"width": 1280, "height": 850},
            )

        page = context.pages[0] if context.pages else context.new_page()

        # Check existing cookies first
        cookies = context.cookies(["https://disneyworld.disney.go.com", "https://disney.go.com"])
        for c in cookies:
            if c.get("name") == TOKEN_COOKIE_NAME:
                print(f"[auth] Valid auth cookie {TOKEN_COOKIE_NAME} already present!")
                captured_tokens["token_cookie"] = c.get("value")
                break

        if not captured_tokens.get("token_cookie"):
            # Load restaurant page first to warm up TLS and session
            warmup_url = "https://disneyworld.disney.go.com/dine-res/restaurant/space-220-lounge/"
            print(f"Loading warmup page: {warmup_url}...")
            try:
                page.goto(warmup_url, wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(2000)
                _dismiss_overlays(page)
            except Exception as e:
                print(f"Warmup page notice: {e}")

            # Click Sign In on the top nav
            sign_in_selectors = [
                "a[href*='login' i]",
                "button:has-text('Sign In')",
                "a:has-text('Sign In')",
                "text=Sign In",
                "#navigation-user-sign-in",
                "[data-testid*='sign-in' i]",
            ]
            frame, btn = _find_field(page, sign_in_selectors, timeout_ms=4000)
            if btn:
                print("Found 'Sign In' link/button. Clicking...")
                btn.click()
                page.wait_for_timeout(3000)
            else:
                print("Directly navigating to login URL...")
                page.goto("https://disneyworld.disney.go.com/login", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)

            _dismiss_overlays(page)

            # Loop to find email field
            email_selectors = [
                "input[type='email']",
                "input[name*='email' i]",
                "input[id*='email' i]",
                "input[autocomplete='email']",
                "input[autocomplete='username']",
                "input[name*='username' i]",
                "input[placeholder*='email' i]",
            ]
            print("Looking for email input field...")
            email_frame, email_input = None, None
            for attempt in range(12):
                email_frame, email_input = _find_field(page, email_selectors, timeout_ms=2000)
                if email_input:
                    break
                page.wait_for_timeout(1000)

            if email_input:
                print(f"Email field detected! Filling {args.email}...")
                email_input.fill(args.email)
                page.wait_for_timeout(800)
                page.screenshot(path=str(SCREENSHOTS_DIR / "1_email_entered.png"))

                # Click Continue/Submit
                continue_selectors = [
                    "button[type='submit']",
                    "button:has-text('Continue')",
                    "button:has-text('Next')",
                    "button:has-text('Sign In')",
                    "input[type='submit']",
                ]
                _, cont_btn = _find_field(page, continue_selectors, timeout_ms=2000)
                if cont_btn:
                    print("Clicking Continue button...")
                    cont_btn.click()
                else:
                    print("Submitting via Enter key...")
                    email_input.press("Enter")

                page.wait_for_timeout(4000)
                page.screenshot(path=str(SCREENSHOTS_DIR / "2_after_email_submit.png"))

                # Look for Password field
                pw_selectors = [
                    "input[type='password']",
                    "input[name*='password' i]",
                    "input[id*='password' i]",
                ]
                pw_frame, pw_input = None, None
                for attempt in range(10):
                    pw_frame, pw_input = _find_field(page, pw_selectors, timeout_ms=2000)
                    if pw_input:
                        break
                    page.wait_for_timeout(1000)

                if pw_input:
                    print("Password field detected! Entering password...")
                    pw_input.fill(args.password)
                    page.wait_for_timeout(800)
                    page.screenshot(path=str(SCREENSHOTS_DIR / "3_password_entered.png"))

                    # Click Sign In
                    submit_selectors = [
                        "button[type='submit']",
                        "button:has-text('Sign In')",
                        "button:has-text('Log In')",
                        "button:has-text('Continue')",
                    ]
                    _, submit_btn = _find_field(page, submit_selectors, timeout_ms=2000)
                    if submit_btn:
                        print("Clicking Sign In submit...")
                        submit_btn.click()
                    else:
                        pw_input.press("Enter")

                    page.wait_for_timeout(5000)
                    page.screenshot(path=str(SCREENSHOTS_DIR / "4_after_login_submit.png"))
                else:
                    print("Password field not detected. Could be an OTP or CAPTCHA screen.")
                    page.screenshot(path=str(SCREENSHOTS_DIR / "no_password_field.png"))
            else:
                print("Email field not found. Capturing current state screenshot...")
                page.screenshot(path=str(SCREENSHOTS_DIR / "no_email_field.png"))

            # Monitoring loop for cookies or OTP
            print("\nWaiting for authentication completion...")
            start_time = time.time()
            while time.time() - start_time < args.timeout_sec:
                cookies = context.cookies(["https://disneyworld.disney.go.com", "https://disney.go.com"])
                for c in cookies:
                    if c.get("name") == TOKEN_COOKIE_NAME:
                        captured_tokens["token_cookie"] = c.get("value")
                        print(f"\n[SUCCESS] Auth cookie {TOKEN_COOKIE_NAME} captured!")
                        break
                if captured_tokens.get("token_cookie"):
                    break

                # Check if OTP requested
                otp_found = False
                for fr in page.frames:
                    try:
                        txt = fr.locator("body").inner_text(timeout=500)
                        if any(k in txt.lower() for k in ["passcode", "security code", "check your email", "verification code", "one-time passcode"]):
                            print("[NOTICE] Disney is requesting a verification code sent to your email!")
                            otp_found = True
                            break
                    except Exception:
                        pass

                if otp_found:
                    page.screenshot(path=str(SCREENSHOTS_DIR / "otp_required.png"))
                    # Check for OTP code from args or file
                    otp_code = (args.otp or "").strip()
                    otp_file = Path(__file__).parent / "otp_code.txt"
                    if not otp_code and otp_file.exists():
                        try:
                            otp_code = otp_file.read_text().strip()
                        except Exception:
                            pass

                    if otp_code and len(otp_code) == 6 and otp_code.isdigit():
                        print(f"[OTP] Entering 6-digit code {otp_code}...")
                        inputs = fr.locator("input:visible").all()
                        if len(inputs) == 6:
                            for idx, digit in enumerate(otp_code):
                                inputs[idx].fill(digit)
                                page.wait_for_timeout(100)
                        elif len(inputs) >= 1:
                            inputs[0].fill(otp_code)

                        page.wait_for_timeout(500)
                        cont_btn = fr.locator("button:has-text('Continue'), button#BtnSubmit, button[type='submit']").first
                        if cont_btn.is_visible(timeout=2000):
                            print("[OTP] Clicking Continue...")
                            cont_btn.click()
                        else:
                            page.keyboard.press("Enter")
                        page.wait_for_timeout(4000)
                        if otp_file.exists():
                            otp_file.unlink(missing_ok=True)
                        args.otp = None  # Consume once
                    elif args.headless:
                        print("[NOTICE] Running headless and OTP requested. Please switch to headed mode or provide OTP code.")
                        break

                page.wait_for_timeout(2000)

        # Post-authentication processing
        if captured_tokens.get("token_cookie"):
            try:
                decoded = _decode_token_blob(captured_tokens["token_cookie"])
                acc = decoded.get("access_token") or decoded.get("accessToken") or decoded.get("swid_token")
                ref = decoded.get("refresh_token") or decoded.get("refreshToken", "")
                swid = decoded.get("swid", "")
                
                tokens_payload = {
                    "access_token": acc,
                    "refresh_token": ref,
                    "token_cookie": captured_tokens["token_cookie"],
                    "swid": swid,
                    "account_email": args.email,
                    "updated_at": time.time(),
                }
                tokens_path = Path(__file__).parent / "tokens.json"
                tokens_path.write_text(json.dumps(tokens_payload, indent=2))
                print(f"[STORED] Authenticated tokens saved to {tokens_path}!")
                print(f"JWT Access Token prefix: {acc[:45]}...")
                if swid:
                    print(f"Disney SWID: {swid}")
            except Exception as e:
                print(f"Error decoding token blob: {e}")

        page.screenshot(path=str(SCREENSHOTS_DIR / "final_screen.png"))
        context.close()
        print("Session script finished.")


if __name__ == "__main__":
    main()
