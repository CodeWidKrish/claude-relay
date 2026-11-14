"""End-to-end and edge-case tests for Claude Relay (no Claude Code / Antigravity needed).

Run:  python tests/test_relay.py
Creates a fake home folder and a fake Claude Code transcript, then sends the relay the same
JSON Claude Code sends to the status line and to the StopFailure hook.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "relay.py"
RESULTS = []


def run(args, payload=None, env=None, raw=None, expect_ok=True):
    data = raw if raw is not None else (json.dumps(payload) if payload is not None else "")
    p = subprocess.run([sys.executable, str(RELAY)] + args, input=data.encode("utf-8"),
                       capture_output=True, env=env, cwd=env["PROJECT"])
    out = p.stdout.decode(env.get("PYTHONIOENCODING", "utf-8").split(":")[0], errors="replace")
    if expect_ok:
        assert p.returncode == 0, p.stderr.decode(errors="replace")
    return out


def case(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print(f"  PASS  {name}")
    except AssertionError as e:
        RESULTS.append((name, False, str(e)))
        print(f"  FAIL  {name}: {e}")


def setup():
    home = Path(tempfile.mkdtemp(prefix="relay-home-"))
    project = home / "projects" / "smart-home-api"
    project.mkdir(parents=True)
    (project / "app.py").write_text("print('hello')\n")
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), RELAY_DRY_RUN="1", PROJECT=str(project))
    env.pop("CLAUDE_CONFIG_DIR", None)
    key = "".join(c if c.isalnum() else "-" for c in str(project.resolve()))
    tdir = home / ".claude" / "projects" / key
    tdir.mkdir(parents=True)
    transcript = tdir / "session-123.jsonl"
    events = [
        {"type": "user", "message": {"role": "user", "content": "Build a REST API to switch Galaxy smart lights on/off with FastAPI"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "I'll create the FastAPI app and a devices model."},
            {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
                {"content": "Create FastAPI app", "status": "completed"},
                {"content": "Add /lights/{id} endpoints", "status": "in_progress"},
                {"content": "Write tests", "status": "pending"}]}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": str(project / "app.py")}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pip install fastapi uvicorn"}}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
        "this line is not JSON and must be ignored",
        {"type": "user", "message": {"role": "user", "content": "Now add an endpoint to dim the lights (मंद करें)"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Added PUT /lights/{id}/brightness. Next: validate 0-100 and add tests."},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": str(project / "routes.py")}}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "API Error: Claude usage limit reached. Your limit will reset at 7pm."}]}},
    ]
    transcript.write_text("\n".join(e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)
                                    for e in events), encoding="utf-8")
    return home, project, transcript, env


def main():
    home, project, transcript, env = setup()
    handoff = project / ".relay" / "HANDOFF.md"
    reset = int(time.time()) + 3600
    st = lambda pct, r=reset: {"model": {"display_name": "Sonnet"}, "workspace": {"current_dir": str(project)},
                               "transcript_path": str(transcript),
                               "rate_limits": {"five_hour": {"used_percentage": pct, "resets_at": r}}}
    hook = {"hook_event_name": "StopFailure", "error_type": "rate_limit", "error": "rate_limit",
            "cwd": str(project), "transcript_path": str(transcript)}
    print("Claude Relay test suite\n")

    def t_install():
        out = run(["install", "--mode", "auto", "--threshold", "90"], env=env)
        s = json.loads((home / ".claude" / "settings.json").read_text())
        assert "relay.py" in s["statusLine"]["command"], "status line not installed"
        assert s["hooks"]["StopFailure"][0]["matcher"] == "rate_limit"
        assert (home / ".claude" / "commands" / "handoff.md").exists()
        assert "Installed" in out
    case("1  install adds status line, StopFailure hook and /handoff", t_install)

    def t_reinstall():
        (home / ".claude" / "settings.json").write_text(json.dumps(
            {**json.loads((home / ".claude" / "settings.json").read_text()), "model": "sonnet"}))
        run(["install"], env=env)
        s = json.loads((home / ".claude" / "settings.json").read_text())
        assert len(s["hooks"]["StopFailure"]) == 1, "duplicate hooks after reinstall"
        assert s.get("model") == "sonnet", "other user settings were lost"
    case("2  re-install keeps user settings and adds no duplicate hooks", t_reinstall)

    def t_45():
        out = run(["statusline"], st(45), env)
        assert "45%" in out and "relay: auto" in out and not handoff.exists()
    case("3  status line at 45%: green bar, no handoff yet", t_45)

    def t_no_limits():
        out = run(["statusline"], {"model": {"display_name": "Opus"}}, env)
        assert "5h: --" in out
    case("4  status line before first message (no rate_limits): no crash", t_no_limits)

    def t_garbage():
        out = run(["statusline"], env=env, raw="{not json")
        assert out.strip(), "status line printed nothing"
    case("5  status line with broken JSON input: no crash", t_garbage)

    def t_cp1252():
        e = dict(env, PYTHONIOENCODING="cp1252")
        out = run(["statusline"], st(70), e)
        assert "70%" in out
    case("6  Windows cp1252 console: bar characters do not crash", t_cp1252)

    def t_92():
        out = run(["statusline"], st(92), env)
        assert "/handoff" in out and handoff.exists(), "handoff not pre-built at 92%"
        assert (project / ".agents" / "rules" / "relay-handoff.md").exists()
    case("7  status line at 92%: red bar + handoff prepared in advance", t_92)

    def t_ms_iso():
        run(["statusline"], st(50, reset * 1000), env)
        run(["statusline"], st(50, "2026-11-14T19:00:00+05:30"), env)
    case("8  resets_at as milliseconds or ISO text: handled", t_ms_iso)

    def t_other_error():
        before = handoff.stat().st_mtime
        time.sleep(0.05)
        out = run(["on-limit"], {**hook, "error_type": "authentication_failed", "error": "authentication_failed"}, env)
        assert "would open" not in out and handoff.stat().st_mtime == before
    case("9  StopFailure for a non-limit error (auth): ignored", t_other_error)

    def t_limit():
        out = run(["on-limit"], hook, env)
        assert "would open a new terminal" in out and "agy" in out and ".relay/HANDOFF.md" in out
        text = handoff.read_text(encoding="utf-8")
        for must in ["Build a REST API", "मंद करें", "PUT /lights/{id}/brightness", "routes.py",
                     "[ ] Add /lights/{id} endpoints", "pip install fastapi"]:
            assert must in text, f"missing in handoff: {must}"
        assert "will reset at 7pm" not in text, "limit error text leaked into 'where it stopped'"
    case("10 limit reached: handoff with full context + Antigravity opened", t_limit)

    def t_dupe():
        out = run(["on-limit"], hook, env)
        assert "would open" not in out
    case("11 StopFailure fires again for the same limit: no second window", t_dupe)

    def t_notes_kept():
        text = handoff.read_text(encoding="utf-8").replace("- (next agent: add notes here)",
                                                           "- Antigravity: added brightness validation")
        handoff.write_text(text, encoding="utf-8")
        run(["handoff", "--cwd", str(project)], env=env)
        assert "added brightness validation" in handoff.read_text(encoding="utf-8")
    case("12 rebuilding the handoff keeps Antigravity's progress notes", t_notes_kept)

    def t_manual():
        out = run(["handoff", "--launch", "--cwd", str(project)], env=env)
        assert "Handoff written" in out and "would open" in out
    case("13 manual switch (/handoff): handoff + Antigravity opened", t_manual)

    def t_model():
        run(["install", "--model", "Claude Sonnet 4.6"], env=env)
        out = run(["launch", "--cwd", str(project)], env=env)
        assert '--model "Claude Sonnet 4.6"' in out or "--model 'Claude Sonnet 4.6'" in out
        run(["install", "--model", ""], env=env)
    case("14 optional Claude model inside Antigravity is passed to agy", t_model)

    def t_back():
        out = run(["back", "--launch", "--cwd", str(project)], env=env)
        assert "Returned to Claude Code" in handoff.read_text(encoding="utf-8") and "claude" in out
    case("15 switch back after reset: Claude Code opened with the progress", t_back)

    def t_no_transcript():
        empty = home / "projects" / "brand-new"
        empty.mkdir()
        e = dict(env, PROJECT=str(empty))
        run(["handoff", "--cwd", str(empty), "--transcript", str(empty / "missing.jsonl")], env=e)
        assert (empty / ".relay" / "HANDOFF.md").exists()
    case("16 project with no transcript: handoff still created", t_no_transcript)

    def t_missing_agy():
        e = dict(env, RELAY_DRY_RUN="0", PATH="")
        cfgf = home / ".claude-relay" / "config.json"
        c = json.loads(cfgf.read_text()); c["target_cmd"] = "agy-not-installed"; cfgf.write_text(json.dumps(c))
        out = run(["launch", "--cwd", str(project)], env=e)
        c["target_cmd"] = "agy"; cfgf.write_text(json.dumps(c))
        assert "not found on PATH" in out and "run this yourself" in out
    case("17 Antigravity not installed: clear message with the exact command", t_missing_agy)

    def t_simulate():
        out = run(["simulate", "--cwd", str(project)], env=env)
        assert "92%" in out and "would open" in out
    case("18 'simulate' demo mode works without using the real limit", t_simulate)

    def t_doctor():
        out = run(["doctor"], env=env)
        assert "Status line installed" in out and "StopFailure hook installed" in out
    case("19 'doctor' reports the setup", t_doctor)

    def t_bad_settings():
        sp = home / ".claude" / "settings.json"
        good = sp.read_text()
        sp.write_text("{ broken")
        p = subprocess.run([sys.executable, str(RELAY), "install"], capture_output=True, env=env, cwd=env["PROJECT"])
        assert p.returncode != 0 and sp.read_text() == "{ broken", "broken settings were overwritten"
        sp.write_text(good)
    case("20 broken Claude settings file is never overwritten", t_bad_settings)

    def t_uninstall():
        run(["uninstall"], env=env)
        s = json.loads((home / ".claude" / "settings.json").read_text())
        assert "relay.py" not in json.dumps(s) and s.get("model") == "sonnet"
    case("21 uninstall removes Relay and keeps other settings", t_uninstall)

    passed = sum(1 for r in RESULTS if r[1])
    print(f"\n{passed}/{len(RESULTS)} tests passed")
    print("ALL TESTS PASSED" if passed == len(RESULTS) else "SOME TESTS FAILED")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
