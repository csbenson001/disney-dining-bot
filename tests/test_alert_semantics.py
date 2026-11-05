import base64
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import disney_bot
import monitor
import notify
import supabase_pipe
import watch_store
from monitor import Slot
from notify import DeliveryReceipt, _format_message, booking_url


def make_slot(**overrides):
    data = {
        "restaurant_name": "'Ohana",
        "facility_id": "90002606",
        "slug": "ohana",
        "date": "2026-07-01",
        "time": "19:50",
        "label": "07:50 PM",
        "meal_period": "DINNER",
        "party_size": 2,
        "offer_id": "offer-123",
        "owner_id": "craig",
        "watch_id": "watch_abc",
        "recipient_phone": "whatsapp:+15555550123",
    }
    data.update(overrides)
    return Slot(**data)


def make_jwt(exp):
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def make_token_blob(access_token, refresh_token=""):
    payload = {"access_token": access_token, "refresh_token": refresh_token}
    return base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def make_signed_token_blob(access_token, refresh_token=""):
    payload = {"access_token": access_token, "refresh_token": refresh_token}
    blob = json.dumps(payload) + json.dumps({"kid": "guestcontroller", "alg": "ES256"})
    return "5=" + base64.urlsafe_b64encode(blob.encode()).decode().rstrip("=") + ".signature.extra"


