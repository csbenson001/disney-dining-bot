"""
Disney Lightning Lane Autonomous Booking Daemon (autobook_service.py)
Part of disney-dining-bot & disney-alerts integration.

Maintains the WDW Ride Coach session (wdwridecoach@gmail.com), resolves connected
family/friends party members, and executes Lightning Lane hold & booking API requests
directly on Disney's Virtual Access Service (VAS) backend.
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Load environment
ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

import auth
import storage

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [autobook] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("autobook")

AUTOBOOK_API_KEY = os.environ.get("AUTOBOOK_API_KEY", os.environ.get("API_SECRET", "disney_autobook_benson_2026_secret")).strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# Default Park Mapping
PARK_BY_RIDE_HINT: Dict[str, str] = {
    # Magic Kingdom: 80007944
    "magic": "80007944",
    "tron": "80007944",
    "space": "80007944",
    "mine train": "80007944",
    "big thunder": "80007944",
    "peter pan": "80007944",
    "jungle cruise": "80007944",
    "tiana": "80007944",
    # Epcot: 80007838
    "epcot": "80007838",
    "guardians": "80007838",
    "remy": "80007838",
    "test track": "80007838",
    "soarin": "80007838",
    "frozen": "80007838",
    # Hollywood Studios: 80007998
    "hollywood": "80007998",
    "rise of the resistance": "80007998",
    "slinky": "80007998",
    "tower of terror": "80007998",
    "runaway railway": "80007998",
    "millennium falcon": "80007998",
    # Animal Kingdom: 80007823
    "animal": "80007823",
    "flight of passage": "80007823",
    "expedition everest": "80007823",
    "safari": "80007823",
}

DEFAULT_PARK_ID = "80007944"  # Magic Kingdom

app = FastAPI(
    title="Disney Autonomous Booking Daemon",
    description="VPS backend executing automated Lightning Lane holds and bookings via coach account delegation.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth Guard ─────────────────────────────────────────────────────────────

def verify_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
    x_api_secret: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
):
    if not AUTOBOOK_API_KEY:
        return  # Open access in local test mode if not configured
    if request.method == "OPTIONS":
        return

    supplied_key = x_api_key or x_api_secret
    if not supplied_key and authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            supplied_key = parts[1]

    if supplied_key != AUTOBOOK_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid autobook API key")


# ── Party Roster Management ────────────────────────────────────────────────

def get_party_roster() -> List[Dict[str, Any]]:
    """Loads connected travelling party members from friends_api_data.json or fallback cache."""
    friends_path = Path(__file__).parent / "friends_api_data.json"
    roster = []

    if friends_path.exists():
        try:
            with open(friends_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Look for friends-by-plans block
                for item in data:
                    if isinstance(item, dict) and "friends-by-plans" in item.get("url", ""):
                        friends_block = item.get("data", {}).get("friends", {})
                        all_friends = friends_block.get("withoutPlans", []) + friends_block.get("withPlans", [])
                        for friend in all_friends:
                            name_obj = friend.get("name", {})
                            first = name_obj.get("firstName", "").strip()
                            last = name_obj.get("lastName", "").strip()
                            full_name = friend.get("fullName") or f"{first} {last}".strip()
                            g_ids = friend.get("guestIdentifiers", {})
                            guid = g_ids.get("guid") or g_ids.get("swid") or friend.get("id")
                            xid = g_ids.get("xid")
                            status = friend.get("status")

                            # Swap Neil Blandford -> Arielle Banford for the 5th guest slot
                            if guid == "92a080e7-6622-218c-4c4c-e97845462387" or "neil" in full_name.lower():
                                full_name = "Arielle Banford"
                                first = "Arielle"
                                last = "Banford"

                            roster.append({
                                "name": full_name,
                                "first_name": first,
                                "last_name": last,
                                "id": guid,
                                "guid": guid,
                                "xid": xid,
                                "status": status,
                                "connected": status == "ALREADY_CONNECTED",
                            })
        except Exception as e:
            logger.warning(f"Error parsing friends_api_data.json: {e}")

    # Fallback to verified Benson party definitions if file read produced incomplete list
    if not roster or len(roster) < 5:
        roster = [
            {
                "name": "chris benson",
                "first_name": "chris",
                "last_name": "benson",
                "id": "{A5DD1DA6-0AF1-4442-BCDF-6E567AB36C9F}",
                "guid": "{A5DD1DA6-0AF1-4442-BCDF-6E567AB36C9F}",
                "xid": "60B7B9B4-C079-3FCF-3496-5DCA3846C42F",
                "status": "ALREADY_CONNECTED",
                "connected": True,
            },
            {
                "name": "Mrs. meredith benson",
                "first_name": "meredith",
                "last_name": "benson",
                "id": "3b7f40bd-ec76-4aba-be1f-6907f3ab1d2d",
                "guid": "3b7f40bd-ec76-4aba-be1f-6907f3ab1d2d",
                "xid": "3065EFF1-9E34-A007-99FD-2F2F541D2F23",
                "status": "ALREADY_CONNECTED",
                "connected": True,
            },
            {
                "name": "Mr. ryan benson",
                "first_name": "ryan",
                "last_name": "benson",
                "id": "05635a6c-42d4-9a9d-3bba-34ac2e01add1",
                "guid": "05635a6c-42d4-9a9d-3bba-34ac2e01add1",
                "xid": "B4CA0BD5-1D44-8FA0-68FD-726E39BE0F97",
                "status": "ALREADY_CONNECTED",
                "connected": True,
            },
            {
                "name": "Mr. stephen benson",
                "first_name": "stephen",
                "last_name": "benson",
                "id": "6c2dcfed-fcef-40d7-8468-52295b61bcdd",
                "guid": "6c2dcfed-fcef-40d7-8468-52295b61bcdd",
                "xid": "6AAB3D66-1BE0-4377-61DD-27FA284CAFD7",
                "status": "ALREADY_CONNECTED",
                "connected": True,
            },
            {
                "name": "Arielle Banford",
                "first_name": "Arielle",
                "last_name": "Banford",
                "id": "92a080e7-6622-218c-4c4c-e97845462387",
                "guid": "92a080e7-6622-218c-4c4c-e97845462387",
                "xid": "0DDD26DF-543C-BD2B-D5C5-4604F8A2F3B8",
                "status": "ALREADY_CONNECTED",
                "connected": True,
            },
        ]
    return roster


def resolve_requested_guests(requested_names: Optional[List[str]]) -> List[Dict[str, Any]]:
    """Resolves requested names/ids against active party roster."""
    all_guests = get_party_roster()
    if not requested_names:
        return all_guests  # Default: book for all 5 party members

    matched = []
    for req in requested_names:
        req_clean = req.strip().lower()
        found = False
        for g in all_guests:
            g_name = g["name"].lower()
            g_first = g["first_name"].lower()
            g_last = g["last_name"].lower()
            g_id = str(g["id"]).lower()

            if (
                req_clean == g_id
                or req_clean in g_name
                or (g_first in req_clean and g_last in req_clean)
                or req_clean == g_first
                or (g_first == "arielle" and ("neil" in req_clean or "blandford" in req_clean or "banford" in req_clean or "arielle" in req_clean))
            ):
                if g not in matched:
                    matched.append(g)
                found = True
                break
        if not found:
            logger.warning(f"Could not resolve party member: '{req}'")

    return matched if matched else all_guests


# ── Disney VAS API Helpers ────────────────────────────────────────────────

def get_disney_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"BEARER {token}",
        "x-app-id": "TPR-WDW-LBJS.WEB-PROD",
        "x-correlation-id": str(uuid.uuid4()),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }


def infer_park_id(facility_id: str, ride_name: str, park_id: Optional[str]) -> str:
    if park_id and park_id.isdigit():
        return park_id
    low_name = (ride_name or "").lower()
    for hint, pid in PARK_BY_RIDE_HINT.items():
        if hint in low_name:
            return pid
    return DEFAULT_PARK_ID


def execute_disney_hold(
    token: str,
    target_date: str,
    guest_ids: List[str],
    park_id: str,
    facility_id: str,
    preferred_time: Optional[str] = None,
) -> Dict[str, Any]:
    """Generates an offer set (holds the inventory slot with Disney)."""
    url = "https://disneyworld.disney.go.com/ea-vas/planning/api/v1/experiences/offerset/generate"
    payload = {
        "date": target_date,
        "guestIds": guest_ids,
        "parkId": park_id,
        "experienceIds": [facility_id],
        "targetedTime": preferred_time or None,
        "ignoredBookedExperienceIds": None,
    }

    logger.info(f"Issuing Disney Hold -> {url} for facility {facility_id} (date: {target_date})")
    try:
        from curl_cffi import requests as cffi_requests
        resp = cffi_requests.post(
            url,
            headers=get_disney_headers(token),
            json=payload,
            impersonate="chrome120",
            timeout=15,
        )
    except ImportError:
        import requests
        resp = requests.post(
            url,
            headers=get_disney_headers(token),
            json=payload,
            timeout=15,
        )

    if resp.status_code not in (200, 201):
        logger.error(f"Hold failed HTTP {resp.status_code}: {resp.text[:400]}")
        raise RuntimeError(f"Disney Hold Failed (HTTP {resp.status_code}): {resp.text[:300]}")

    return resp.json()


def execute_disney_reserve(
    token: str,
    offer_set_id: str,
    order_guest_details: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Commits the held offer set into a confirmed Lightning Lane reservation."""
    url = "https://disneyworld.disney.go.com/ea-vas/planning/api/v1/experiences/entitlements/book"
    payload = {
        "offerSetId": offer_set_id,
        "orderGuestDetails": order_guest_details,
    }

    logger.info(f"Issuing Disney Reserve -> {url} (offerSetId: {offer_set_id})")
    try:
        from curl_cffi import requests as cffi_requests
        resp = cffi_requests.post(
            url,
            headers=get_disney_headers(token),
            json=payload,
            impersonate="chrome120",
            timeout=15,
        )
    except ImportError:
        import requests
        resp = requests.post(
            url,
            headers=get_disney_headers(token),
            json=payload,
            timeout=15,
        )

    if resp.status_code not in (200, 201):
        logger.error(f"Reservation commit failed HTTP {resp.status_code}: {resp.text[:400]}")
        raise RuntimeError(f"Disney Reserve Failed (HTTP {resp.status_code}): {resp.text[:300]}")

    return resp.json()


