import os
import json
import subprocess
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import autobook_service


class AutobookSecurityTest(unittest.TestCase):
    def test_api_auth_fails_closed_when_key_is_unconfigured(self):
        original_key = autobook_service.AUTOBOOK_API_KEY
        autobook_service.AUTOBOOK_API_KEY = ""
        try:
            with self.assertRaises(HTTPException) as context:
                autobook_service.verify_api_key(SimpleNamespace(method="POST"))
        finally:
            autobook_service.AUTOBOOK_API_KEY = original_key
        self.assertEqual(context.exception.status_code, 503)

    def test_reseed_fails_closed_before_process_launch_without_credentials(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISNEY_LOGIN_EMAIL", None)
            os.environ.pop("DISNEY_LOGIN_PASSWORD", None)
            with patch("subprocess.Popen") as popen:
                with self.assertRaises(HTTPException) as context:
                    autobook_service.reseed_session()
        self.assertEqual(context.exception.status_code, 503)
        popen.assert_not_called()

    def test_reseed_passes_only_configured_credentials_to_seeder(self):
        env = {
            "DISNEY_LOGIN_EMAIL": "configured@example.invalid",
            "DISNEY_LOGIN_PASSWORD": "configured-test-value",
            "DISNEY_HEADLESS": "false",
        }
        with patch.dict(os.environ, env, clear=False), patch("subprocess.Popen") as popen:
            popen.return_value.pid = 123
            result = autobook_service.reseed_session()
        command = popen.call_args.args[0]
        self.assertNotIn("--email", command)
        self.assertNotIn("--password", command)
        self.assertEqual(popen.call_args.kwargs["env"]["DISNEY_LOGIN_EMAIL"], env["DISNEY_LOGIN_EMAIL"])
        self.assertEqual(popen.call_args.kwargs["env"]["DISNEY_LOGIN_PASSWORD"], env["DISNEY_LOGIN_PASSWORD"])
        self.assertEqual(result["pid"], 123)

    def test_public_health_contains_no_account_or_session_details(self):
        payload = autobook_service.health_check()
        self.assertEqual(set(payload), {"status", "service", "timestamp"})

    def test_missing_protected_roster_fails_closed(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISNEY_FRIENDS_DATA_PATH", None)
            with self.assertRaisesRegex(RuntimeError, "roster is unavailable or incomplete"):
                autobook_service.get_party_roster()

    def test_booking_never_calls_disney_when_roster_is_unavailable(self):
        request = autobook_service.BookingRequest(facility_id="test-facility")
        with patch.object(
            autobook_service,
            "get_party_roster",
            side_effect=RuntimeError("Protected Disney party roster is unavailable or incomplete"),
        ), patch.object(autobook_service, "execute_disney_reserve") as reserve:
            with self.assertRaisesRegex(RuntimeError, "roster is unavailable or incomplete"):
                autobook_service.book_lightning_lane(request)
        reserve.assert_not_called()

    def test_vps_sync_keeps_credentials_out_of_process_arguments(self):
        script_path = Path(__file__).parent.parent / "scripts" / "sync_disney_login_to_vps.py"
        spec = importlib.util.spec_from_file_location("sync_disney_login_to_vps", script_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        completed = subprocess.CompletedProcess([], 0, stdout=b"ok", stderr=b"")
        with patch.object(module.subprocess, "run", return_value=completed) as run:
            module.merge_remote_env(
                "operator@example.invalid",
                "/tmp/test-key",
                "configured@example.invalid",
                "configured-test-value",
            )
        command = run.call_args.args[0]
        rendered = " ".join(command)
        self.assertNotIn("configured@example.invalid", rendered)
        self.assertNotIn("configured-test-value", rendered)
        self.assertIn(b"configured@example.invalid", run.call_args.kwargs["input"])
        self.assertIn(b"configured-test-value", run.call_args.kwargs["input"])

    def test_vps_sync_quotes_dotenv_values_without_injecting_lines(self):
        script_path = Path(__file__).parent.parent / "scripts" / "sync_disney_login_to_vps.py"
        spec = importlib.util.spec_from_file_location("sync_disney_login_to_vps_quoting", script_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        email = "operator+#tag@example.invalid\ncontinued"
        password = "quote\" slash\\ hash# newline\nvalue"
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("PRESERVED=value\nDISNEY_LOGIN_PASSWORD=old\n")
            with patch.object(module, "REMOTE_ENV_PATH", str(env_path)), patch.object(
                module, "ssh_base", return_value=["/bin/sh", "-c"]
            ):
                module.merge_remote_env(
                    "operator@example.invalid",
                    "/tmp/test-key",
                    email,
                    password,
                )
            lines = env_path.read_text().splitlines()
        self.assertEqual(
            lines,
            [
                "PRESERVED=value",
                "DISNEY_LOGIN_EMAIL=" + json.dumps(email.strip()),
                "DISNEY_LOGIN_PASSWORD=" + json.dumps(password),
            ],
        )

    def test_seeder_cli_rejects_missing_credentials_before_browser_start(self):
        clean_env = dict(os.environ)
        clean_env.pop("DISNEY_LOGIN_EMAIL", None)
        clean_env.pop("DISNEY_LOGIN_PASSWORD", None)
        result = subprocess.run(
            [sys.executable, "seed_coach_login.py", "--timeout-sec", "1"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("playwright", (result.stdout + result.stderr).lower())


if __name__ == "__main__":
    unittest.main()