class AlertSemanticsTest(unittest.TestCase):
    def test_filter_new_baselines_first_snapshot_without_alerting(self):
        slot = make_slot()
        original_load_open_keys = disney_bot._load_open_keys
        try:
            disney_bot._load_open_keys = lambda: None
            self.assertEqual(disney_bot.filter_new([slot], previous_open_keys=None), [])
        finally:
            disney_bot._load_open_keys = original_load_open_keys

    def test_filter_new_does_not_realert_still_open_slot(self):
        slot = make_slot()
        self.assertEqual(
            disney_bot.filter_new([slot], previous_open_keys={disney_bot._slot_key(slot)}),
            [],
        )

    def test_filter_new_returns_only_unseen_exact_slots(self):
        seen_slot = make_slot(time="19:50", label="07:50 PM", offer_id="offer-seen")
        new_slot = make_slot(time="20:55", label="08:55 PM", offer_id="offer-new")
        self.assertEqual(
            disney_bot.filter_new(
                [seen_slot, new_slot],
                previous_open_keys={disney_bot._slot_key(seen_slot)},
            ),
            [new_slot],
        )

    def test_filter_new_alerts_when_slot_reopens_after_being_absent(self):
        reopened_slot = make_slot(time="19:50", label="07:50 PM", offer_id="offer-new")
        previously_open_other_time = make_slot(time="20:55", label="08:55 PM", offer_id="offer-other")
        self.assertEqual(
            disney_bot.filter_new(
                [reopened_slot, previously_open_other_time],
                previous_open_keys={disney_bot._slot_key(previously_open_other_time)},
            ),
            [reopened_slot],
        )

    def test_offer_id_is_not_part_of_stable_slot_identity(self):
        old_offer = make_slot(time="19:50", label="07:50 PM", offer_id="offer-a")
        new_offer = make_slot(time="19:50", label="07:50 PM", offer_id="offer-b")
        self.assertEqual(
            disney_bot.filter_new([new_offer], previous_open_keys={disney_bot._slot_key(old_offer)}),
            [],
        )

    def test_failed_restaurant_poll_preserves_previous_open_keys(self):
        old_slot = make_slot()
        old_key = disney_bot._slot_key(old_slot)
        next_keys = disney_bot._next_open_keys(
            current_open_keys=set(),
            previous_open_keys={old_key},
            new_slots=[],
            sent_slots=[],
            failed_open_prefixes=[disney_bot._watch_open_prefix({
                "watch_id": old_slot.watch_id,
                "facility_id": old_slot.facility_id,
            })],
        )
        self.assertIn(old_key, next_keys)

    def test_failed_new_alert_is_not_baselined_until_sent(self):
        new_slot = make_slot()
        new_key = disney_bot._slot_key(new_slot)
        next_keys = disney_bot._next_open_keys(
            current_open_keys={new_key},
            previous_open_keys=set(),
            new_slots=[new_slot],
            sent_slots=[],
            failed_open_prefixes=[],
        )
        self.assertNotIn(new_key, next_keys)

    def test_sent_new_alert_is_baselined(self):
        new_slot = make_slot()
        new_key = disney_bot._slot_key(new_slot)
        next_keys = disney_bot._next_open_keys(
            current_open_keys={new_key},
            previous_open_keys=set(),
            new_slots=[new_slot],
            sent_slots=[new_slot],
            failed_open_prefixes=[],
        )
        self.assertIn(new_key, next_keys)

    def test_message_lists_each_new_opening_not_all_times_grouped(self):
        message = _format_message([
            make_slot(time="19:50", label="07:50 PM", offer_id="offer-a"),
            make_slot(time="20:55", label="08:55 PM", offer_id="offer-b"),
        ])
        self.assertIn("2 new openings", message)
        self.assertIn("New opening: 'Ohana", message)
        self.assertIn("2026-07-01 at 07:50 PM", message)
        self.assertIn("2026-07-01 at 08:55 PM", message)
        self.assertIn("Reply STOP to opt out. Reply HELP for help.", message)
        self.assertNotIn("Times:", message)

    def test_booking_url_uses_restaurant_slug_with_no_query_params(self):
        # Disney's SPA does not honor date/time/partySize/offerId on a cold
        # URL load, so we link to the canonical restaurant page (same URL the
        # bot itself navigates to before polling) and rely on the message body
        # to carry the slot details.
        url = booking_url(make_slot(offer_id="abc 123", time="19:50"))
        self.assertEqual(url, "https://disneyworld.disney.go.com/dine-res/restaurant/ohana/")
        self.assertNotIn("offerId", url)
        self.assertNotIn("?", url)

    def test_booking_url_falls_back_to_facility_id_when_slug_missing(self):
        url = booking_url(make_slot(slug=""))
        self.assertEqual(url, "https://disneyworld.disney.go.com/dine-res/restaurant/90002606/")

    def test_message_lists_each_opening_distinctly(self):
        message = _format_message([
            make_slot(time="19:50", label="07:50 PM", offer_id="offer-a"),
            make_slot(time="20:55", label="08:55 PM", offer_id="offer-b"),
        ])
        # Two openings → two "Book:" lines and two distinct time labels in the
        # body, even though the URL is the same per restaurant per day.
        self.assertEqual(message.count("Book:"), 2)
        self.assertIn("07:50 PM", message)
        self.assertIn("08:55 PM", message)

    def test_watch_dates_expire_after_park_date_passes(self):
        self.assertFalse(watch_store.is_active_watch({"date": "2026-05-03"}, today="2026-05-04"))
        self.assertTrue(watch_store.is_active_watch({"date": "2026-05-04"}, today="2026-05-04"))
        self.assertTrue(watch_store.is_active_watch({"date": "2026-05-05"}, today="2026-05-04"))

    def test_disney_cookie_token_valid_after_more_than_24_hours(self):
        token = make_jwt(int(time.time()) + 172800)
        bearer = monitor._bearer_from_cookie_value(make_token_blob(token))
        self.assertEqual(bearer, f"BEARER {token}")

    def test_disney_cookie_token_with_expired_access_and_refresh_is_refreshable(self):
        token = make_jwt(int(time.time()) - 60)
        state = monitor._token_state_from_cookie_value(make_token_blob(token, "refresh-token"))
        self.assertEqual(state.access_token, token)
        self.assertEqual(state.refresh_token, "refresh-token")
        self.assertLess(state.expires_at, time.time())

    def test_disney_cookie_token_accepts_signed_base64url_wrapper(self):
        token = make_jwt(int(time.time()) + 172800)
        state = monitor._token_state_from_cookie_value(make_signed_token_blob(token, "refresh-token"))
        self.assertEqual(state.access_token, token)
        self.assertEqual(state.refresh_token, "refresh-token")
        self.assertGreater(state.expires_at, time.time())

    def test_disney_auth_errors_are_categorized(self):
        self.assertEqual(disney_bot._error_category(monitor.DisneyAuthRequired("login required")), "disney_auth")
        self.assertEqual(disney_bot._error_category("No Disney auth token found in Playwright profile"), "disney_auth")

    # ── session-failure alert grace window ──────────────────────────────────

    def test_consecutive_auth_failures_increments_and_resets(self):
        self.assertEqual(disney_bot._next_consecutive_auth_failures(0, True), 1)
        self.assertEqual(disney_bot._next_consecutive_auth_failures(2, True), 3)
        # Any clean poll resets the streak.
        self.assertEqual(disney_bot._next_consecutive_auth_failures(5, False), 0)
        # Missing / malformed prior state is treated as 0.
        self.assertEqual(disney_bot._next_consecutive_auth_failures(None, True), 1)
        self.assertEqual(disney_bot._next_consecutive_auth_failures("bad", True), 1)

    def test_no_alert_during_grace_window(self):
        now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(disney_bot._should_send_session_alert(1, None, now))
        self.assertFalse(disney_bot._should_send_session_alert(2, None, now))

    def test_alert_fires_at_threshold(self):
        now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(disney_bot._should_send_session_alert(3, None, now))

    def test_recovery_failure_alone_does_not_alert(self):
        # The core bug fix: alerting is driven by the consecutive-failure count,
        # not by recovery returning False. A single failed poll must stay silent.
        now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(disney_bot._should_send_session_alert(1, None, now))

    def test_no_realert_within_6h(self):
        now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
        last_alert = (now - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
        self.assertFalse(disney_bot._should_send_session_alert(4, last_alert, now))

    def test_realert_after_6h(self):
        now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
        last_alert = (now - timedelta(hours=7)).isoformat().replace("+00:00", "Z")
        self.assertTrue(disney_bot._should_send_session_alert(4, last_alert, now))

    def test_clean_poll_rearms_alerting(self):
        # Option B: a clean poll resets the streak to 0 AND the caller clears
        # last_alert_at. A fresh outage then reaching threshold pages again even
        # though the wall-clock is well within the prior alert's 6h cooldown.
        now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(disney_bot._next_consecutive_auth_failures(4, False), 0)
        self.assertTrue(disney_bot._should_send_session_alert(3, None, now))

    def test_naive_now_is_handled(self):
        # poll() passes datetime.now(timezone.utc) (aware), but guard against a
        # naive `now` so the timedelta comparison never raises.
        now = datetime(2026, 6, 22, 12, 0)
        last_alert = "2026-06-22T04:00:00Z"
        self.assertTrue(disney_bot._should_send_session_alert(3, last_alert, now))


def make_sa_slot(**overrides):
    data = {
        "restaurant_name": "Harmony Barber Shop",
        "facility_id": "15437454",
        "slug": "booking-harmony-barber-shop",
        "date": "2026-07-06",
        "time": "11:40",
        "label": "11:40 AM",
        "meal_period": "MORNING",
        "party_size": 2,
        "offer_id": "priceSummary0",
        "owner_id": "craig",
        "watch_id": "watch_sa1",
        "recipient_phone": "+15555550123",
        "booking_type": "scheduled_activity",
    }
    data.update(overrides)
    return Slot(**data)


def make_sa_offers_data(offers_by_date):
    return {"bookableExperience": {"availableOffers": {"offers": offers_by_date}}}


class ScheduledActivityTest(unittest.TestCase):
    def _parse(self, offers_by_date, **overrides):
        kwargs = {
            "data": make_sa_offers_data(offers_by_date),
            "facility_id": "15437454",
            "slug": "booking-harmony-barber-shop",
            "restaurant_name": "Harmony Barber Shop",
            "dates": ["2026-07-06", "2026-07-07"],
            "party_size": 2,
            "meal_periods": ["ALL"],
        }
        kwargs.update(overrides)
        return monitor._parse_sa_offers_response(**kwargs)

    def test_offer_parsing_maps_timeperiod_and_skips_unavailable(self):
        slots = self._parse({
            "2026-07-06": [
                {"startTime": "11:40:00-04:00", "timePeriod": "Morning", "priceId": "priceSummary0"},
            ],
            "2026-07-07": [
                {"date": "2026-07-07", "status": "UNAVAILABLE"},
            ],
        })
        self.assertEqual(len(slots), 1)
        slot = slots[0]
        self.assertEqual(slot.date, "2026-07-06")
        self.assertEqual(slot.time, "11:40")
        self.assertEqual(slot.label, "11:40 AM")
        self.assertEqual(slot.meal_period, "MORNING")
        self.assertEqual(slot.offer_id, "priceSummary0")
        self.assertEqual(slot.booking_type, "scheduled_activity")

    def test_offer_with_missing_timeperiod_falls_back_to_unknown(self):
        slots = self._parse({
            "2026-07-06": [{"startTime": "14:00:00-04:00"}],
        })
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].meal_period, "UNKNOWN")
        self.assertEqual(slots[0].label, "2:00 PM")

    def test_offer_outside_requested_dates_is_ignored(self):
        slots = self._parse({
            "2026-08-01": [{"startTime": "11:40:00-04:00", "timePeriod": "Morning"}],
        })
        self.assertEqual(slots, [])

    def test_time_window_filters_offers(self):
        offers = {
            "2026-07-06": [
                {"startTime": "09:20:00-04:00", "timePeriod": "Morning"},
                {"startTime": "15:10:00-04:00", "timePeriod": "Afternoon"},
            ],
        }
        slots = self._parse(offers, time_from="10:00", time_to="16:00")
        self.assertEqual([s.time for s in slots], ["15:10"])

    def test_sa_slot_key_is_stable_and_alert_semantics_apply(self):
        slot = make_sa_slot()
        key = disney_bot._slot_key(slot)
        self.assertIn("MORNING", key)
        self.assertEqual(disney_bot.filter_new([slot], previous_open_keys={key}), [])
        self.assertEqual(disney_bot.filter_new([slot], previous_open_keys=set()), [slot])

    def test_sa_date_windows_chunks_to_nine_days(self):
        dates = [f"2026-07-{d:02d}" for d in range(1, 13)]
        self.assertEqual(
            monitor._sa_date_windows(dates),
            [("2026-07-01", "2026-07-09"), ("2026-07-10", "2026-07-12")],
        )

    def test_sa_date_windows_packs_sparse_dates(self):
        self.assertEqual(
            monitor._sa_date_windows(["2026-07-01", "2026-07-20"]),
            [("2026-07-01", "2026-07-01"), ("2026-07-20", "2026-07-20")],
        )
        self.assertEqual(
            monitor._sa_date_windows(["2026-07-03"]),
            [("2026-07-03", "2026-07-03")],
        )

    def test_sa_scheduled_dates_clamps_to_bookable_and_scheduled(self):
        details = {
            "status": 200,
            "data": {
                "bookableStartDate": "2026-07-06",
                "bookableEndDate": "2026-09-03",
                "bookableExperience": {"product": {
                    "availableDaysDateRange": 9,
                    "schedules": [
                        {"date": "2026-07-06", "status": "SCHEDULED"},
                        {"date": "2026-07-08", "status": "SCHEDULED"},
                    ],
                }},
            },
        }
        dates, window = monitor._sa_scheduled_dates(
            details, ["2026-07-05", "2026-07-06", "2026-07-07", "2026-07-08"], "Harmony"
        )
        self.assertEqual(dates, ["2026-07-06", "2026-07-08"])
        self.assertEqual(window, 9)

    def test_sa_scheduled_dates_raises_on_401(self):
        with self.assertRaises(RuntimeError) as ctx:
            monitor._sa_scheduled_dates({"status": 401}, ["2026-07-06"], "Harmony")
        self.assertEqual(disney_bot._error_category(ctx.exception), "disney_auth")

    def test_booking_url_uses_enchanting_extras_link(self):
        url = booking_url(make_sa_slot())
        self.assertEqual(
            url,
            "https://disneyworld.disney.go.com/enchanting-extras-collection/booking-harmony-barber-shop/",
        )

    def test_dining_booking_url_unchanged(self):
        self.assertEqual(booking_url(make_slot()), "https://disneyworld.disney.go.com/dine-res/restaurant/ohana/")

    def test_grouped_requests_split_by_booking_type(self):
        dining = {
            "watch_id": "w1", "owner_id": "craig", "facility_id": "15437454",
            "date": "2026-07-06", "party_size": 2,
        }
        sa = dict(dining, watch_id="w2", booking_type="scheduled_activity")
        groups = watch_store.grouped_restaurant_requests([dining, sa])
        self.assertEqual(len(groups), 2)
        types = sorted(g.get("booking_type") for g in groups)
        self.assertEqual(types, ["dining", "scheduled_activity"])


class OwnerScopedDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.original_get_rows = supabase_pipe._get_rows
        self.original_entity = supabase_pipe._entity_for_reference
        self.original_enabled = supabase_pipe.is_supabase_enabled
        self.original_dispatch = notify._dispatch_with_receipt
        self.original_record = getattr(supabase_pipe, "record_delivery_receipts")
        self.original_sync = supabase_pipe.sync_plans_from_supabase
        self.original_storage_read_json = watch_store.storage.read_json

    def tearDown(self):
        supabase_pipe._get_rows = self.original_get_rows
        supabase_pipe._entity_for_reference = self.original_entity
        supabase_pipe.is_supabase_enabled = self.original_enabled
        notify._dispatch_with_receipt = self.original_dispatch
        supabase_pipe.record_delivery_receipts = self.original_record
        supabase_pipe.sync_plans_from_supabase = self.original_sync
        watch_store.storage.read_json = self.original_storage_read_json

    def test_supabase_sync_preserves_two_users_watch_ids_and_recipients(self):
        watches = [
            {
                "id": "watch_alice", "user_id": "alice", "entity_id": "entity_a",
                "threshold_json": {"kind": "ADR", "date": "2026-07-01", "party_size": 2},
            },
            {
                "id": "watch_bob", "user_id": "bob", "entity_id": "entity_a",
                "threshold_json": {"kind": "ADR", "date": "2026-07-01", "party_size": 2},
            },
        ]
        phones = {"alice": "+15550000001", "bob": "+15550000002"}

        def fake_get_rows(table, params):
            if table == "watches":
                return watches
            if table == "users":
                user_id = params["id"].removeprefix("eq.")
                return [{"id": user_id, "phone_e164": phones[user_id]}]
            return []

        supabase_pipe._get_rows = fake_get_rows
        supabase_pipe._entity_for_reference = lambda ref, name="": {
            "id": "entity_a", "external_id": "90002606", "name": "'Ohana",
            "booking_url": "https://disneyworld.disney.go.com/dine-res/restaurant/ohana/",
        }
        supabase_pipe.is_supabase_enabled = lambda: True

        targets = supabase_pipe.sync_plans_from_supabase()
        self.assertEqual(
            {(t["owner_id"], t["watch_id"], t["recipient_phone"]) for t in targets},
            {
                ("alice", "watch_alice", "+15550000001"),
                ("bob", "watch_bob", "+15550000002"),
            },
        )

        raw_slot = make_slot(owner_id="", watch_id="", recipient_phone="")
        watches_for_match = [
            {
                "owner_id": t["owner_id"], "watch_id": t["watch_id"],
                "recipient_phone": t["recipient_phone"], "facility_id": t["facility_id"],
                "date": t["dates"][0], "party_size": t["party_size"],
                "meal_periods": t["meal_periods"], "slug": t["slug"],
            }
            for t in targets
        ]
        matched = disney_bot._matching_owner_slots([raw_slot], watches_for_match)
        self.assertEqual(
            {(s.owner_id, s.watch_id, s.recipient_phone) for s in matched},
            {
                ("alice", "watch_alice", "+15550000001"),
                ("bob", "watch_bob", "+15550000002"),
            },
        )

    def test_supabase_sync_refuses_missing_recipient_without_default_attribution(self):
        supabase_pipe.is_supabase_enabled = lambda: True
        supabase_pipe._get_rows = lambda table, params: ([{
            "id": "watch_orphan", "user_id": "orphan", "entity_id": "entity_a",
            "threshold_json": {"kind": "ADR", "date": "2026-07-01"},
        }] if table == "watches" else [])
        supabase_pipe._entity_for_reference = lambda ref, name="": {
            "id": "entity_a", "external_id": "90002606", "name": "'Ohana", "booking_url": "",
        }
        self.assertEqual(supabase_pipe.sync_plans_from_supabase(), [])

    def test_malformed_watch_does_not_block_valid_owner_sync(self):
        watches = [
            {
                "id": "watch_bad", "user_id": "bad", "entity_id": "entity_a",
                "threshold_json": {"kind": "ADR", "date": "2026-07-01", "party_size": "many"},
            },
            {
                "id": "watch_alice", "user_id": "alice", "entity_id": "entity_a",
                "threshold_json": {"kind": "ADR", "date": "2026-07-01", "party_size": 2},
            },
        ]
        supabase_pipe.is_supabase_enabled = lambda: True
        supabase_pipe._get_rows = lambda table, params: (
            watches if table == "watches" else [{"id": "alice", "phone_e164": "+15550000001"}]
        )
        supabase_pipe._entity_for_reference = lambda ref, name="": {
            "id": "entity_a", "external_id": "90002606", "name": "'Ohana", "booking_url": "",
        }
        targets = supabase_pipe.sync_plans_from_supabase()
        self.assertEqual([(target["owner_id"], target["watch_id"]) for target in targets], [
            ("alice", "watch_alice"),
        ])

    def test_owner_filtered_load_never_reintroduces_other_supabase_users(self):
        watch_store.storage.read_json = lambda filename: []
        supabase_pipe.is_supabase_enabled = lambda: True
        supabase_pipe.sync_plans_from_supabase = lambda: [
            {
                "watch_id": "watch_alice", "owner_id": "alice", "recipient_phone": "+15550000001",
                "facility_id": "90002606", "name": "'Ohana", "slug": "ohana", "party_size": 2,
                "meal_periods": ["DINNER"], "dates": ["2099-07-01"],
            },
            {
                "watch_id": "watch_bob", "owner_id": "bob", "recipient_phone": "+15550000002",
                "facility_id": "90002606", "name": "'Ohana", "slug": "ohana", "party_size": 2,
                "meal_periods": ["DINNER"], "dates": ["2099-07-01"],
            },
        ]
        watches = watch_store.load_watches(owner_id="alice", include_expired=True)
        self.assertEqual([(watch["owner_id"], watch["watch_id"]) for watch in watches], [
            ("alice", "watch_alice"),
        ])

    def test_failed_send_is_not_sent_or_recorded(self):
        calls = []
        notify._dispatch_with_receipt = lambda recipient, body: DeliveryReceipt(
            recipient, "twilio", "failed", error="Twilio rejected"
        )
        supabase_pipe.is_supabase_enabled = lambda: True
        supabase_pipe.record_delivery_receipts = lambda *args: calls.append(args)

        result = notify.send_sms([make_slot(recipient_phone="+15550000001")])
        self.assertEqual(result.sent_slots, [])
        self.assertEqual(result.receipts, [])
        self.assertEqual(calls, [])
        self.assertIn("Twilio rejected", result.errors)

    def test_missing_recipient_is_visible_and_never_falls_back(self):
        dispatched = []
        notify._dispatch_with_receipt = lambda recipient, body: (
            dispatched.append(recipient)
            or DeliveryReceipt(recipient, "twilio", "queued", message_id="SM123")
        )
        supabase_pipe.is_supabase_enabled = lambda: False
        result = notify.send_sms([
            make_slot(owner_id="alice", watch_id="watch_alice", recipient_phone="+15550000001"),
            make_slot(owner_id="bob", watch_id="watch_bob", recipient_phone="", time="20:55"),
        ])
        self.assertEqual(dispatched, ["+15550000001"])
        self.assertEqual([slot.watch_id for slot in result.sent_slots], ["watch_alice"])
        self.assertIn("No recipient phone configured for watch watch_bob", result.errors)

    def test_terminal_provider_failure_without_exception_remains_retryable(self):
        recorded = []
        notify._dispatch_with_receipt = lambda recipient, body: DeliveryReceipt(
            recipient, "twilio", "undelivered", message_id="SM_failed"
        )
        supabase_pipe.is_supabase_enabled = lambda: True
        supabase_pipe.record_delivery_receipts = lambda *args: recorded.append(args)
        result = notify.send_sms([make_slot(recipient_phone="+15550000001")])
        self.assertEqual(result.sent_slots, [])
        self.assertEqual(result.receipts, [])
        self.assertEqual(recorded, [])
        self.assertIn("twilio reported undelivered", result.errors[0])

    def test_success_records_exact_watch_and_provider_metadata(self):
        posted = []
        patched = []

        class Response:
            status_code = 201
            text = ""

            def json(self):
                return [{"id": 99}]

        supabase_pipe.is_supabase_enabled = lambda: True
        supabase_pipe._get_rows = lambda table, params: []
        original_post = supabase_pipe.requests.post
        original_patch = supabase_pipe.requests.patch
        supabase_pipe.requests.post = lambda *args, **kwargs: (posted.append(kwargs["json"]) or Response())
        supabase_pipe.requests.patch = lambda *args, **kwargs: (
            patched.append(kwargs["json"])
            or type("PatchResponse", (), {"status_code": 200, "text": ""})()
        )
        try:
            receipt = DeliveryReceipt("+15550000001", "twilio", "queued", message_id="SM123")
            record_result = supabase_pipe.record_delivery_receipts(
                [make_slot(owner_id="alice", watch_id="watch_alice", recipient_phone="+15550000001")],
                booking_url,
                receipt,
            )
        finally:
            supabase_pipe.requests.post = original_post
            supabase_pipe.requests.patch = original_patch

        self.assertEqual(record_result.inserted, 1)
        self.assertEqual(record_result.delivery_records, 1)
        self.assertEqual(record_result.errors, [])
        delivery_row = posted[0]
        alert_row = posted[1]
        self.assertEqual(alert_row["watch_id"], "watch_alice")
        self.assertEqual(alert_row["payload_json"]["delivery"], {
            "provider": "twilio", "messageSid": "SM123", "providerStatus": "queued",
        })
        self.assertEqual(delivery_row["user_id"], "alice")
        self.assertEqual(delivery_row["watch_id"], "watch_alice")
        self.assertIsNone(delivery_row["alert_id"])
        self.assertEqual(patched, [{"alert_id": 99}])
        self.assertEqual(delivery_row["provider_message_id"], "SM123")
        self.assertEqual(delivery_row["destination_last4"], "0001")
        self.assertNotIn("+15550000001", json.dumps(delivery_row))

    def test_delivery_receipt_dedupes_same_sid_but_records_new_sid(self):
        posts = []
        supabase_pipe.is_supabase_enabled = lambda: True
        supabase_pipe._get_rows = lambda table, params: (
            [{"id": 42}]
            if table == "alerts" and params.get("payload_json->delivery->>messageSid") == "eq.SM123"
            else []
        )

        class Response:
            status_code = 201
            text = ""

        original_post = supabase_pipe.requests.post
        original_patch = supabase_pipe.requests.patch
        supabase_pipe.requests.post = lambda *args, **kwargs: (posts.append(kwargs) or Response())
        supabase_pipe.requests.patch = lambda *args, **kwargs: type(
            "PatchResponse", (), {"status_code": 200, "text": ""}
        )()
        try:
            duplicate_result = supabase_pipe.record_delivery_receipts(
                [make_slot(owner_id="alice", watch_id="watch_alice", recipient_phone="+15550000001")],
                booking_url,
                DeliveryReceipt("+15550000001", "twilio", "queued", message_id="SM123"),
            )
            reopened_result = supabase_pipe.record_delivery_receipts(
                [make_slot(owner_id="alice", watch_id="watch_alice", recipient_phone="+15550000001")],
                booking_url,
                DeliveryReceipt("+15550000001", "twilio", "queued", message_id="SM456"),
            )
        finally:
            supabase_pipe.requests.post = original_post
            supabase_pipe.requests.patch = original_patch
        self.assertEqual(duplicate_result.duplicates, 1)
        self.assertEqual(duplicate_result.inserted, 0)
        self.assertEqual(reopened_result.inserted, 1)

    def test_twilio_send_sets_configured_status_callback(self):
        captured = []

        class Messages:
            def create(self, **kwargs):
                captured.append(kwargs)
                return type("Message", (), {"sid": "SM123", "status": "queued"})()

        fake_client = type("Client", (), {"messages": Messages()})()
        original_twilio = notify._twilio
        notify._twilio = lambda: fake_client
        try:
            with patch.dict(os.environ, {
                "TWILIO_MESSAGING_SERVICE_SID": "MG123",
                "TWILIO_STATUS_CALLBACK_URL": "https://disscan.com/api/webhooks/twilio/status",
            }):
                receipt = notify._send_via_twilio("+15550000001", "hello", "sms")
        finally:
            notify._twilio = original_twilio
        self.assertIsNone(receipt.error)
        self.assertEqual(captured[0]["status_callback"], "https://disscan.com/api/webhooks/twilio/status")

    def test_twilio_send_omits_status_callback_when_unconfigured(self):
        captured = []

        class Messages:
            def create(self, **kwargs):
                captured.append(kwargs)
                return type("Message", (), {"sid": "SM123", "status": "queued"})()

        fake_client = type("Client", (), {"messages": Messages()})()
        original_twilio = notify._twilio
        notify._twilio = lambda: fake_client
        try:
            with patch.dict(os.environ, {
                "TWILIO_MESSAGING_SERVICE_SID": "MG123",
                "TWILIO_STATUS_CALLBACK_URL": "",
            }):
                receipt = notify._send_via_twilio("+15550000001", "hello", "sms")
        finally:
            notify._twilio = original_twilio
        self.assertIsNone(receipt.error)
        self.assertNotIn("status_callback", captured[0])

    def test_receipt_persistence_failure_is_visible_without_resending_sms(self):
        notify._dispatch_with_receipt = lambda recipient, body: DeliveryReceipt(
            recipient, "twilio", "queued", message_id="SM123"
        )
        supabase_pipe.is_supabase_enabled = lambda: True
        supabase_pipe.record_delivery_receipts = lambda *args: supabase_pipe.ReceiptRecordResult(
            errors=["watch watch_abc receipt insert returned 500"]
        )
        result = notify.send_sms([make_slot(recipient_phone="+15550000001")])
        self.assertEqual([slot.watch_id for slot in result.sent_slots], ["watch_abc"])
        self.assertIn("Delivery receipt persistence failed", result.errors[0])

    def test_worker_receipt_retry_cannot_downgrade_existing_terminal_status(self):
        calls = []

        class Response:
            status_code = 201
            text = ""

            def json(self):
                return []

        supabase_pipe.is_supabase_enabled = lambda: True
        supabase_pipe._get_rows = lambda table, params: [{"id": 99}] if table == "alerts" else []
        original_post = supabase_pipe.requests.post
        original_patch = supabase_pipe.requests.patch
        supabase_pipe.requests.post = lambda *args, **kwargs: (calls.append((args, kwargs)) or Response())
        supabase_pipe.requests.patch = lambda *args, **kwargs: type(
            "PatchResponse", (), {"status_code": 200, "text": ""}
        )()
        try:
            result = supabase_pipe.record_delivery_receipts(
                [make_slot(owner_id="alice", watch_id="watch_alice", recipient_phone="+15550000001")],
                booking_url,
                DeliveryReceipt("+15550000001", "twilio", "queued", message_id="SM123"),
            )
        finally:
            supabase_pipe.requests.post = original_post
            supabase_pipe.requests.patch = original_patch
        self.assertEqual(result.delivery_records, 1)
        self.assertEqual(len(calls), 1)
        _, delivery_kwargs = calls[0]
        self.assertIn("resolution=ignore-duplicates", delivery_kwargs["headers"]["Prefer"])
        self.assertEqual(delivery_kwargs["json"]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