# ── Supabase Logging ────────────────────────────────────────────────────────

def log_autobook_event_to_supabase(
    watch_id: Optional[str],
    ride_name: str,
    outcome: str,
    details: Dict[str, Any],
):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    import requests

    try:
        alert_payload = {
            "watch_id": watch_id or "autobook-daemon",
            "channel": f"autobook_{outcome}",
            "condition_sig": f"AUTOBOOK:{ride_name}:{outcome.upper()}",
            "payload_json": {
                "message": f"🤖 Autobook {outcome.upper()}: {ride_name}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **details,
            },
        }
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        requests.post(f"{SUPABASE_URL}/rest/v1/alerts", headers=headers, json=alert_payload, timeout=5)
    except Exception as e:
        logger.warning(f"Failed to log autobook event to Supabase: {e}")


# ── Request / Response Models ──────────────────────────────────────────────

class BookingRequest(BaseModel):
    facility_id: str = Field(..., description="Disney Attraction Facility ID (e.g., 80010179 for Space Mountain)")
    ride_name: Optional[str] = Field(None, description="Human readable ride name")
    pass_type: Optional[str] = Field("LL_MULTI", description="LL_MULTI, LL_SINGLE, LL_RETURN_TIME, or LL_PAID")
    target_date: Optional[str] = Field(None, description="YYYY-MM-DD target date (defaults to today)")
    preferred_time: Optional[str] = Field(None, description="Preferred return time or 'ASAP'")
    party_members: Optional[List[str]] = Field(None, description="Names or IDs of party members to include")
    park_id: Optional[str] = Field(None, description="Disney Park ID (inferred if omitted)")
    dry_run: Optional[bool] = Field(False, description="Simulate validation and party resolution without booking")
    watch_id: Optional[str] = Field(None, description="Associated watch ID from disney-alerts")
    user_id: Optional[str] = Field(None, description="Initiating user ID")


