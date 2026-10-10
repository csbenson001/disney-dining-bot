"""Owner-scoped watch storage for the self-hosted dining monitor.

The website stores individual watches in ``watches.json``. Older installs used
``config.yaml`` grouped by restaurant; this module reads both so existing
watches keep working while new writes use the owner-aware format.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import yaml

import storage

WATCHES_FILE = "watches.json"
CONFIG_FILE = "config.yaml"
DEFAULT_OWNER_ID = os.environ.get("DEFAULT_OWNER_ID", "craig")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _today_iso_in_park_time() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def is_active_watch(watch: dict, today: Optional[str] = None) -> bool:
    today = today or _today_iso_in_park_time()
    return str(watch.get("date") or "") >= today


def _parse_users() -> Dict[str, dict]:
    raw = os.environ.get("WATCH_USERS") or os.environ.get("DISNEY_USERS") or ""
    if raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {
                    str(user_id): {
                        "id": str(user_id),
                        "name": str(data.get("name") or user_id).strip(),
                        "phone": str(data.get("phone") or "").strip(),
                        "password": str(data.get("password") or "").strip(),
                    }
                    for user_id, data in parsed.items()
                    if isinstance(data, dict)
                }
        except json.JSONDecodeError:
            pass

    # Backward-compatible single-user fallback for local development.
    return {
        DEFAULT_OWNER_ID: {
            "id": DEFAULT_OWNER_ID,
            "name": os.environ.get("DEFAULT_OWNER_NAME", "Craig"),
            "phone": os.environ.get("TWILIO_TO", ""),
            "password": os.environ.get("API_SECRET", ""),
        }
    }


def profiles(include_private: bool = False) -> Dict[str, dict]:
    users = _parse_users()
    if include_private:
        return users
    return {
        user_id: {
            "id": user_id,
            "name": user.get("name") or user_id,
            "has_phone": bool(user.get("phone")),
        }
        for user_id, user in users.items()
    }


def default_owner_id() -> str:
    users = profiles(include_private=True)
    if DEFAULT_OWNER_ID in users:
        return DEFAULT_OWNER_ID
    return next(iter(users), DEFAULT_OWNER_ID)


def recipient_for(owner_id: str, fallback: str = "") -> str:
    user = profiles(include_private=True).get(owner_id, {})
    return user.get("phone") or fallback


def watch_id(owner_id: str, facility_id: str, party_size: int, date: str) -> str:
    return f"{owner_id}__{facility_id}__{int(party_size)}__{date}"


def new_watch_id() -> str:
    return f"watch_{uuid.uuid4().hex[:16]}"


def parse_watch_id(value: str) -> Optional[dict]:
    parts = value.split("__")
    if len(parts) != 4:
        return None
    owner_id, facility_id, party_size, date = parts
    try:
        party_size_int = int(party_size)
    except ValueError:
        return None
    return {
        "owner_id": owner_id,
        "facility_id": facility_id,
        "party_size": party_size_int,
        "date": date,
    }


def _normalize_watch(raw: dict) -> dict:
    owner_id = str(raw.get("owner_id") or default_owner_id())
    facility_id = str(raw["facility_id"])
    party_size = int(raw.get("party_size", 2))
    date = str(raw["date"])
    normalized = {
        "watch_id": raw.get("watch_id") or raw.get("watchId") or new_watch_id(),
        "owner_id": owner_id,
        "facility_id": facility_id,
        "name": raw.get("name") or raw.get("restaurant_name") or facility_id,
        "slug": raw.get("slug") or facility_id,
        "party_size": party_size,
        "meal_periods": raw.get("meal_periods") or ["ALL"],
        "booking_type": raw.get("booking_type") or "dining",
        "date": date,
        "time_from": raw.get("time_from") or None,
        "time_to": raw.get("time_to") or None,
        "recipient_phone": raw.get("recipient_phone") or recipient_for(owner_id),
        "created_at": raw.get("created_at") or _utc_now(),
    }
    return normalized


def _migrate_config_yaml() -> List[dict]:
    text = storage.read_text(CONFIG_FILE, "")
    if not text:
        return []
    cfg = yaml.safe_load(text) or {}
    owner_id = default_owner_id()
    migrated: List[dict] = []
    for restaurant in cfg.get("restaurants", []):
        for date in restaurant.get("dates", []):
            migrated.append(_normalize_watch({
                "owner_id": owner_id,
                "facility_id": restaurant["facility_id"],
                "name": restaurant.get("name"),
                "slug": restaurant.get("slug"),
                "party_size": restaurant.get("party_size", 2),
                "meal_periods": restaurant.get("meal_periods", ["ALL"]),
                "date": date,
                "time_from": restaurant.get("time_from"),
                "time_to": restaurant.get("time_to"),
            }))
    return migrated


def load_watches(owner_id: Optional[str] = None, include_expired: bool = False) -> List[dict]:
    data = storage.read_json(WATCHES_FILE)
    if isinstance(data, dict):
        raw_watches = data.get("watches", [])
    elif isinstance(data, list):
        raw_watches = data
    else:
        raw_watches = _migrate_config_yaml()

    watches = [_normalize_watch(w) for w in raw_watches if w.get("facility_id") and w.get("date")]
    if not include_expired:
        today = _today_iso_in_park_time()
        watches = [w for w in watches if is_active_watch(w, today)]
    if owner_id:
        watches = [w for w in watches if w["owner_id"] == owner_id]

    try:
        import supabase_pipe
        if supabase_pipe.is_supabase_enabled():
            for target in supabase_pipe.sync_plans_from_supabase():
                for d in target.get("dates", []):
                    w_norm = _normalize_watch({
                        "owner_id": default_owner_id(),
                        "facility_id": target["facility_id"],
                        "name": target.get("name"),
                        "slug": target.get("slug"),
                        "party_size": target.get("party_size", 2),
                        "meal_periods": target.get("meal_periods", ["ALL"]),
                        "date": d,
                    })
                    if not any(w["facility_id"] == w_norm["facility_id"] and w["date"] == w_norm["date"] and w["party_size"] == w_norm["party_size"] for w in watches):
                        watches.append(w_norm)
    except Exception as exc:
        print(f'[watch_store] Supabase plan sync failed: {exc}')

    return sorted(watches, key=lambda w: (w["owner_id"], w["name"], w["date"], w["party_size"]))


def save_watches(watches: Iterable[dict]) -> None:
    normalized = [_normalize_watch(w) for w in watches]
    storage.write_json(WATCHES_FILE, {
        "schema_version": 1,
        "updated_at": _utc_now(),
        "watches": normalized,
    })


def add_watches(
    *,
    owner_id: str,
    facility_id: str,
    name: str,
    slug: str,
    party_size: int,
    meal_periods: List[str],
    dates: List[str],
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
) -> List[str]:
    watches = load_watches(include_expired=True)
    by_id = {w["watch_id"]: w for w in watches}
    added: List[str] = []
    for date in sorted(set(dates)):
        wid = new_watch_id()
        by_id[wid] = _normalize_watch({
            "watch_id": wid,
            "owner_id": owner_id,
            "facility_id": facility_id,
            "name": name,
            "slug": slug,
            "party_size": party_size,
            "meal_periods": meal_periods,
            "date": date,
            "time_from": time_from,
            "time_to": time_to,
        })
        added.append(wid)
    save_watches(by_id.values())
    return added


def remove_watch(wid: str, owner_id: Optional[str] = None) -> bool:
    watches = load_watches(include_expired=True)
    remaining = []
    removed = False
    for watch in watches:
        if watch["watch_id"] == wid and (owner_id is None or watch["owner_id"] == owner_id):
            removed = True
            continue
        remaining.append(watch)
    if removed:
        save_watches(remaining)
    return removed


def grouped_restaurant_requests(watches: Iterable[dict]) -> List[dict]:
    groups: Dict[tuple, dict] = {}
    for watch in watches:
        key = (
            watch["facility_id"],
            watch.get("slug") or watch["facility_id"],
            watch.get("name") or watch["facility_id"],
            int(watch.get("party_size", 2)),
            tuple(watch.get("meal_periods") or ["ALL"]),
            watch.get("booking_type") or "dining",
            watch.get("time_from"),
            watch.get("time_to"),
        )
        group = groups.setdefault(key, {
            "facility_id": watch["facility_id"],
            "slug": watch.get("slug") or watch["facility_id"],
            "name": watch.get("name") or watch["facility_id"],
            "party_size": int(watch.get("party_size", 2)),
            "meal_periods": list(watch.get("meal_periods") or ["ALL"]),
            "booking_type": watch.get("booking_type") or "dining",
            "time_from": watch.get("time_from"),
            "time_to": watch.get("time_to"),
            "dates": set(),
            "watches": [],
        })
        group["dates"].add(watch["date"])
        group["watches"].append(watch)

    result = []
    for group in groups.values():
        group["dates"] = sorted(group["dates"])
        result.append(group)
    return result


def public_watch(watch: dict) -> dict:
    return {k: v for k, v in watch.items() if k != "recipient_phone"}
