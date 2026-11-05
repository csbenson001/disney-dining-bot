"""Owner-scoped Supabase bridge for ADR watches and delivery receipts.

Supabase rows are authoritative identities. The bridge never invents an owner,
substitutes an unrelated watch, or records an SMS before provider acceptance.
"""

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import requests

from monitor import Slot

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


@dataclass
class ReceiptRecordResult:
    inserted: int = 0
    duplicates: int = 0
    errors: List[str] = field(default_factory=list)


def is_supabase_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _get_rows(table: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=_headers(),
        params=params,
        timeout=8,
    )
    if response.status_code != 200:
        raise RuntimeError(f"{table} returned {response.status_code}: {response.text[:200]}")
    payload = response.json()
    return payload if isinstance(payload, list) else []


def _first(rows: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return next(iter(rows), None)


def _entity_for_reference(reference: str, fallback_name: str = "") -> Optional[Dict[str, Any]]:
    reference = str(reference or "").strip()
    if not reference:
        return None
    select = "id,external_id,name,booking_url"
    try:
        uuid.UUID(reference)
        entity = _first(_get_rows("entities", {"id": f"eq.{reference}", "select": select, "limit": "1"}))
        if entity:
            return entity
    except ValueError:
        pass
    entity = _first(_get_rows(
        "entities",
        {"external_id": f"eq.{reference}", "type": "eq.RESTAURANT", "select": select, "limit": "1"},
    ))
    if entity or not fallback_name:
        return entity
    return _first(_get_rows(
        "entities",
        {"name": f"ilike.*{fallback_name}*", "type": "eq.RESTAURANT", "select": select, "limit": "1"},
    ))


def _recipient_for_user(user_id: str) -> str:
    """Resolve only the phone attached to the exact verified owner profile."""
    user = _first(_get_rows(
        "users",
        {"id": f"eq.{user_id}", "select": "id,phone_e164", "limit": "1"},
    ))
    return str((user or {}).get("phone_e164") or "").strip()


def _watch_dates(threshold: Dict[str, Any]) -> List[str]:
    dates: List[str] = []
    raw_dates = threshold.get("dates")
    if isinstance(raw_dates, list):
        dates.extend(str(value) for value in raw_dates if value)
    for key in ("date", "dine_date", "target_date"):
        value = threshold.get(key)
        if value:
            dates.append(str(value))
    return list(dict.fromkeys(dates))


def _watch_entities(watch: Dict[str, Any], threshold: Dict[str, Any]) -> List[Dict[str, str]]:
    refs: List[Dict[str, str]] = []
    if watch.get("entity_id"):
        refs.append({"id": str(watch["entity_id"]), "name": str(threshold.get("entity_name") or "")})
    elif threshold.get("entity_id") or threshold.get("venue_id"):
        refs.append({
            "id": str(threshold.get("entity_id") or threshold.get("venue_id")),
            "name": str(threshold.get("entity_name") or ""),
        })
    bundled = threshold.get("bundled_entities")
    if isinstance(bundled, list):
        for item in bundled:
            if isinstance(item, dict) and item.get("id"):
                refs.append({"id": str(item["id"]), "name": str(item.get("name") or "")})
    unique: Dict[str, Dict[str, str]] = {}
    for ref in refs:
        unique.setdefault(ref["id"], ref)
    return list(unique.values())


def _targets_for_watch(watch: Dict[str, Any], seen: set) -> List[Dict[str, Any]]:
    watch_id = str(watch.get("id") or "").strip()
    user_id = str(watch.get("user_id") or "").strip()
    threshold = watch.get("threshold_json") if isinstance(watch.get("threshold_json"), dict) else {}
    if not watch_id or not user_id or str(threshold.get("kind") or "").upper() != "ADR":
        return []
    dates = _watch_dates(threshold)
    if not dates:
        return []
    recipient = _recipient_for_user(user_id)
    if not recipient:
        print(f"[supabase_pipe] skipping watch {watch_id}: no exact recipient")
        return []
    party_size = int(threshold.get("party_size") or 2)
    raw_period = str(threshold.get("meal_period") or "ALL").upper()
    meal_periods = [raw_period] if raw_period != "ALL" else ["BREAKFAST", "LUNCH", "DINNER"]
    targets: List[Dict[str, Any]] = []
    for ref in _watch_entities(watch, threshold):
        entity = _entity_for_reference(ref["id"], ref["name"])
        if not entity or not entity.get("external_id"):
            print(f"[supabase_pipe] skipping watch {watch_id}: unresolved restaurant {ref['id']}")
            continue
        facility_id = str(entity["external_id"])
        identity = (watch_id, facility_id)
        if identity in seen:
            continue
        seen.add(identity)
        slug = str(entity.get("booking_url") or "").rstrip("/").split("/")[-1]
        targets.append({
            "watch_id": watch_id,
            "owner_id": user_id,
            "recipient_phone": recipient,
            "name": entity.get("name") or ref["name"] or facility_id,
            "facility_id": facility_id,
            "slug": slug,
            "party_size": party_size,
            "dates": dates,
            "meal_periods": meal_periods,
        })
    return targets


def sync_plans_from_supabase() -> List[Dict[str, Any]]:
    """Return one poll target per exact active ADR watch and restaurant.

    Rows missing any delivery identity, date, or resolvable restaurant are
    skipped. A dangling dining plan cannot be attributed to another watch.
    """
    if not is_supabase_enabled():
        return []
    try:
        watches = _get_rows("watches", {
            "status": "eq.active",
            "select": "id,user_id,entity_id,type,threshold_json",
        })
        targets: List[Dict[str, Any]] = []
        seen = set()
        for watch in watches:
            try:
                targets.extend(_targets_for_watch(watch, seen))
            except Exception as exc:
                watch_id = str(watch.get("id") or "unknown")
                print(f"[supabase_pipe] skipping malformed watch {watch_id}: {exc}")
        return targets
    except Exception as exc:
        print(f"[supabase_pipe] error syncing watches from Supabase: {exc}")
        return []


def _condition_sig(slot: Slot) -> str:
    return ":".join([
        "DINING_ADR", slot.watch_id, slot.facility_id, slot.date, slot.time, str(slot.party_size),
    ])


def record_delivery_receipts(slots: List[Slot], booking_url_fn, receipt) -> ReceiptRecordResult:
    """Record provider-accepted sends against their exact watch foreign key."""
    result = ReceiptRecordResult()
    if not is_supabase_enabled() or not slots or getattr(receipt, "error", None):
        return result
    fired_at_dt = datetime.now(timezone.utc)
    fired_at = fired_at_dt.isoformat()
    dedupe_cutoff = (fired_at_dt - timedelta(hours=2)).isoformat()
    for slot in slots:
        if not slot.watch_id or not slot.owner_id or not slot.recipient_phone:
            print(f"[supabase_pipe] refusing unattributed receipt for {slot.restaurant_name}")
            continue
        condition_sig = _condition_sig(slot)
        try:
            duplicate_filter = {
                "watch_id": f"eq.{slot.watch_id}",
                "condition_sig": f"eq.{condition_sig}",
                "select": "id",
                "limit": "1",
            }
            message_id = str(getattr(receipt, "message_id", None) or "").strip()
            if message_id:
                duplicate_filter["payload_json->delivery->>messageSid"] = f"eq.{message_id}"
            else:
                duplicate_filter["fired_at"] = f"gte.{dedupe_cutoff}"
            duplicate = _get_rows("alerts", duplicate_filter)
            if duplicate:
                result.duplicates += 1
                continue
            book_url = booking_url_fn(slot)
            time_label = slot.label or slot.time
            payload = {
                "kind": "DINING_ADR",
                "entity": slot.restaurant_name,
                "url": book_url,
                "message": (
                    f"{slot.restaurant_name} table open on {slot.date} at {time_label} "
                    f"for {slot.party_size}. Book in My Disney Experience: {book_url}"
                ),
                "date": slot.date,
                "time": slot.time,
                "partySize": slot.party_size,
                "mealPeriod": slot.meal_period,
                "delivery": {
                    "provider": getattr(receipt, "provider", "unknown"),
                    "messageSid": getattr(receipt, "message_id", None),
                    "providerStatus": getattr(receipt, "status", "accepted"),
                },
            }
            response = requests.post(
                f"{SUPABASE_URL}/rest/v1/alerts",
                headers=_headers(),
                json={
                    "watch_id": slot.watch_id,
                    "channel": "sms",
                    "condition_sig": condition_sig,
                    "payload_json": payload,
                    "fired_at": fired_at,
                },
                timeout=8,
            )
            if response.status_code in (200, 201):
                result.inserted += 1
            else:
                error = f"watch {slot.watch_id} receipt insert returned {response.status_code}"
                result.errors.append(error)
                print(f"[supabase_pipe] {error}: {response.text}")
        except Exception as exc:
            error = f"watch {slot.watch_id} receipt persistence failed: {exc}"
            result.errors.append(error)
            print(f"[supabase_pipe] {error}")
    return result
