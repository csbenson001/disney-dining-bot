"""
Supabase integration pipe for disney-dining-bot.
Pipes reservation availability directly into the disney-alerts platform.
"""

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests

from monitor import Slot

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def is_supabase_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def get_default_user_id() -> Optional[str]:
    """Finds the primary user in the disney-alerts database."""
    if not is_supabase_enabled():
        return None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/users?phone_e164=not.is.null&order=created_at.asc&limit=1",
            headers=_headers(),
            timeout=8,
        )
        if r.status_code == 200 and r.json():
            return r.json()[0]["id"]
    except Exception as e:
        print(f"[supabase_pipe] error fetching user: {e}")
    return None


def get_or_create_watch_for_facility(facility_id: str, restaurant_name: str) -> Optional[str]:
    """Resolves entity_id and ensures an active watch exists so foreign key is satisfied."""
    if not is_supabase_enabled():
        return None
    try:
        # 1. Lookup entity
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/entities?external_id=eq.{facility_id}&type=eq.RESTAURANT&limit=1",
            headers=_headers(),
            timeout=8,
        )
        entity = r.json()[0] if (r.status_code == 200 and r.json()) else None
        if not entity:
            # Fallback lookup by name
            r2 = requests.get(
                f"{SUPABASE_URL}/rest/v1/entities?name=ilike.*{restaurant_name}*&type=eq.RESTAURANT&limit=1",
                headers=_headers(),
                timeout=8,
            )
            entity = r2.json()[0] if (r2.status_code == 200 and r2.json()) else None

        user_id = get_default_user_id()
        if not user_id:
            return None

        # 2. Check existing watch for this entity
        if entity:
            r_w = requests.get(
                f"{SUPABASE_URL}/rest/v1/watches?entity_id=eq.{entity['id']}&user_id=eq.{user_id}&status=eq.active&limit=1",
                headers=_headers(),
                timeout=8,
            )
            if r_w.status_code == 200 and r_w.json():
                return r_w.json()[0]["id"]

            # Create an active watch for this restaurant
            new_watch = {
                "user_id": user_id,
                "park_id": entity["park_id"],
                "entity_id": entity["id"],
                "type": "DINING_WALKUP",
                "status": "active",
                "cooldown_min": 60,
            }
            r_create = requests.post(
                f"{SUPABASE_URL}/rest/v1/watches",
                headers=_headers(),
                json=new_watch,
                timeout=8,
            )
            if r_create.status_code in (200, 201) and r_create.json():
                return r_create.json()[0]["id"]

        # 3. Fallback: return any active watch for user
        r_any = requests.get(
            f"{SUPABASE_URL}/rest/v1/watches?user_id=eq.{user_id}&limit=1",
            headers=_headers(),
            timeout=8,
        )
        if r_any.status_code == 200 and r_any.json():
            return r_any.json()[0]["id"]

    except Exception as e:
        print(f"[supabase_pipe] error resolving watch for {facility_id}: {e}")
    return None


def pipe_slots_to_supabase(slots: List[Slot], booking_url_fn) -> int:
    """Pipes newly found reservation slots directly into the disney-alerts alerts table."""
    if not is_supabase_enabled() or not slots:
        return 0

    inserted = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for slot in slots:
        condition_sig = f"DINING_ADR:{slot.facility_id}:{slot.date}:{slot.time}:{slot.party_size}"
        try:
            # Deduplication check: did we alert on this exact condition in the last 2 hours?
            cutoff = datetime.fromtimestamp(time.time() - 7200, tz=timezone.utc).isoformat()
            r_dup = requests.get(
                f"{SUPABASE_URL}/rest/v1/alerts?condition_sig=eq.{condition_sig}&fired_at=gte.{cutoff}&limit=1",
                headers=_headers(),
                timeout=8,
            )
            if r_dup.status_code == 200 and r_dup.json():
                # Already alerted recently
                continue

            watch_id = get_or_create_watch_for_facility(slot.facility_id, slot.restaurant_name)
            if not watch_id:
                print(f"[supabase_pipe] unable to link watch for slot {slot.restaurant_name}")
                continue

            book_url = booking_url_fn(slot)
            time_label = slot.label or slot.time
            payload = {
                "kind": "DINING_ADR",
                "entity": slot.restaurant_name,
                "url": book_url,
                "message": (
                    f"🍽️ {slot.restaurant_name} table OPEN on {slot.date} at {time_label} "
                    f"(Party {slot.party_size}, {slot.meal_period})! Book in My Disney Experience: {book_url}"
                ),
                "date": slot.date,
                "time": slot.time,
                "partySize": slot.party_size,
                "mealPeriod": slot.meal_period,
            }

            alert_row = {
                "watch_id": watch_id,
                "channel": "sms",
                "condition_sig": condition_sig,
                "payload_json": payload,
                "fired_at": now_iso,
            }

            r_post = requests.post(
                f"{SUPABASE_URL}/rest/v1/alerts",
                headers=_headers(),
                json=alert_row,
                timeout=8,
            )
            if r_post.status_code in (200, 201):
                inserted += 1
                print(f"[supabase_pipe] Successfully piped ADR alert to disney-alerts: {slot.restaurant_name} ({slot.date} {time_label})")
            else:
                print(f"[supabase_pipe] failed to insert alert ({r_post.status_code}): {r_post.text}")
        except Exception as e:
            print(f"[supabase_pipe] failed to pipe slot {slot.restaurant_name}: {e}")

    return inserted


