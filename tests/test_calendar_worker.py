import copy
import hashlib
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

import calendar_worker
import disney_bot
import monitor


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def request_job(**changes):
    job = {
        "id": "11111111-1111-4111-8111-111111111111",
        "entity_id": "22222222-2222-4222-8222-222222222222",
        "facility_id": "19433706", "restaurant_name": "Space 220 Restaurant",
        "slug": "space-220", "destination": "WDW", "party_size": 4,
        "start_date": "2026-11-06", "end_date": "2026-11-08",
        "status": "queued", "attempt_count": 0,
    }
    return {**job, **changes}


def valid_response(restaurants=None):
    if restaurants is None:
        restaurants = {
            "2026-11-07": [{
                "mealPeriodType": "Dinner",
                "offersByAccessibility": [{"offers": [{
                    "time": "18:30:00", "label": "6:30 PM", "offerId": "private-offer-token",
                }]}],
            }],
        }
    return {"status": 200, "data": {"statusCode": 200, "restaurants": restaurants}, "request_id": "provider-request-1"}


class FakeDatabase:
    def __init__(self, job=None, claimed=True):
        self.job = request_job() if job is None else job
        self.calls = []
        self.claimed = claimed

    def __call__(self, method, table, *, params=None, payload=None):
        self.calls.append(copy.deepcopy((method, table, params, payload)))
        if method == "GET":
            if not self.job:
                return []
            if params.get("claim_token") and params["claim_token"] != f"eq.{self.job.get('claim_token')}":
                return []
            if params.get("status") and params["status"] != f"eq.{self.job.get('status')}":
                return []
            return [copy.deepcopy(self.job)]
        if table == "dining_calendar_requests" and payload.get("status") == "processing":
            if not self.claimed:
                return []
            self.job = {**self.job, **payload}
            return [copy.deepcopy(self.job)]
        return [copy.deepcopy(payload)] if isinstance(payload, dict) else copy.deepcopy(payload)

    def posted(self, table):
        return [body for method, name, params, body in self.calls if method == "POST" and name == table]

    def patched(self, table):
        return [body for method, name, params, body in self.calls if method == "PATCH" and name == table]


