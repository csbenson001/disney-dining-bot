"""Read-only Disney calendar jobs, independent of alert watches and baselines.

One queued request is claimed atomically and handled after the normal alert
poll. Only validated provider responses produce complete per-date coverage.
This module never calls a notification provider or reads/writes alert state.
"""

import hashlib
import os
import re
import subprocess
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import requests

from monitor import _max_bookable_date, fetch_calendar_availability_via_playwright


class CalendarError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _database_config():
    return (
        os.environ.get("SUPABASE_URL", "").rstrip("/"),
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
    )


def _db(method: str, table: str, *, params=None, payload=None) -> List[Dict[str, Any]]:
    url, key = _database_config()
    response = requests.request(
        method,
        f"{url}/rest/v1/{table}",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
        params=params,
        json=payload,
        timeout=15,
    )
    if response.status_code not in (200, 201, 204):
        raise CalendarError("persistence_failed", f"{table} returned HTTP {response.status_code}")
    if response.status_code == 204:
        return []
    try:
        body = response.json()
    except ValueError as exc:
        raise CalendarError("persistence_failed", f"{table} returned invalid JSON") from exc
    if not isinstance(body, list):
        raise CalendarError("persistence_failed", f"{table} returned an invalid row response")
    return body


def _rpc(function_name: str, payload: Dict[str, Any]) -> Any:
    url, key = _database_config()
    response = requests.post(
        f"{url}/rest/v1/rpc/{function_name}",
        headers={
            "apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json=payload, timeout=15,
    )
    if response.status_code != 200:
        raise CalendarError("persistence_failed", f"{function_name} returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise CalendarError("persistence_failed", f"{function_name} returned invalid JSON") from exc


def _worker_commit():
    for key in ("DISSCAN_WORKER_COMMIT", "RAILWAY_GIT_COMMIT_SHA", "SOURCE_COMMIT"):
        value = os.environ.get(key, "")
        if re.fullmatch(r"[a-fA-F0-9]{40}", value):
            return value.lower()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent,
            capture_output=True, text=True, timeout=3, check=True,
        )
        value = result.stdout.strip()
        return value if re.fullmatch(r"[a-fA-F0-9]{40}", value) else None
    except (OSError, subprocess.SubprocessError):
        return None


def _date_range(start: str, end: str) -> List[str]:
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError) as exc:
        raise CalendarError("invalid_request", "Calendar dates are invalid") from exc
    if first > last or (last - first).days > 41:
        raise CalendarError("invalid_request", "Calendar range must contain 1 to 42 days")
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def _request_dates(job: Dict[str, Any]) -> List[str]:
    if job.get("destination", "WDW") != "WDW":
        raise CalendarError("unsupported_destination", "Calendar source currently supports Walt Disney World")
    if not re.fullmatch(r"\d+", str(job.get("facility_id") or "")):
        raise CalendarError("invalid_request", "Calendar restaurant facility is invalid")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(job.get("slug") or "")):
        raise CalendarError("invalid_request", "Calendar restaurant slug is invalid")
    party = job.get("party_size")
    if isinstance(party, bool) or not isinstance(party, int) or not 1 <= party <= 12:
        raise CalendarError("invalid_request", "Calendar party size must be from 1 to 12")
    return _date_range(job.get("start_date"), job.get("end_date"))


