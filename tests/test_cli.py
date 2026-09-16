import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from capacity_guard import cli
from capacity_guard.config import Settings, load_settings, save_settings
from capacity_guard.state import Store
from test_log_source import FailureEvent, THREAD, TURN


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = {**os.environ, "CODEX_HOME": str(self.directory / "codex"), "CODEX_CAPACITY_GUARD_HOME": str(self.directory / "guard")}

    def command(self, *args, input=None):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "capacity_guard.py"), *args], env=self.env, input=input, text=True, capture_output=True, timeout=10)

    def test_config_status_cancel_and_disable(self):
        self.assertEqual(0, self.command("configure", "--initial-delay", "31", "--max-delay", "121").returncode)
        self.assertEqual(31, load_settings(self.directory / "guard").initial_delay)
        status = self.command("status", "--json")
        self.assertEqual(0, status.returncode)
        self.assertFalse(json.loads(status.stdout)["enabled"])
        self.assertEqual(0, self.command("cancel", "--thread", THREAD).returncode)
        self.assertEqual(0, self.command("disable").returncode)

    def test_invalid_settings_and_ids_are_rejected(self):
        self.assertNotEqual(0, self.command("configure", "--initial-delay", "0").returncode)
        self.assertNotEqual(0, self.command("enable", "--thread", "not-an-id").returncode)
        for values in [{"initial_delay": float("nan")}, {"max_delay": float("inf")}, {"poll_interval": float("nan")}]:
            with self.assertRaises(ValueError):
                Settings(**values)

    def test_hook_does_not_cancel_waiting_recovery_after_capacity_failure(self):
        store = Store(self.directory / "guard")
        self.addCleanup(store.close)
        store.schedule(FailureEvent(1, 100, THREAD, TURN, "model", True), 130)
        result = self.command("hook", "UserPromptSubmit", input=json.dumps({"session_id": THREAD, "prompt": ""}))
        self.assertEqual(0, result.returncode)
        self.assertEqual("waiting", store.recovery(THREAD)["status"])

    def test_hook_marks_manual_submission_and_never_restarts_disabled_guard(self):
        store = Store(self.directory / "guard")
        self.addCleanup(store.close)
        store.schedule(FailureEvent(1, 100, THREAD, TURN, "model", True), 130)
        store.update(THREAD, status="sending", token=TURN)
        result = self.command("hook", "UserPromptSubmit", input=json.dumps({"session_id": THREAD, "prompt": "continue"}))
        self.assertEqual(0, result.returncode)
        self.assertEqual({}, json.loads(result.stdout))
        self.assertEqual("cancelled", store.recovery(THREAD)["status"])

    def test_hook_recognizes_only_its_exact_outstanding_message(self):
        from capacity_guard.recovery import recovery_message
        store = Store(self.directory / "guard")
        self.addCleanup(store.close)
        store.schedule(FailureEvent(1, 100, THREAD, TURN, "model", True), 130)
        store.update(THREAD, status="sending", token=TURN)
        self.command("hook", "UserPromptSubmit", input=json.dumps({"session_id": THREAD, "prompt": recovery_message(TURN)}))
        self.assertEqual("sending", store.recovery(THREAD)["status"])
        self.command("hook", "UserPromptSubmit", input=json.dumps({"session_id": THREAD, "prompt": recovery_message(THREAD)}))
        self.assertEqual("cancelled", store.recovery(THREAD)["status"])

    def test_enable_starts_independent_process_disable_stops_it(self):
        import time
        try:
            enabled = self.command("enable", "--thread", THREAD)
            self.assertEqual(0, enabled.returncode, enabled.stderr + enabled.stdout)
            status = json.loads(self.command("status", "--json").stdout)
            self.assertTrue(status["enabled"] and status["running"])
            self.assertEqual([THREAD], status["threads"])
        finally:
            self.command("disable")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = json.loads(self.command("status", "--json").stdout)
            if not status["running"]:
                break
            time.sleep(.05)
        self.assertFalse(status["running"])

    def test_install_registration_preserves_other_plugins_and_is_idempotent(self):
        spec = importlib.util.spec_from_file_location("install_plugin", ROOT / "scripts" / "install_plugin.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        agents = self.directory / "agents"
        marketplace = agents / "plugins" / "marketplace.json"
        marketplace.parent.mkdir(parents=True)
        existing = {"name": "personal", "interface": {"displayName": "Mine"}, "plugins": [{"name": "another"}]}
        marketplace.write_text(json.dumps(existing))
        module.register(ROOT, agents)
        first = marketplace.read_text()
        module.register(ROOT, agents)
        self.assertEqual(first, marketplace.read_text())
        actual = json.loads(first)
        self.assertEqual(existing["plugins"][0], actual["plugins"][0])
        self.assertEqual("Mine", actual["interface"]["displayName"])
        self.assertEqual(ROOT, (agents / "plugins" / "plugins" / "codex-capacity-guard").resolve())
        self.assertEqual(ROOT, (agents.parent / "plugins" / "codex-capacity-guard").resolve())


if __name__ == "__main__":
    unittest.main()