class CalendarWorkerTest(unittest.TestCase):
    def setUp(self):
        self.patchers = [
            patch.object(calendar_worker, "_database_config", return_value=("https://database.test", "mock-key")),
            patch.object(calendar_worker, "datetime", FixedDateTime),
            patch.object(calendar_worker, "_max_bookable_date", return_value=date(2027, 1, 5)),
            patch.object(calendar_worker, "_worker_commit", return_value="a" * 40),
            patch.object(calendar_worker, "_rpc", return_value=True),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_job(self, result=None, job=None, db=None, error=None):
        database = db or FakeDatabase(job)
        provider = Mock(return_value=result or valid_response(), side_effect=error)
        with patch.object(calendar_worker, "_db", side_effect=database), \
             patch.object(calendar_worker, "fetch_calendar_availability_via_playwright", provider):
            outcome = calendar_worker.process_pending_calendar_request()
        return outcome, database, provider

    def test_complete_receipt_covers_empty_dates_and_exact_party_range(self):
        outcome, db, provider = self.run_job()
        self.assertEqual(outcome["status"], "complete")
        provider.assert_called_once_with(facility_id="19433706", slug="space-220", party_size=4,
                                         start_date="2026-11-06", end_date="2026-11-08")
        days = db.posted("dining_calendar_days")[0]
        self.assertEqual([day["date"] for day in days], ["2026-11-06", "2026-11-07", "2026-11-08"])
        self.assertTrue(all(day["coverage_status"] == "complete" for day in days))
        self.assertEqual(db.patched("dining_calendar_requests")[-1]["status"], "complete")
        scan = db.posted("dining_calendar_scans")[0]
        self.assertEqual(scan["status"], "failed")  # Not published before child rows persist.
        self.assertEqual(scan["provider_status"], 200)
        self.assertEqual(scan["provider_request_id"], "provider-request-1")
        self.assertEqual(scan["worker_commit"], "a" * 40)
        self.assertTrue(scan["observed_at"])
        tables = {table for method, table, params, payload in db.calls}
        self.assertEqual(tables, {"dining_calendar_requests", "dining_calendar_scans", "dining_calendar_days", "dining_calendar_slots"})

    def test_offer_references_are_hashed_and_times_normalized(self):
        outcome, db, provider = self.run_job()
        slot = db.posted("dining_calendar_slots")[0][0]
        self.assertEqual(slot["time"], "18:30")
        self.assertEqual(slot["meal_period"], "DINNER")
        self.assertEqual(slot["offer_ref_hash"], hashlib.sha256(b"private-offer-token").hexdigest())
        self.assertNotIn("private-offer-token", repr(db.calls))

    def test_valid_empty_response_is_complete_with_no_slots(self):
        outcome, db, provider = self.run_job(valid_response({}))
        self.assertEqual(outcome["status"], "complete")
        self.assertEqual(db.posted("dining_calendar_slots"), [])
        self.assertTrue(all(day["coverage_status"] == "complete" for day in db.posted("dining_calendar_days")[0]))

    def test_http_errors_and_malformed_payloads_never_certify_empty_days(self):
        invalid = [
            {"status": 404, "data": None}, {"status": 403, "data": None},
            {"status": 200, "data": None}, {"status": 200, "data": {}},
            {"status": 200, "data": {"restaurants": {}}},
            {"status": 200, "data": {"statusCode": 404, "restaurants": {}}},
            {"status": 200, "data": {"restaurants": []}},
            valid_response({"2026-11-07": {}}),
            valid_response({"2026-11-07": [{"mealPeriodType": "Dinner"}]}),
            valid_response({"2026-11-07": [{"mealPeriodType": "Dinner", "offersByAccessibility": [{}]}]}),
            valid_response({"2026-11-07": [{"mealPeriodType": "Dinner", "offersByAccessibility": [{"offers": [{"time": "25:30"}]}]}]}),
            valid_response({"2026-11-09": []}),
        ]
        for result in invalid:
            with self.subTest(result=result):
                outcome, db, provider = self.run_job(result)
                self.assertEqual(outcome["status"], "failed")
                self.assertTrue(all(day["coverage_status"] == "unknown" for day in db.posted("dining_calendar_days")[0]))
                self.assertEqual(db.posted("dining_calendar_slots"), [])
                self.assertTrue(all(change["status"] == "failed" for change in db.patched("dining_calendar_scans")))

    def test_provider_exception_is_sanitized_and_failed(self):
        outcome, db, provider = self.run_job(error=RuntimeError("secret-auth-value"))
        self.assertEqual(outcome["status"], "failed")
        self.assertNotIn("secret-auth-value", repr(db.calls))
        self.assertEqual(db.patched("dining_calendar_requests")[-1]["status"], "failed")

    def test_atomic_claim_limits_one_job_and_losing_worker_skips_provider(self):
        outcome, db, provider = self.run_job(db=FakeDatabase(claimed=False))
        self.assertEqual(outcome["processed"], 0)
        provider.assert_not_called()
        self.assertEqual(db.calls[0][2]["limit"], "1")
        self.assertEqual(db.calls[1][2]["status"], "eq.queued")
        self.assertEqual(db.calls[1][3]["attempt_count"], 1)

    def test_replaced_claim_cannot_publish_or_complete(self):
        db = FakeDatabase()
        original = db.__call__
        ownership_checks = 0
        def replaced(method, table, **kwargs):
            nonlocal ownership_checks
            params = kwargs.get("params") or {}
            if method == "GET" and params.get("claim_token"):
                ownership_checks += 1
                return []
            return original(method, table, **kwargs)
        with patch.object(calendar_worker, "_db", side_effect=replaced), \
             patch.object(calendar_worker, "fetch_calendar_availability_via_playwright", return_value=valid_response()):
            outcome = calendar_worker.process_pending_calendar_request()
        self.assertEqual(outcome["status"], "claim_lost")
        self.assertGreaterEqual(ownership_checks, 1)
        self.assertEqual(db.posted("dining_calendar_scans"), [])

    def test_atomic_publication_rejects_claim_lost_after_child_persistence(self):
        db = FakeDatabase()
        with patch.object(calendar_worker, "_db", side_effect=db), \
             patch.object(calendar_worker, "_rpc", return_value=False), \
             patch.object(calendar_worker, "fetch_calendar_availability_via_playwright", return_value=valid_response()):
            outcome = calendar_worker.process_pending_calendar_request()
        self.assertEqual(outcome["status"], "claim_lost")
        self.assertEqual(db.posted("dining_calendar_scans")[0]["status"], "failed")
        self.assertFalse(any(change["status"] in ("complete", "partial") for change in db.patched("dining_calendar_scans")))

    def test_partial_booking_window_never_marks_future_dates_complete(self):
        with patch.object(calendar_worker, "_max_bookable_date", return_value=date(2026, 11, 7)):
            outcome, db, provider = self.run_job()
        self.assertEqual(outcome["status"], "partial")
        self.assertEqual(provider.call_args.kwargs["end_date"], "2026-11-07")
        days = db.posted("dining_calendar_days")[0]
        self.assertEqual(days[-1]["coverage_status"], "unknown")

    def test_unsupported_destination_and_invalid_requests_skip_provider(self):
        for changes in [{"destination": "DLR"}, {"party_size": 0}, {"party_size": True},
                        {"start_date": "bad"}, {"end_date": "2027-02-01"},
                        {"slug": "space-220?token=value"}, {"facility_id": "../secret"}]:
            with self.subTest(changes=changes):
                outcome, db, provider = self.run_job(job=request_job(**changes))
                self.assertEqual(outcome["status"], "failed")
                provider.assert_not_called()

    def test_outside_booking_window_is_unknown_without_provider_call(self):
        outcome, db, provider = self.run_job(job=request_job(start_date="2027-02-01", end_date="2027-02-03"))
        self.assertEqual(outcome["error_code"], "outside_booking_window")
        provider.assert_not_called()
        self.assertTrue(all(day["coverage_status"] == "unknown" for day in db.posted("dining_calendar_days")[0]))

    def test_child_persistence_failure_never_publishes_complete_scan(self):
        db = FakeDatabase()
        def failing(method, table, **kwargs):
            if table == "dining_calendar_slots":
                raise calendar_worker.CalendarError("persistence_failed", "Slots could not be persisted")
            return db(method, table, **kwargs)
        with patch.object(calendar_worker, "_db", side_effect=failing), \
             patch.object(calendar_worker, "fetch_calendar_availability_via_playwright", return_value=valid_response()):
            outcome = calendar_worker.process_pending_calendar_request()
        self.assertEqual(outcome["status"], "failed")
        self.assertFalse(any(change["status"] in ("complete", "partial") for change in db.patched("dining_calendar_scans")))

    def test_duplicate_accessibility_offers_become_one_visible_slot(self):
        response = valid_response()
        meal = response["data"]["restaurants"]["2026-11-07"][0]
        meal["offersByAccessibility"].append(copy.deepcopy(meal["offersByAccessibility"][0]))
        slots = calendar_worker.normalize_calendar_response(response, ["2026-11-06", "2026-11-07"])
        self.assertEqual(len(slots), 1)

    def test_all_meal_groups_are_preserved_without_watch_filters(self):
        response = valid_response()
        meals = response["data"]["restaurants"]["2026-11-07"]
        for period, clock in [("Breakfast", "08:00"), ("Lunch", "12:00")]:
            meals.append({"mealPeriodType": period, "offersByAccessibility": [{"offers": [{"time": clock}]}]})
        slots = calendar_worker.normalize_calendar_response(response, ["2026-11-07"])
        self.assertEqual({slot["meal_period"] for slot in slots}, {"BREAKFAST", "LUNCH", "DINNER"})

    def test_calendar_runs_only_after_alert_poll_finishes(self):
        sequence = []
        with patch.object(disney_bot, "poll", side_effect=lambda config: sequence.append("alerts")), \
             patch.object(calendar_worker, "process_pending_calendar_request", side_effect=lambda: sequence.append("calendar") or {"processed": 0, "status": "idle"}):
            disney_bot.poll_cycle({"polling_interval_minutes": 10})
        self.assertEqual(sequence, ["alerts", "calendar"])

    def test_database_error_status_is_fail_closed(self):
        response = Mock(status_code=403)
        with patch.object(calendar_worker.requests, "request", return_value=response):
            with self.assertRaises(calendar_worker.CalendarError):
                calendar_worker._db("GET", "dining_calendar_requests")

    def test_empty_child_insert_receipt_never_publishes_complete_scan(self):
        db = FakeDatabase()
        def incomplete(method, table, **kwargs):
            recorded = db(method, table, **kwargs)
            return [] if table == "dining_calendar_days" and method == "POST" else recorded
        with patch.object(calendar_worker, "_db", side_effect=incomplete), \
             patch.object(calendar_worker, "fetch_calendar_availability_via_playwright", return_value=valid_response()):
            outcome = calendar_worker.process_pending_calendar_request()
        self.assertEqual(outcome["status"], "failed")
        self.assertFalse(any(change["status"] == "complete" for change in db.patched("dining_calendar_scans")))
        self.assertEqual(db.patched("dining_calendar_requests")[-1]["status"], "failed")

    def test_provider_adapter_preserves_raw_failure_and_exact_request_criteria(self):
        page = Mock()
        page.evaluate.return_value = {"status": 404, "data": None, "request_id": None}
        browser = Mock(pages=[page])
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value = browser
        manager = Mock()
        manager.__enter__ = Mock(return_value=playwright)
        manager.__exit__ = Mock(return_value=False)
        with patch("playwright.sync_api.sync_playwright", return_value=manager), \
             patch.object(monitor, "_token_from_playwright_context", return_value="mock-auth"):
            result = monitor.fetch_calendar_availability_via_playwright(
                facility_id="19433706", slug="space-220", party_size=7,
                start_date="2026-11-06", end_date="2026-11-30",
            )
        self.assertEqual(result["status"], 404)
        args = page.evaluate.call_args.args[1]
        self.assertEqual((args["facilityId"], args["partySize"], args["start"], args["end"]),
                         ("19433706", 7, "2026-11-06", "2026-11-30"))
        self.assertEqual(args["fetchTimeoutMs"], 30000)
        browser.close.assert_called_once()

    def test_provider_context_closes_after_auth_failure(self):
        page = Mock()
        browser = Mock(pages=[page])
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value = browser
        manager = Mock()
        manager.__enter__ = Mock(return_value=playwright)
        manager.__exit__ = Mock(return_value=False)
        with patch("playwright.sync_api.sync_playwright", return_value=manager), \
             patch.object(monitor, "_token_from_playwright_context", side_effect=monitor.DisneyAuthRequired("Login required")):
            with self.assertRaises(monitor.DisneyAuthRequired):
                monitor.fetch_calendar_availability_via_playwright(
                    facility_id="19433706", slug="space-220", party_size=4,
                    start_date="2026-11-06", end_date="2026-11-30",
                )
        browser.close.assert_called_once()
        page.evaluate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