def normalize_calendar_response(result: Dict[str, Any], covered_dates: List[str]) -> List[Dict[str, Any]]:
    """A correct HTTP 200 certifies the exact requested interval, including empty days.

    Disney omits dates with no offers. Missing dates inside a valid response are
    empty; missing/invalid response structure is a failure, never a sold-out day.
    Raw offer identifiers remain private and only their hash is persisted.
    """
    if not isinstance(result, dict) or result.get("status") != 200:
        raise CalendarError("provider_http_error", "Disney did not return a successful calendar response")
    data = result.get("data")
    if not isinstance(data, dict) or data.get("statusCode") != 200:
        raise CalendarError("provider_malformed", "Disney returned an invalid calendar response")
    restaurants = data.get("restaurants")
    if not isinstance(restaurants, dict):
        raise CalendarError("provider_malformed", "Disney calendar coverage is missing")
    covered = set(covered_dates)
    slots, seen = [], set()
    for day, meals in restaurants.items():
        if day not in covered or not isinstance(meals, list):
            raise CalendarError("provider_malformed", "Disney returned unexpected calendar dates or meals")
        for meal in meals:
            if not isinstance(meal, dict):
                raise CalendarError("provider_malformed", "Disney returned an invalid meal group")
            period = str(meal.get("mealPeriodType") or "").upper()
            groups = meal.get("offersByAccessibility")
            if period not in ("BREAKFAST", "LUNCH", "DINNER") or not isinstance(groups, list):
                raise CalendarError("provider_malformed", "Disney returned incomplete meal coverage")
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("offers"), list):
                    raise CalendarError("provider_malformed", "Disney returned incomplete offer coverage")
                for offer in group["offers"]:
                    if not isinstance(offer, dict):
                        raise CalendarError("provider_malformed", "Disney returned an invalid reservation offer")
                    raw_time = offer.get("time")
                    if not isinstance(raw_time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", raw_time):
                        raise CalendarError("provider_malformed", "Disney returned an invalid reservation time")
                    clock = raw_time[:5]
                    identity = (day, clock, period)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    label = offer.get("label")
                    if not isinstance(label, str) or not label.strip():
                        label = datetime.strptime(clock, "%H:%M").strftime("%I:%M %p").lstrip("0")
                    offer_id = offer.get("offerId")
                    slots.append({
                        "date": day, "time": clock, "label": label[:80], "meal_period": period,
                        "offer_ref_hash": hashlib.sha256(str(offer_id).encode()).hexdigest() if offer_id else None,
                    })
    return sorted(slots, key=lambda slot: (slot["date"], slot["time"], slot["meal_period"]))


def _owns_claim(job: Dict[str, Any]) -> bool:
    token = job.get("claim_token")
    if not token:
        return False
    rows = _db("GET", "dining_calendar_requests", params={
        "id": f"eq.{job['id']}", "status": "eq.processing",
        "claim_token": f"eq.{token}", "select": "id", "limit": "1",
    })
    return len(rows) == 1


def _persist(job: Dict[str, Any], *, status: str, observed_at: str,
             provider_status=None, provider_request_id=None, dates=None,
             covered_dates=None, slots=None, error_code=None, error_message=None) -> str:
    scan_id = str(uuid.uuid4())
    scan = {
        "id": scan_id, "request_id": job["id"], "entity_id": job.get("entity_id"),
        "facility_id": job["facility_id"], "party_size": job["party_size"],
        "start_date": job["start_date"], "end_date": job["end_date"],
        "status": "failed", "source": "disney-dining-bot", "observed_at": observed_at,
        "provider_status": provider_status, "provider_request_id": provider_request_id,
        "worker_commit": _worker_commit(), "error_code": "persistence_pending",
        "error_message": "Calendar receipt is being persisted",
    }
    if len(_db("POST", "dining_calendar_scans", payload=scan)) != 1:
        raise CalendarError("persistence_failed", "Calendar scan insert was not confirmed")
    covered = set(covered_dates or [])
    days = [{"scan_id": scan_id, "date": day, "coverage_status": "complete" if day in covered else "unknown"} for day in dates or []]
    if days:
        if len(_db("POST", "dining_calendar_days", payload=days)) != len(days):
            raise CalendarError("persistence_failed", "Calendar day coverage insert was not confirmed")
    if slots:
        rows = _db("POST", "dining_calendar_slots", payload=[{"scan_id": scan_id, **slot} for slot in slots])
        if len(rows) != len(slots):
            raise CalendarError("persistence_failed", "Calendar reservation insert was not confirmed")
    if status in ("complete", "partial"):
        published = _rpc("publish_dining_calendar_scan", {
            "p_scan_id": scan_id, "p_request_id": job["id"],
            "p_claim_token": job["claim_token"], "p_status": status,
            "p_error_code": error_code, "p_error_message": error_message,
        })
        if published is not True:
            raise CalendarError("claim_lost", "Calendar request lease was replaced")
    else:
        published = _db("PATCH", "dining_calendar_scans", params={"id": f"eq.{scan_id}"}, payload={
            "status": status, "error_code": error_code, "error_message": error_message,
        })
        if len(published) != 1:
            raise CalendarError("persistence_failed", "Calendar scan publication was not confirmed")
    return scan_id


def process_pending_calendar_request() -> Dict[str, Any]:
    """Handle at most one job. Atomic status claim prevents duplicate workers.

    All browser work runs synchronously in the alert worker's existing loop.
    Failures remain independent of its polling health and notification state.
    """
    if not all(_database_config()):
        return {"processed": 0, "status": "disabled"}
    try:
        queued = _db("GET", "dining_calendar_requests", params={
            "status": "eq.queued", "order": "requested_at.asc", "limit": "1", "select": "*",
        })
        if not queued:
            return {"processed": 0, "status": "idle"}
        job = queued[0]
        claim_token = str(uuid.uuid4())
        claimed = _db("PATCH", "dining_calendar_requests", params={
            "id": f"eq.{job['id']}", "status": "eq.queued",
        }, payload={
            "status": "processing", "claimed_at": _utc_now(), "claim_token": claim_token,
            "attempt_count": int(job.get("attempt_count") or 0) + 1,
            "error_code": None, "error_message": None,
        })
        if not claimed:
            return {"processed": 0, "status": "claimed_elsewhere"}
        job = claimed[0]
    except Exception:
        return {"processed": 0, "status": "persistence_failed"}

    dates, covered_dates, result = [], [], {}
    try:
        dates = _request_dates(job)
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        maximum = _max_bookable_date().isoformat()
        covered_dates = [day for day in dates if today <= day <= maximum]
        if not covered_dates:
            raise CalendarError("outside_booking_window", "No requested dates are inside the current booking window")
        result = fetch_calendar_availability_via_playwright(
            facility_id=job["facility_id"], slug=job["slug"], party_size=job["party_size"],
            start_date=covered_dates[0], end_date=covered_dates[-1],
        )
        slots = normalize_calendar_response(result, covered_dates)
        if not _owns_claim(job):
            raise CalendarError("claim_lost", "Calendar request lease was replaced")
        status = "complete" if len(covered_dates) == len(dates) else "partial"
        scan_id = _persist(
            job, status=status, observed_at=_utc_now(), provider_status=result["status"],
            provider_request_id=result.get("request_id"), dates=dates,
            covered_dates=covered_dates, slots=slots,
            error_code="partial_booking_window" if status == "partial" else None,
            error_message="Dates outside the current booking window remain unknown" if status == "partial" else None,
        )
        completed = _db("PATCH", "dining_calendar_requests", params={
            "id": f"eq.{job['id']}", "status": "eq.processing", "claim_token": f"eq.{job['claim_token']}",
        }, payload={
            "status": "complete", "completed_at": _utc_now(), "error_code": None, "error_message": None,
        })
        if len(completed) != 1:
            raise CalendarError("persistence_failed", "Calendar request completion was not confirmed")
        return {"processed": 1, "status": status, "request_id": job["id"], "scan_id": scan_id}
    except Exception as exc:
        code = exc.code if isinstance(exc, CalendarError) else "provider_failed"
        if code == "claim_lost":
            return {"processed": 1, "status": "claim_lost", "request_id": job["id"], "error_code": code}
        # Never expose raw provider response bodies, authentication or offer tokens.
        message = str(exc) if isinstance(exc, CalendarError) else "Calendar provider request failed"
        try:
            _persist(job, status="failed", observed_at=_utc_now(),
                     provider_status=result.get("status") if isinstance(result, dict) else None,
                     dates=dates, error_code=code, error_message=message)
        except Exception:
            code = "persistence_failed"
        # A broken child table must not strand an otherwise writable queue row.
        try:
            _db("PATCH", "dining_calendar_requests", params={
                "id": f"eq.{job['id']}", "status": "eq.processing", "claim_token": f"eq.{job['claim_token']}",
            }, payload={
                "status": "failed", "completed_at": _utc_now(), "error_code": code, "error_message": message,
            })
        except Exception:
            code = "persistence_failed"
        return {"processed": 1, "status": "failed", "request_id": job["id"], "error_code": code}