# ── API Endpoints ──────────────────────────────────────────────────────────

@app.get("/health")
def health_check():
    """Returns daemon health, coach token freshness, and linked party status."""
    token_valid = False
    exp_remaining = 0
    token_err = None

    try:
        token = auth.get_valid_token()
        exp = auth._jwt_exp(token)
        exp_remaining = int(exp - time.time())
        token_valid = exp_remaining > 60
    except Exception as e:
        token_err = str(e)

    party = get_party_roster()
    tokens_meta = storage.read_json("tokens.json") or {}

    return {
        "status": "healthy" if token_valid else "degraded",
        "service": "Disney Autonomous Booking Daemon",
        "coach_account": tokens_meta.get("account_email", "wdwridecoach@gmail.com"),
        "coach_swid": tokens_meta.get("swid", "{6C27C4FE-D20C-4DEE-B27B-026DB6C9458E}"),
        "token_valid": token_valid,
        "token_expires_in_seconds": exp_remaining,
        "token_error": token_err,
        "party_count": len(party),
        "party_members": [p["name"] for p in party],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/party", dependencies=[Depends(verify_api_key)])
def list_party():
    """Lists all family & friends party members currently delegated to WDW Ride Coach."""
    party = get_party_roster()
    return {
        "count": len(party),
        "party": party,
    }


@app.post("/hold", dependencies=[Depends(verify_api_key)])
def hold_pass(req: BookingRequest):
    """Executes a reservation hold only (secures inventory slot without final commit)."""
    token = auth.get_valid_token()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_date = req.target_date or today_str
    park_id = infer_park_id(req.facility_id, req.ride_name or "", req.park_id)

    guests = resolve_requested_guests(req.party_members)
    guest_ids = [g["id"] for g in guests]

    logger.info(f"Requesting HOLD for {req.ride_name or req.facility_id} for {len(guest_ids)} guests")

    if req.dry_run:
        return {
            "ok": True,
            "status": "DRY_RUN_HOLD_SIMULATED",
            "facility_id": req.facility_id,
            "ride_name": req.ride_name,
            "target_date": target_date,
            "park_id": park_id,
            "party": [g["name"] for g in guests],
            "guest_ids": guest_ids,
            "offer_set_id": f"sim-hold-{uuid.uuid4()}",
            "expires_in_seconds": 300,
        }

    hold_data = execute_disney_hold(
        token=token,
        target_date=target_date,
        guest_ids=guest_ids,
        park_id=park_id,
        facility_id=req.facility_id,
        preferred_time=req.preferred_time,
    )

    return {
        "ok": True,
        "status": "HELD",
        "facility_id": req.facility_id,
        "ride_name": req.ride_name,
        "target_date": target_date,
        "party": [g["name"] for g in guests],
        "hold_response": hold_data,
    }


@app.post("/book", dependencies=[Depends(verify_api_key)])
def book_lightning_lane(req: BookingRequest):
    """
    Main Autonomous Booker Endpoint:
    1. Validates coach auth session.
    2. Resolves party members to Disney identifiers.
    3. Issues hold / offer generation on Disney VAS API.
    4. Confirms reservation entitlement booking.
    5. Records result to Supabase alerts feed.
    """
    start_time = time.time()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_date = req.target_date or today_str
    ride_name = req.ride_name or f"Attraction {req.facility_id}"
    park_id = infer_park_id(req.facility_id, ride_name, req.park_id)

    # 1. Resolve party
    guests = resolve_requested_guests(req.party_members)
    guest_ids = [g["id"] for g in guests]
    party_names = [g["name"] for g in guests]

    logger.info(f"⚡ AUTOBOOK ENGAGED for '{ride_name}' ({req.facility_id}) | Date: {target_date} | Party ({len(guests)}): {party_names}")

    # Dry-Run mode for testing/integration verification
    if req.dry_run:
        sim_confirmation = f"LL-{uuid.uuid4().hex[:8].upper()}"
        result = {
            "ok": True,
            "status": "CONFIRMED",
            "dry_run": True,
            "confirmation_id": sim_confirmation,
            "ride_name": ride_name,
            "facility_id": req.facility_id,
            "target_date": target_date,
            "return_time": req.preferred_time or "10:30 AM",
            "party": party_names,
            "guest_count": len(guests),
            "execution_ms": int((time.time() - start_time) * 1000),
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
        log_autobook_event_to_supabase(req.watch_id, ride_name, "success", result)
        return result

    # 2. Get active Disney coach session token
    try:
        token = auth.get_valid_token()
    except Exception as e:
        err_msg = f"Failed to acquire valid Disney token: {e}"
        logger.error(err_msg)
        log_autobook_event_to_supabase(req.watch_id, ride_name, "error", {"error": err_msg})
        raise HTTPException(status_code=502, detail=err_msg)

    # 3. Step A: Hold
    try:
        hold_res = execute_disney_hold(
            token=token,
            target_date=target_date,
            guest_ids=guest_ids,
            park_id=park_id,
            facility_id=req.facility_id,
            preferred_time=req.preferred_time,
        )
    except Exception as e:
        err_msg = f"Disney hold step failed: {e}"
        logger.error(err_msg)
        log_autobook_event_to_supabase(req.watch_id, ride_name, "error", {"error": err_msg})
        raise HTTPException(status_code=422, detail=err_msg)

    # Extract offer set id
    offer_set_groups = hold_res.get("offerSetGroup", {}).get("offerSets", [])
    if not offer_set_groups:
        err_msg = "No available offer sets returned by Disney during hold step"
        logger.warning(err_msg)
        log_autobook_event_to_supabase(req.watch_id, ride_name, "unavailable", {"error": err_msg})
        return {
            "ok": False,
            "status": "UNAVAILABLE",
            "message": err_msg,
            "ride_name": ride_name,
            "party": party_names,
        }

    offer_set_id = offer_set_groups[0].get("offerSetId")
    logger.info(f"Hold successfully secured! OfferSetId: {offer_set_id}")

    # Prepare order guest details from hold response party details
    party_details = hold_res.get("party", {}).get("guests", [])
    order_guest_details = []
    for g in party_details:
        order_details = g.get("orderDetails")
        if order_details:
            order_guest_details.append({
                "guestDetails": [{
                    "guestId": g.get("id"),
                    "externalIdentifier": order_details.get("externalIdentifier"),
                }],
                "orderId": order_details.get("orderId"),
                "orderItemId": order_details.get("orderItemId"),
            })

    # If orderGuestDetails structure not in response, construct from resolved guests
    if not order_guest_details:
        order_guest_details = [
            {
                "guestDetails": [{"guestId": gid}],
                "orderId": None,
                "orderItemId": None,
            }
            for gid in guest_ids
        ]

    # 4. Step B: Reserve
    try:
        reserve_res = execute_disney_reserve(
            token=token,
            offer_set_id=offer_set_id,
            order_guest_details=order_guest_details,
        )
    except Exception as e:
        err_msg = f"Disney reservation commit step failed: {e}"
        logger.error(err_msg)
        log_autobook_event_to_supabase(req.watch_id, ride_name, "error", {"error": err_msg, "offer_set_id": offer_set_id})
        raise HTTPException(status_code=502, detail=err_msg)

    confirmation_id = str(uuid.uuid4().hex[:10].upper())
    entitlements = reserve_res.get("entitlementExperiences", [])
    return_time = req.preferred_time or "Confirmed"
    if entitlements:
        return_time = entitlements[0].get("startDateTime", return_time)

    result_payload = {
        "ok": True,
        "status": "CONFIRMED",
        "confirmation_id": confirmation_id,
        "ride_name": ride_name,
        "facility_id": req.facility_id,
        "target_date": target_date,
        "return_time": return_time,
        "party": party_names,
        "guest_count": len(party_names),
        "execution_ms": int((time.time() - start_time) * 1000),
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "disney_response": reserve_res,
    }

    log_autobook_event_to_supabase(req.watch_id, ride_name, "success", result_payload)
    logger.info(f"🎉 BOOKING CONFIRMED: {ride_name} for {party_names} at {return_time}")
    return result_payload


@app.post("/reseed", dependencies=[Depends(verify_api_key)])
def reseed_session():
    """Trigger seed_coach_login.py to refresh or establish fresh Disney session tokens."""
    import subprocess
    cmd = [
        "python3",
        str(Path(__file__).parent / "seed_coach_login.py"),
        "--email", os.environ.get("DISNEY_LOGIN_EMAIL", "wdwridecoach@gmail.com"),
        "--password", os.environ.get("DISNEY_LOGIN_PASSWORD", "Christo123!"),
    ]
    if os.environ.get("DISNEY_HEADLESS", "false").lower() == "true":
        cmd.append("--headless")

    logger.info("Triggering Disney session re-seeder...")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return {
        "ok": True,
        "message": "Disney session seeder launched in background",
        "pid": proc.pid,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("AUTOBOOK_PORT", 8089))
    logger.info(f"Starting Disney Autonomous Booking Daemon on port {port}...")
    uvicorn.run("autobook_service:app", host="0.0.0.0", port=port, reload=True)
