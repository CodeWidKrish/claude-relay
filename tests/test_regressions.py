"""Regression tests for session isolation, launcher failures, and installation ownership."""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import relay


class RelayRegressions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.project = root / "project"
        self.other = root / "other"
        self.project.mkdir()
        self.other.mkdir()
        state = root / "relay"
        claude = root / "claude"
        paths = patch.multiple(relay, STATE_DIR=state, STATE_FILE=state / "state.json",
                               CONFIG_FILE=state / "config.json", LOG_FILE=state / "relay.log",
                               CLAUDE_DIR=claude, CLAUDE_SETTINGS=claude / "settings.json",
                               CLAUDE_PROJECTS=claude / "projects")
        paths.start()
        self.addCleanup(paths.stop)

    def transcript(self, project, session, text):
        path = relay.project_dir_for(project) / (session + ".jsonl")
        relay.save_text(path, json.dumps({"type": "user", "message": {"content": text}}))
        return path

    def hook(self, project, session):
        return {"error_type": "rate_limit", "cwd": str(project), "session_id": session}

    def fire(self, payload):
        with patch.object(relay, "read_stdin_json", return_value=payload), patch.object(relay, "notify"):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                relay.cmd_on_limit(None)
            return output.getvalue()

    def test_foreign_transcript_is_excluded(self):
        private = self.transcript(self.other, "same", "Private other-project request")
        path = relay.build_handoff(self.project, private, session="same")
        text = path.read_text(encoding="utf-8")
        self.assertIn("Transcript:** unavailable", text)
        self.assertNotIn("Private other-project request", text)

    def test_two_sessions_and_two_projects_switch_independently(self):
        first = self.hook(self.project, "one")
        second = self.hook(self.project, "two")
        third = self.hook(self.other, "one")
        self.transcript(self.project, "one", "First unique task")
        self.transcript(self.project, "two", "Second unique task")
        with patch.object(relay, "open_in_new_terminal", return_value=True) as launcher:
            for payload in (first, second, third, first):
                self.fire(payload)
        self.assertEqual(launcher.call_count, 3)
        self.assertIn("First unique task", relay.checkpoint(self.project, "one").read_text(encoding="utf-8"))
        self.assertIn("Second unique task", relay.checkpoint(self.project, "two").read_text(encoding="utf-8"))

    def test_concurrent_hooks_launch_once(self):
        relay.save_json(relay.CONFIG_FILE, {"mode": "auto", "target_cmd": sys.executable})
        payload = json.dumps(self.hook(self.project, "parallel"))
        env = dict(os.environ, RELAY_HOME=str(relay.STATE_DIR), CLAUDE_CONFIG_DIR=str(relay.CLAUDE_DIR),
                   RELAY_DRY_RUN="0")
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); import relay; "
            "relay.open_in_new_terminal = lambda *args: True; relay.notify = lambda *args: None; "
            "relay.main(['on-limit'])"
        )
        command = [sys.executable, "-c", script, str(Path(relay.__file__).resolve().parent)]
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env) as first:
            with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env) as second:
                first.stdin.write(payload.encode())
                first.stdin.flush()
                other = second.communicate(payload.encode(), timeout=30)[0]
                original = first.communicate(timeout=30)[0]
        self.assertEqual((original + other).count(b"Antigravity launch requested"), 1)
        self.assertEqual((first.returncode, second.returncode), (0, 0))

    def test_failed_launch_can_retry(self):
        payload = self.hook(self.project, "test")
        with patch.object(relay, "open_in_new_terminal", side_effect=[False, True]) as launcher:
            self.assertIn("launch failed", self.fire(payload))
            self.assertIn("launch requested", self.fire(payload))
            self.assertEqual(self.fire(payload), "")
        self.assertEqual(launcher.call_count, 2)

    def test_uninstall_preserves_user_commands_and_settings(self):
        custom = relay.CLAUDE_DIR / "commands" / "handoff.md"
        relay.save_text(custom, "Custom handoff")
        original = {"type": "command", "command": "my-status"}
        unrelated = {"matcher": "rate_limit", "hooks": [{"type": "command", "command": "other/relay.py"}]}
        relay.save_json(relay.CLAUDE_SETTINGS, {"statusLine": original, "hooks": {"StopFailure": [unrelated]}})
        args = argparse.Namespace(mode=None, threshold=None, model=None)
        with contextlib.redirect_stdout(io.StringIO()):
            relay.cmd_install(args)
            relay.cmd_install(args)
            ask = relay.CLAUDE_DIR / "commands" / "ask-antigravity.md"
            ask.write_text("My edited command", encoding="utf-8")
            relay.cmd_uninstall(None)
        self.assertEqual(custom.read_text(encoding="utf-8"), "Custom handoff")
        self.assertEqual(ask.read_text(encoding="utf-8"), "My edited command")
        settings = relay.read_settings()
        self.assertEqual(settings["statusLine"], original)
        self.assertEqual(settings["hooks"]["StopFailure"], [unrelated])

    def test_invalid_settings_leave_config_intact(self):
        relay.save_json(relay.CONFIG_FILE, {"mode": "manual"})
        relay.save_text(relay.CLAUDE_SETTINGS, "{ invalid")
        with self.assertRaises(ValueError):
            relay.cmd_install(argparse.Namespace(mode="auto", threshold=None, model=None))
        self.assertEqual(relay.load_json(relay.CONFIG_FILE, {}), {"mode": "manual"})

    def test_partial_ask_output_preserves_failure(self):
        result = subprocess.CompletedProcess([], 5, "Partial output", "CLI error")
        with patch.object(relay.shutil, "which", return_value="agy.exe"), patch.object(
                relay.subprocess, "run", return_value=result):
            ok, answer = relay.run_antigravity_headless(relay.DEFAULT_CONFIG, "Question", self.project)
        self.assertFalse(ok)
        self.assertIn("Partial output", answer)
        self.assertIn("CLI error", answer)

    def test_back_launch_canonicalizes_workspace_path(self):
        relay.build_handoff(self.project, session="original")
        alternate_path = self.project / ".." / self.project.name
        args = argparse.Namespace(cwd=str(alternate_path), session="original", launch=True, resume=False)
        with patch.object(relay, "open_in_new_terminal", return_value=True) as launcher:
            with contextlib.redirect_stdout(io.StringIO()):
                relay.cmd_back(args)
        command, cwd, _ = launcher.call_args.args
        self.assertEqual(cwd, self.project.resolve())
        self.assertIn(".relay/sessions/", command[-1])

    def test_dry_run_leaves_real_cooldown_available(self):
        payload = self.hook(self.project, "dry")
        relay.save_json(relay.CONFIG_FILE, {"dry_run": True})
        with patch.object(relay, "open_in_new_terminal", return_value=True):
            self.assertIn("launch simulated", self.fire(payload))
            relay.save_json(relay.CONFIG_FILE, {"dry_run": False})
            self.assertIn("launch requested", self.fire(payload))

    def test_simulate_does_not_touch_project_or_account_state(self):
        relay.save_json(relay.STATE_FILE, {"five_hour_pct": 42})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            relay.cmd_simulate(argparse.Namespace(cwd=str(self.project), live=False))
        self.assertIn("launch simulated", output.getvalue())
        self.assertIn("temporary checkpoint was removed", output.getvalue())
        self.assertFalse((self.project / ".relay").exists())
        self.assertEqual(relay.load_json(relay.STATE_FILE, {}), {"five_hour_pct": 42})

    def test_streaming_transcript_skips_malformed_records_and_bounds_history(self):
        transcript = self.project / "transcript.jsonl"
        events = ['{ broken', json.dumps({"type": "user", "message": {"content": 42}})]
        for number in range(35):
            events.append(json.dumps({"type": "user", "message": {"content": f"Request {number}"}}))
            events.append(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": None},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": f"file-{number}"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": f"command {number}"}},
                {"type": "tool_use", "name": "Bash", "input": ["invalid"]},
            ]}}))
        transcript.write_text("\n".join(events), encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=AssertionError("Transcript must be streamed")):
            info = relay.parse_transcript(transcript)
        self.assertEqual(info["first_request"], "Request 0")
        self.assertEqual(info["recent_requests"], [f"Request {n}" for n in range(30, 35)])
        self.assertEqual(info["files_changed"], [f"file-{n}" for n in range(10, 35)])
        self.assertEqual(info["commands"], [f"command {n}" for n in range(27, 35)])

    def test_resume_is_explicit_and_checks_cli_support(self):
        result = subprocess.CompletedProcess([], 0, "Options: --resume ID", "")
        with patch.object(relay.subprocess, "run", return_value=result):
            self.assertEqual(relay.ClaudeCode.resume_command("original"), ["claude", "--resume", "original"])
        result.stdout = "Usage: claude"
        with patch.object(relay.subprocess, "run", return_value=result), self.assertRaises(ValueError):
            relay.ClaudeCode.resume_command("original")


if __name__ == "__main__":
    unittest.main()