def sync_plans_from_supabase() -> List[Dict[str, Any]]:
    """Fetches unbooked dining plans from disney-alerts to dynamically supplement target restaurants."""
    if not is_supabase_enabled():
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/dining_plans_view?booked=eq.false&select=*&order=dine_date.asc",
            headers=_headers(),
            timeout=8,
        )
        if r.status_code != 200 or not r.json():
            return []

        plans = r.json()
        targets_by_facility: Dict[str, Dict[str, Any]] = {}

        for plan in plans:
            # Lookup entity facility_id
            e_id = plan.get("entity_id")
            if not e_id:
                continue
            r_ent = requests.get(
                f"{SUPABASE_URL}/rest/v1/entities?id=eq.{e_id}&select=external_id,name,booking_url&limit=1",
                headers=_headers(),
                timeout=8,
            )
            if r_ent.status_code == 200 and r_ent.json():
                ent = r_ent.json()[0]
                fac_id = ent.get("external_id")
                if not fac_id:
                    continue
                dine_date = plan.get("dine_date")
                party_size = plan.get("party_size", 2)
                slug = (ent.get("booking_url") or "").rstrip("/").split("/")[-1]

                if fac_id not in targets_by_facility:
                    targets_by_facility[fac_id] = {
                        "name": ent["name"],
                        "facility_id": fac_id,
                        "slug": slug,
                        "party_size": party_size,
                        "dates": [],
                        "meal_periods": ["BREAKFAST", "LUNCH", "DINNER"],
                    }
                if dine_date and dine_date not in targets_by_facility[fac_id]["dates"]:
                    targets_by_facility[fac_id]["dates"].append(dine_date)

        # Also check active watches with threshold_json->kind = ADR
        try:
            r_w = requests.get(
                f"{SUPABASE_URL}/rest/v1/watches?status=eq.active&select=entity_id,threshold_json",
                headers=_headers(),
                timeout=8,
            )
            if r_w.status_code == 200 and r_w.json():
                for w in r_w.json():
                    tj = w.get("threshold_json") or {}
                    if (tj.get("kind") == "ADR" or tj.get("date")) and tj.get("date"):
                        e_id = w.get("entity_id")
                        if not e_id:
                            continue
                        r_ent = requests.get(
                            f"{SUPABASE_URL}/rest/v1/entities?id=eq.{e_id}&select=external_id,name,booking_url&limit=1",
                            headers=_headers(),
                            timeout=8,
                        )
                        if r_ent.status_code == 200 and r_ent.json():
                            ent = r_ent.json()[0]
                            fac_id = ent.get("external_id")
                            if not fac_id:
                                continue
                            dine_date = tj.get("date")
                            party_size = tj.get("party_size", 2)
                            slug = (ent.get("booking_url") or "").rstrip("/").split("/")[-1]
                            mp = tj.get("meal_period")
                            periods = [mp] if mp and mp != "ALL" else ["BREAKFAST", "LUNCH", "DINNER"]

                            if fac_id not in targets_by_facility:
                                targets_by_facility[fac_id] = {
                                    "name": ent["name"],
                                    "facility_id": fac_id,
                                    "slug": slug,
                                    "party_size": party_size,
                                    "dates": [],
                                    "meal_periods": periods,
                                }
                            if dine_date and dine_date not in targets_by_facility[fac_id]["dates"]:
                                targets_by_facility[fac_id]["dates"].append(dine_date)
        except Exception as e_w:
            print(f"[supabase_pipe] error syncing watches from Supabase: {e_w}")

        return list(targets_by_facility.values())
    except Exception as e:
        print(f"[supabase_pipe] error syncing plans from Supabase: {e}")
        return []
