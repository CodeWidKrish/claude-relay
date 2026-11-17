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
import time
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
        # Relay settings exported in the developer's shell must not change test results.
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("RELAY_DRY_RUN", "RELAY_HOME", "RELAY_PROMPT_FILE", "CLAUDE_CONFIG_DIR", "NO_COLOR")}
        environ = patch.dict(os.environ, clean, clear=True)
        environ.start()
        self.addCleanup(environ.stop)

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

    def test_empty_headless_answer_is_a_failure_with_the_reason(self):
        denied = "no output produced - a tool required the \"command\" permission that headless mode cannot prompt for"
        result = subprocess.CompletedProcess([], 0, "", denied)
        with patch.object(relay.shutil, "which", return_value="agy.exe"), patch.object(
                relay.subprocess, "run", return_value=result) as run:
            ok, answer = relay.run_antigravity_headless(relay.DEFAULT_CONFIG, "Question", self.project)
        self.assertFalse(ok)
        self.assertIn("empty answer", answer)
        self.assertIn("headless mode cannot prompt", answer)
        self.assertEqual(run.call_args.args[0][1:4], ["--mode", "plan", "-p"])

    def test_ask_prompt_steers_away_from_unapprovable_shell_commands(self):
        args = argparse.Namespace(question="Review app.py", file=None, cwd=str(self.project), allow_edits=False,
                                  timeout=5)
        with patch.object(relay, "run_antigravity_headless", return_value=(True, "Fine")) as headless, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            relay.cmd_ask(args)
        prompt = headless.call_args.args[1]
        self.assertIn("file-reading tools", prompt)
        self.assertIn("Do not modify any files", prompt)
        self.assertTrue((self.project / ".relay" / ".gitignore").is_file())

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

    def test_relay_folder_ignores_itself_in_git(self):
        path = relay.build_handoff(self.project, session="private")
        ignore = self.project / ".relay" / ".gitignore"
        self.assertTrue(path.is_file())
        self.assertEqual(ignore.read_text(encoding="utf-8").splitlines()[-1], "*")
        ignore.write_text("custom\n", encoding="utf-8")
        relay.build_handoff(self.project, session="private")
        self.assertEqual(ignore.read_text(encoding="utf-8"), "custom\n")

    def test_handoff_instructions_name_existing_sections(self):
        text = relay.build_handoff(self.project, session="headings").read_text(encoding="utf-8")
        steps = text.split("## Instructions for the next agent", 1)[1]
        for quoted in ("Last recorded agent update", "Progress since handoff"):
            self.assertIn(quoted, steps)
            self.assertIn("## " + quoted, text)

    def test_unexpanded_session_placeholder_is_ignored(self):
        self.assertIsNone(relay.clean_session("${CLAUDE_SESSION_ID}"))
        self.assertIsNone(relay.clean_session("$CLAUDE_SESSION_ID"))
        self.assertEqual(relay.clean_session("abc-123"), "abc-123")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            relay.main(["handoff", "--cwd", str(self.project), "--session", "${CLAUDE_SESSION_ID}"])
        self.assertIn("unexpanded session ID", err.getvalue())
        self.assertTrue((self.project / ".relay" / "HANDOFF.md").is_file())
        self.assertFalse((self.project / ".relay" / "sessions").exists())

    def test_unknown_reset_prepares_again_in_a_new_window(self):
        payload = {"session_id": "window", "workspace": {"current_dir": str(self.project)},
                   "rate_limits": {"five_hour": {"used_percentage": 95}}}
        path = relay.checkpoint(self.project, "window")
        with patch.object(relay, "read_stdin_json", return_value=payload), \
                contextlib.redirect_stdout(io.StringIO()):
            relay.cmd_statusline(None)
            self.assertTrue(path.is_file())
            path.unlink()
            relay.cmd_statusline(None)
            self.assertFalse(path.exists(), "same window must not rebuild the checkpoint")
            later = time.time() + 5 * 3600
            with patch.object(relay.time, "time", return_value=later):
                relay.cmd_statusline(None)
        self.assertTrue(path.is_file(), "a new 5-hour window must prepare a fresh checkpoint")

    def test_session_lock_is_released_after_an_error(self):
        with self.assertRaises(RuntimeError):
            with relay.session_state(self.project, "lock"):
                raise RuntimeError("boom")
        with relay.session_state(self.project, "lock") as state:
            state["ok"] = True
        with relay.session_state(self.project, "lock") as state:
            self.assertTrue(state["ok"])

    # ---- v1.5: safer and more useful checkpoints -------------------------------------------------
    def git_project(self):
        def g(*args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com"] + list(args),
                           cwd=str(self.project), check=True, capture_output=True)
        g("init", "-q")
        (self.project / "app.py").write_text("x = 1\n", encoding="utf-8")
        g("add", ".")
        g("commit", "-qm", "init")
        return g

    def test_checkpoint_redacts_secrets_but_keeps_normal_text(self):
        secret = "sk-ant-api03-" + "a" * 30
        self.transcript(self.project, "keys", f"Use OPENAI_API_KEY={secret} and keep max_tokens=1024")
        text = relay.build_handoff(self.project, session="keys").read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)
        self.assertIn("max_tokens=1024", text)
        relay.save_json(relay.CONFIG_FILE, {"redact": False})
        self.assertIn(secret, relay.build_handoff(self.project, session="keys").read_text(encoding="utf-8"))

    def test_checkpoint_includes_a_bounded_diff_excerpt(self):
        self.git_project()
        (self.project / "app.py").write_text("".join(f"line {n}\n" for n in range(400)), encoding="utf-8")
        relay.save_json(relay.CONFIG_FILE, {"diff_lines": 30})
        text = relay.build_handoff(self.project, session="diff").read_text(encoding="utf-8")
        excerpt = text.split("## Uncommitted changes (excerpt)", 1)[1].split("## Instructions", 1)[0]
        self.assertIn("+line 0", excerpt)
        self.assertIn("more lines: run git diff", excerpt)
        self.assertLess(len(excerpt.splitlines()), 40)
        self.assertRegex(text, r"Git commit at handoff:\*\* [0-9a-f]{40}")
        relay.save_json(relay.CONFIG_FILE, {"diff_lines": 0})
        self.assertNotIn("Uncommitted changes", relay.build_handoff(self.project, session="diff").read_text(
            encoding="utf-8"))

    def test_back_reports_what_changed_since_the_handoff(self):
        g = self.git_project()
        relay.build_handoff(self.project, session="trip")
        (self.project / "routes.py").write_text("router = 1\n", encoding="utf-8")
        g("add", "routes.py")
        g("commit", "-qm", "Add brightness route")
        (self.project / "notes.md").write_text("todo\n", encoding="utf-8")
        args = argparse.Namespace(cwd=str(self.project), session="trip", launch=False, resume=False)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            relay.cmd_back(args)
        text = relay.checkpoint(self.project, "trip").read_text(encoding="utf-8")
        returned = text.split("## Returned to Claude Code", 1)[1]
        for expected in ("Add brightness route", "routes.py", "notes.md"):
            self.assertIn(expected, returned)
            self.assertIn(expected, output.getvalue())

    def test_status_is_readable_and_tells_you_when_claude_is_back(self):
        relay.save_json(relay.STATE_FILE, {"five_hour_pct": 97, "resets_at": time.time() - 600,
                                           "updated": time.time() - 60})
        relay.build_handoff(self.project, session="shown")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            relay.cmd_status(argparse.Namespace(json=False, cwd=str(self.project)))
        text = output.getvalue()
        self.assertIn("97%", text)
        self.assertIn("Claude is available again", text)
        self.assertIn("Checkpoints  : 1", text)
        self.assertIn("shown", text)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            relay.cmd_status(argparse.Namespace(json=True, cwd=str(self.project)))
        self.assertEqual(json.loads(output.getvalue())["state"]["five_hour_pct"], 97)

    def test_ask_attaches_named_files_but_never_secrets(self):
        (self.project / "src").mkdir()
        (self.project / "src" / "routes.py").write_text("TOKEN = 'abcd1234efgh'\nrouter = 1\n", encoding="utf-8")
        (self.project / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (self.project / "big.log").write_text("x" * (relay.ASK_FILE_LIMIT + 1), encoding="utf-8")
        (self.other / "outside.py").write_text("print(1)\n", encoding="utf-8")
        files = dict(relay.referenced_files(
            "Review ./src/routes.py, .env, big.log, missing.py and ../other/outside.py", self.project))
        self.assertEqual(list(files), ["src/routes.py"])
        self.assertIn("router = 1", files["src/routes.py"])
        self.assertNotIn("abcd1234efgh", files["src/routes.py"])
        prompt = relay.ask_prompt("Review src/routes.py", list(files.items()), False)
        self.assertIn("--- src/routes.py ---", prompt)

    def test_ask_retries_without_tools_after_a_permission_denial(self):
        denied = (False, "Antigravity returned an empty answer.\nno output produced: the command permission was denied")
        args = argparse.Namespace(question="Review app.py", file=None, cwd=str(self.project), allow_edits=False,
                                  timeout=5)
        with patch.object(relay, "run_antigravity_headless", side_effect=[denied, (True, "Looks fine")]) as headless, \
                contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            relay.cmd_ask(args)
        self.assertEqual(headless.call_count, 2)
        self.assertIn("Do not call any tools", headless.call_args_list[1].args[1])
        self.assertIn("Looks fine", output.getvalue())

    def test_config_command_validates_and_saves(self):
        def config(*argv):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                relay.main(["config"] + list(argv))
            return output.getvalue()
        self.assertIn("prepare_at", config())
        config("mode", "manual")
        config("prepare_at", "85")
        config("redact", "off")
        self.assertEqual(relay.load_json(relay.CONFIG_FILE, {}), {"mode": "manual", "prepare_at": 85, "redact": False})
        self.assertEqual(config("mode").strip(), '"manual"')
        for bad in (["mode", "sometimes"], ["prepare_at", "20"], ["warn_at", "95"], ["colour", "true"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                config(*bad)
        self.assertEqual(relay.config()["prepare_at"], 85)


if __name__ == "__main__":
    unittest.main()
