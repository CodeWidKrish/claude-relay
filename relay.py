#!/usr/bin/env python3
"""
Claude Relay - continue your Claude Code work in Google Antigravity when the
Claude Pro 5-hour session limit is reached (automatically or manually), then
come back to Claude Code when the limit resets.

Pure Python standard library (3.8+). Windows, macOS and Linux.

Commands
  relay.py doctor                 check that everything needed is installed
  relay.py install [--mode auto|manual] [--threshold 90] [--model "Claude Sonnet 4.6"]
  relay.py uninstall              remove Relay from Claude Code (restores the settings backup)
  relay.py statusline             used by Claude Code's status line (JSON on stdin)
  relay.py on-limit               used by Claude Code's StopFailure hook (JSON on stdin)
  relay.py handoff [--launch]     manual switch: write the handoff (and open Antigravity)
  relay.py launch                 open Antigravity on the existing handoff
  relay.py ask "question" | --file F   ask Antigravity from inside a Claude Code session
  relay.py back [--launch]        after the reset: hand the work back to Claude Code
  relay.py simulate [--live]      demo the automatic switch without using up your limit
  relay.py status                 show last known usage, mode and log
"""
import argparse
import datetime as dt
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Windows consoles often use cp1252: never crash on the bar characters or emoji.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

VERSION = "1.2.1"
IS_WIN = platform.system() == "Windows"
HOME = Path.home()
STATE_DIR = Path(os.environ.get("RELAY_HOME", HOME / ".claude-relay"))
STATE_FILE = STATE_DIR / "state.json"
CONFIG_FILE = STATE_DIR / "config.json"
LOG_FILE = STATE_DIR / "relay.log"
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude"))
CLAUDE_SETTINGS = CLAUDE_DIR / "settings.json"
CLAUDE_PROJECTS = CLAUDE_DIR / "projects"
HANDOFF_DIR, HANDOFF_NAME = ".relay", "HANDOFF.md"
SWITCH_COOLDOWN = 15 * 60  # seconds; StopFailure can fire many times for one limit

DEFAULT_CONFIG = {
    "mode": "auto",          # auto: switch when the limit is hit; manual: only on /handoff
    "warn_at": 80,           # status line turns yellow
    "prepare_at": 90,        # handoff written in advance (while Claude can still answer)
    "target_cmd": "agy",     # Antigravity CLI
    "target_model": "",      # e.g. "Claude Sonnet 4.6" (exact name from `agy models`)
    "color": True,
    "dry_run": False,        # print launch commands instead of running them
}


# ------------------------------------------------------------------------ utils
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # atomic: a crash never leaves half a settings file


def config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_FILE, {}))
    if os.environ.get("RELAY_DRY_RUN") == "1":
        cfg["dry_run"] = True
    if os.environ.get("NO_COLOR"):
        cfg["color"] = False
    return cfg


def read_stdin_json():
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_epoch(v):
    """resets_at is epoch seconds today; also accept ms or ISO strings defensively."""
    if v in (None, ""):
        return None
    try:
        n = float(v)
        return int(n / 1000) if n > 1e12 else int(n)
    except (TypeError, ValueError):
        try:
            return int(dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
        except Exception:
            return None


def fmt_time(epoch):
    epoch = to_epoch(epoch)
    return dt.datetime.fromtimestamp(epoch).strftime("%H:%M (%d %b)") if epoch else "unknown"


def log(msg):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')}  {msg}\n")
    except Exception:
        pass


def cmd_path(p):
    """Forward slashes work in cmd, PowerShell and Git Bash (which Claude Code uses on Windows)."""
    return Path(p).resolve().as_posix()


# ------------------------------------------------------------ transcript reading
def project_dir_for(cwd):
    key = "".join(c if c.isalnum() else "-" for c in str(Path(cwd).resolve()))
    return CLAUDE_PROJECTS / key


def latest_transcript(cwd):
    d = project_dir_for(cwd)
    files = list(d.glob("*.jsonl")) if d.exists() else []
    if not files and CLAUDE_PROJECTS.exists():
        files = list(CLAUDE_PROJECTS.glob("*/*.jsonl"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in (content or [])
                     if isinstance(b, dict) and b.get("type") == "text")


def parse_transcript(path):
    info = {"first_request": "", "recent_requests": [], "last_answer": "",
            "files_changed": [], "commands": [], "todos": []}
    if not path or not Path(path).exists():
        return info
    files, cmds = [], []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return info
    for line in lines:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict) or ev.get("isMeta") or ev.get("isSidechain"):
            continue
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        role, content = msg.get("role") or ev.get("type"), msg.get("content")
        if role == "user":
            is_tool_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            text = _text_of(content).strip()
            if text and not is_tool_result and not text.startswith("<") and "[Request interrupted" not in text:
                info["first_request"] = info["first_request"] or text
                info["recent_requests"].append(text)
        elif role == "assistant":
            text = _text_of(content).strip()
            if text and not text.startswith("API Error") and "usage limit" not in text.lower():
                info["last_answer"] = text
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name, inp = b.get("name", ""), b.get("input") or {}
                if name in ("Edit", "Write", "MultiEdit", "NotebookEdit") and inp.get("file_path"):
                    files.append(inp["file_path"])
                elif name in ("Bash", "PowerShell") and inp.get("command"):
                    cmds.append(inp["command"])
                elif name == "TodoWrite" and isinstance(inp.get("todos"), list):
                    info["todos"] = inp["todos"]
    info["recent_requests"] = info["recent_requests"][-5:]
    info["files_changed"] = list(dict.fromkeys(files))[-25:]
    info["commands"] = cmds[-8:]
    return info


def git_summary(cwd):
    if not shutil.which("git"):
        return ""
    try:
        kw = dict(cwd=str(cwd), capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
        st = subprocess.run(["git", "status", "--short"], **kw)
        if st.returncode != 0:
            return ""
        diff = subprocess.run(["git", "diff", "--stat"], **kw)
        return (st.stdout.strip() + ("\n\n" + diff.stdout.strip() if diff.stdout.strip() else "")).strip()
    except Exception:
        return ""


# ------------------------------------------------------------------- handoff file
def build_handoff(cwd, transcript=None, reason="manual", resets_at=None):
    cwd = Path(cwd).resolve()
    transcript = transcript if transcript and Path(transcript).exists() else latest_transcript(cwd)
    info = parse_transcript(transcript)
    clip = lambda s, n: (s[:n] + " …") if len(s) > n else s
    out = cwd / HANDOFF_DIR / HANDOFF_NAME
    keep_notes = ""
    if out.exists():  # never lose the other agent's progress notes
        old = out.read_text(encoding="utf-8", errors="ignore")
        if "## Progress since handoff" in old:
            keep_notes = old.split("## Progress since handoff", 1)[1].strip()
    L = ["# Handoff: continue this task", "",
         f"- **Created:** {dt.datetime.now():%d %b %Y, %H:%M}",
         f"- **Reason:** {reason}",
         "- **From → to:** Claude Code → Antigravity",
         f"- **Claude limit resets at:** {fmt_time(resets_at)}",
         f"- **Project folder:** `{cwd}`", "",
         "## Original goal", clip(info["first_request"] or "(not found in transcript – ask the user)", 2000), "",
         "## Most recent requests (oldest → newest)"]
    L += [f"{i}. {clip(r, 600)}" for i, r in enumerate(info["recent_requests"], 1)] or ["- (none)"]
    L += ["", "## Where the previous agent stopped", clip(info["last_answer"] or "(no answer recorded)", 2500), ""]
    if info["todos"]:
        L.append("## Task list")
        for t in info["todos"]:
            t = t if isinstance(t, dict) else {"content": str(t)}
            L.append(f"- [{'x' if t.get('status') == 'completed' else ' '}] "
                     f"{t.get('content') or t.get('subject') or ''}  ({t.get('status', '')})")
        L.append("")
    L.append("## Files changed in that session")
    L += [f"- `{f}`" for f in info["files_changed"]] or ["- (none)"]
    if info["commands"]:
        L += ["", "## Last commands run", "```"] + info["commands"] + ["```"]
    g = git_summary(cwd)
    if g:
        L += ["", "## Git state", "```", g, "```"]
    L += ["", "## Instructions for the next agent",
          "1. Read this file and the files listed above before changing anything.",
          "2. Continue from 'Where the previous agent stopped'. Do not redo finished work.",
          "3. As you work, add short notes to 'Progress since handoff' below, so the work can move back.",
          "", "## Progress since handoff", keep_notes or "- (next agent: add notes here)", ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    ensure_rules(cwd)
    log(f"handoff written: {out} (reason={reason}, transcript={transcript})")
    return out


def ensure_rules(cwd):
    """Antigravity IDE and agy CLI both load .agents/rules/*.md from the workspace."""
    rule = Path(cwd) / ".agents" / "rules" / "relay-handoff.md"
    if not rule.exists():
        rule.parent.mkdir(parents=True, exist_ok=True)
        rule.write_text("# Relay handoff\n\nIf `.relay/HANDOFF.md` exists, read it before anything else: it is "
                        "the task another coding agent was working on. Continue that work and add short notes "
                        "to its 'Progress since handoff' section as you go.\n", encoding="utf-8")


# ---------------------------------------------------------------------- launching
PROMPT_TO_AG = ("Claude Code reached its usage limit. Read .relay/HANDOFF.md and continue the task "
                "exactly where it stopped. Add short notes to the Progress since handoff section as you work.")
PROMPT_BACK = ("I worked on this task in Antigravity while you were rate-limited. Read .relay/HANDOFF.md, "
               "especially the Progress since handoff section, check the current files, then continue.")


def antigravity_command(cfg, prompt=PROMPT_TO_AG):
    cmd = [cfg["target_cmd"]]
    if cfg.get("target_model"):
        cmd += ["--model", cfg["target_model"]]
    return cmd + ["-i", prompt]  # -i: run the opening prompt, then stay interactive


def open_in_new_terminal(cmd, cwd, cfg):
    """Open the agent in its own terminal window, detached from Claude Code's hook process."""
    cwd = str(Path(cwd).resolve())
    exe = shutil.which(cmd[0])
    shown = subprocess.list2cmdline(cmd) if IS_WIN else " ".join(shlex.quote(c) for c in cmd)
    if cfg.get("dry_run"):
        print(f"[dry-run] would open a new terminal in {cwd} and run: {shown}")
        log(f"dry-run launch: {shown}")
        return True
    if not exe:
        msg = (f"'{cmd[0]}' was not found on PATH. Install it, or run this yourself in {cwd}:\n  {shown}")
        print(msg)
        log(msg)
        return False
    try:
        if IS_WIN:
            # A real argv list (no shell quoting); CREATE_NEW_CONSOLE gives it its own window.
            # .cmd/.bat shims (npm installs) need cmd.exe /k so the window stays open.
            argv = [exe] + cmd[1:]
            if exe.lower().endswith((".cmd", ".bat")):
                argv = ["cmd.exe", "/k"] + argv
            subprocess.Popen(argv, cwd=cwd, creationflags=subprocess.CREATE_NEW_CONSOLE,
                             close_fds=True)
        elif platform.system() == "Darwin":
            line = f"cd {shlex.quote(cwd)} && {shlex.quote(exe)} " + " ".join(shlex.quote(c) for c in cmd[1:])
            script = f'tell application "Terminal" to do script {json.dumps(line)}\n' \
                     'tell application "Terminal" to activate'
            subprocess.Popen(["osascript", "-e", script], start_new_session=True)
        else:
            line = f"cd {shlex.quote(cwd)} && {shlex.quote(exe)} " + " ".join(shlex.quote(c) for c in cmd[1:])
            for term, args in (("gnome-terminal", ["--", "bash", "-lc"]), ("konsole", ["-e", "bash", "-lc"]),
                               ("x-terminal-emulator", ["-e", "bash", "-lc"]), ("xterm", ["-e", "bash", "-lc"])):
                if shutil.which(term):
                    subprocess.Popen([term] + args + [line + "; exec bash"], start_new_session=True)
                    break
            else:  # no GUI terminal (SSH/WSL): tell the user the exact command
                print(f"No terminal window available. Run this in {cwd}:\n  {line}")
                return False
        log(f"launched: {shown} (cwd={cwd})")
        return True
    except Exception as e:
        print(f"Could not open a new window ({e}). Run this in {cwd}:\n  {shown}")
        log(f"launch failed: {e}")
        return False


def notify(title, text):
    """Best-effort desktop notification (StopFailure output is not shown by Claude Code)."""
    try:
        if IS_WIN:
            ps = ("[reflection.assembly]::loadwithpartialname('System.Windows.Forms')|Out-Null;"
                  "$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;"
                  f"$n.Visible=$true;$n.ShowBalloonTip(8000,'{title}','{text.replace(chr(39), '')}','Info');"
                  "Start-Sleep 9;$n.Dispose()")
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif platform.system() == "Darwin":
            subprocess.Popen(["osascript", "-e", f"display notification {json.dumps(text)} with title {json.dumps(title)}"])
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", title, text])
    except Exception:
        pass


# ----------------------------------------------------------------------- commands
def cmd_statusline(_args):
    data = read_stdin_json()
    try:
        cfg, state = config(), load_json(STATE_FILE, {})
        five = ((data.get("rate_limits") or {}).get("five_hour") or {})
        pct, resets = to_float(five.get("used_percentage")), to_epoch(five.get("resets_at"))
        cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or os.getcwd()
        model = (data.get("model") or {}).get("display_name") or "Claude"
        if pct is not None:
            state.update({"five_hour_pct": pct, "resets_at": resets, "cwd": cwd,
                          "transcript": data.get("transcript_path"), "updated": time.time()})
            window = resets or "unknown"
            if pct >= cfg["prepare_at"] and state.get("prepared_for") != window:
                try:
                    build_handoff(cwd, data.get("transcript_path"),
                                  reason=f"5-hour usage at {pct:.0f}% (prepared in advance)", resets_at=resets)
                    state["prepared_for"] = window
                except Exception as e:
                    log(f"prepare failed: {e}")
            save_json(STATE_FILE, state)
        if pct is None:
            print(f"{model} | 5h: -- | relay: {cfg['mode']}")
            return
        c = (("\033[32m" if pct < cfg["warn_at"] else "\033[33m" if pct < cfg["prepare_at"] else "\033[31m")
             if cfg["color"] else "")
        r = "\033[0m" if cfg["color"] else ""
        bar = "█" * int(min(max(pct, 0), 100) // 10)
        bar += "░" * (10 - len(bar))
        hint = "  → type /handoff to switch to Antigravity" if pct >= cfg["prepare_at"] else ""
        print(f"{model} | 5h {c}{bar} {pct:.0f}%{r} resets {fmt_time(resets)} | relay: {cfg['mode']}{hint}")
    except Exception as e:  # a status line must never crash
        log(f"statusline error: {e}")
        print("Claude | relay")


def _switch(cwd, transcript, reason, launch):
    cfg, state = config(), load_json(STATE_FILE, {})
    path = build_handoff(cwd, transcript, reason=reason, resets_at=state.get("resets_at"))
    ok = open_in_new_terminal(antigravity_command(cfg), cwd, cfg) if launch else None
    state["last_switch"] = time.time()
    save_json(STATE_FILE, state)
    return path, ok


def cmd_on_limit(_args):
    """Claude Code StopFailure hook (matcher: rate_limit). Must never fail."""
    try:
        data = read_stdin_json()
        err = str(data.get("error_type") or data.get("error") or "")
        if err and err != "rate_limit":
            return
        cfg, state = config(), load_json(STATE_FILE, {})
        if time.time() - float(state.get("last_switch") or 0) < SWITCH_COOLDOWN:
            log("on-limit: switched recently, skipping duplicate StopFailure")
            return
        cwd = data.get("cwd") or state.get("cwd") or os.getcwd()
        auto = cfg["mode"] == "auto"
        path, _ = _switch(cwd, data.get("transcript_path"), "Claude usage limit reached (rate_limit)", auto)
        msg = ("Work moved to Antigravity." if auto else
               "Handoff ready. Run: relay.py launch") + f" Resets {fmt_time(state.get('resets_at'))}."
        notify("Claude Relay", f"Claude limit reached. {msg}")
        print(json.dumps({"systemMessage": f"Claude Relay: {msg} ({path})"}))
    except Exception as e:
        log(f"on-limit error: {e}")


def cmd_handoff(args):
    cwd = args.cwd or os.getcwd()
    path, _ = _switch(cwd, args.transcript, "manual switch", args.launch)
    print(f"Handoff written: {path}")


def cmd_launch(args):
    cfg = config()
    open_in_new_terminal(antigravity_command(cfg), args.cwd or os.getcwd(), cfg)


ASK_DIR = "answers"
ASK_TIMEOUT = 600  # seconds


def run_antigravity_headless(cfg, prompt, cwd, allow_edits=False, timeout=ASK_TIMEOUT):
    """Run `agy -p` (headless: one prompt, print the answer, exit). Returns (ok, text)."""
    exe = shutil.which(cfg["target_cmd"])
    if not exe:
        return False, (f"'{cfg['target_cmd']}' (Antigravity CLI) was not found on PATH. "
                       "Install it, sign in once by running `agy`, then try again.")
    argv = [exe]
    if cfg.get("target_model"):
        argv += ["--model", cfg["target_model"]]
    # plan mode = Antigravity may read the project but not change files (safe default)
    via_cmd = IS_WIN and exe.lower().endswith((".cmd", ".bat"))
    if via_cmd or (IS_WIN and "\n" in prompt) or os.environ.get("RELAY_PROMPT_FILE") == "1":
        # cmd.exe cuts arguments at line breaks and expands % ^ & |, so a long/multi-line request is
        # written to a file and Antigravity gets a short single-line instruction to read it.
        pf = Path(cwd) / HANDOFF_DIR / "ask-prompt.md"
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(prompt, encoding="utf-8")
        prompt = ("Read the file .relay/ask-prompt.md in this folder and do exactly what it asks. "
                  "Reply with the answer only.")
    argv += ["--mode", "accept-edits" if allow_edits else "plan", "-p", prompt]
    if via_cmd:
        argv = ["cmd.exe", "/c"] + argv
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"Antigravity did not answer within {timeout} seconds."
    except Exception as e:
        return False, f"Could not run Antigravity: {e}"
    out = (r.stdout or "").strip()
    if r.returncode != 0 and not out:
        return False, f"Antigravity exited with code {r.returncode}: {(r.stderr or '').strip()[:1500]}"
    return True, out or "(Antigravity returned an empty answer)"


def cmd_ask(args):
    """Use Antigravity from inside a Claude Code session: send a question/sub-task, print the answer."""
    cfg, cwd = config(), Path(args.cwd or os.getcwd()).resolve()
    question = args.question or ""
    if args.file:
        try:
            question = Path(args.file).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"Could not read {args.file}: {e}")
            sys.exit(1)
    question = question.strip()
    if not question:
        print('Nothing to ask. Usage: relay.py ask "your question"  or  relay.py ask --file question.md')
        sys.exit(1)
    prompt = ("You are helping another coding agent (Claude Code) that is working in this project folder. "
              + ("" if args.allow_edits else "Do not modify any files; answer only. ")
              + "Answer the request below clearly and concisely.\n\nREQUEST:\n" + question)
    if cfg.get("dry_run"):
        print(f"[dry-run] would run Antigravity headless in {cwd} with: {question[:200]}")
        return
    print(f"Asking Antigravity ({'may edit files' if args.allow_edits else 'read-only'})...", file=sys.stderr)
    ok, answer = run_antigravity_headless(cfg, prompt, cwd, args.allow_edits, args.timeout)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = cwd / HANDOFF_DIR / ASK_DIR / f"{stamp}.md"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"# Question to Antigravity ({stamp})\n\n{question}\n\n# Answer\n\n{answer}\n", encoding="utf-8")
    except OSError:
        out = None
    log(f"ask: ok={ok} saved={out}")
    print(answer)
    if out:
        print(f"\n[saved to {out}]")
    if not ok:
        sys.exit(2)


def cmd_back(args):
    cfg, cwd = config(), Path(args.cwd or os.getcwd())
    handoff = cwd / HANDOFF_DIR / HANDOFF_NAME
    if not handoff.exists():
        print("No .relay/HANDOFF.md in this folder - nothing to hand back.")
        return
    g = git_summary(cwd)
    with open(handoff, "a", encoding="utf-8") as f:
        f.write(f"\n## Returned to Claude Code – {dt.datetime.now():%d %b %Y, %H:%M}\n" +
                (f"```\n{g}\n```\n" if g else ""))
    print(f"Handoff updated: {handoff}")
    if args.launch:
        open_in_new_terminal(["claude", PROMPT_BACK], cwd, cfg)


def cmd_simulate(args):
    """Feed Relay exactly what Claude Code sends, so the switch can be shown without using the limit."""
    cwd = args.cwd or os.getcwd()
    state = load_json(STATE_FILE, {})
    state.pop("last_switch", None)
    save_json(STATE_FILE, state)
    if not args.live:
        os.environ["RELAY_DRY_RUN"] = "1"
    reset = int(time.time()) + 2 * 3600
    me = [sys.executable, str(Path(__file__).resolve())]
    env = dict(os.environ)
    for pct in (45, 85, 92):
        payload = {"model": {"display_name": "Claude Sonnet"}, "workspace": {"current_dir": cwd},
                   "rate_limits": {"five_hour": {"used_percentage": pct, "resets_at": reset}}}
        out = subprocess.run(me + ["statusline"], input=json.dumps(payload), capture_output=True,
                             text=True, encoding="utf-8", cwd=cwd, env=env)
        print(f"status line at {pct}%:  {out.stdout.strip()}")
    print("\nSimulating Claude Code's StopFailure hook (error_type = rate_limit) ...")
    out = subprocess.run(me + ["on-limit"], input=json.dumps(
        {"hook_event_name": "StopFailure", "error_type": "rate_limit", "error": "rate_limit", "cwd": cwd}),
        capture_output=True, text=True, encoding="utf-8", cwd=cwd, env=env)
    print(out.stdout.strip() or "(hook ran)")
    print(f"\nHandoff file: {Path(cwd) / HANDOFF_DIR / HANDOFF_NAME}")


def cmd_doctor(_args):
    cfg, ok_all = config(), True

    def check(label, ok, fix=""):
        nonlocal ok_all
        ok_all &= bool(ok)
        print(f"  [{'OK ' if ok else 'FIX'}] {label}" + ("" if ok else f"\n        → {fix}"))

    def version(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace")
            return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else "?"
        except Exception:
            return None

    print(f"Claude Relay {VERSION} - system check ({platform.system()} {platform.release()})\n")
    check(f"Python {platform.python_version()}", sys.version_info >= (3, 8), "Install Python 3.8 or newer")
    claude = shutil.which("claude")
    check(f"Claude Code CLI: {version([claude, '--version']) if claude else 'not found'}", claude,
          "Install Claude Code and sign in with your Claude Pro account")
    agy = shutil.which(cfg["target_cmd"])
    check(f"Antigravity CLI ({cfg['target_cmd']}): {version([agy, '--version']) if agy else 'not found'}", agy,
          "Install the Antigravity CLI, sign in, then run `agy` once")
    check(f"git: {'found' if shutil.which('git') else 'not found (optional)'}", True)
    s = load_json(CLAUDE_SETTINGS, {})
    check("Status line installed", "relay.py" in json.dumps(s.get("statusLine", {})), "Run: relay.py install")
    check("StopFailure hook installed", "relay.py" in json.dumps(s.get("hooks", {}).get("StopFailure", [])),
          "Run: relay.py install")
    check("/handoff command installed", (CLAUDE_DIR / "commands" / "handoff.md").exists(), "Run: relay.py install")
    check("/ask-antigravity command installed", (CLAUDE_DIR / "commands" / "ask-antigravity.md").exists(),
          "Run: relay.py install")
    st = load_json(STATE_FILE, {})
    if st.get("five_hour_pct") is not None:
        print(f"\n  Last seen 5-hour usage: {st['five_hour_pct']:.0f}% (resets {fmt_time(st.get('resets_at'))})")
    else:
        print("\n  No usage data yet: send one message in Claude Code (Pro/Max plans only).")
    print(f"  Mode: {cfg['mode']} | prepare at {cfg['prepare_at']}% | model in Antigravity: "
          f"{cfg['target_model'] or 'Antigravity default'}")
    print("\nAll good." if ok_all else "\nFix the items marked FIX, then run doctor again.")


def cmd_status(_args):
    print(json.dumps({"version": VERSION, "config": config(), "state": load_json(STATE_FILE, {})}, indent=2))
    if LOG_FILE.exists():
        print("\nLast log lines:\n" + "\n".join(LOG_FILE.read_text(encoding="utf-8").splitlines()[-10:]))


def cmd_install(args):
    cfg = config()
    if args.mode:
        cfg["mode"] = args.mode
    if args.threshold:
        cfg["prepare_at"] = max(50, min(99, args.threshold))
    if args.model is not None:
        cfg["target_model"] = args.model
    cfg["dry_run"] = False
    save_json(CONFIG_FILE, cfg)
    q = lambda x: f'"{x}"' if " " in x else x  # unquoted paths also work if Claude Code uses PowerShell
    py = cmd_path(sys.executable)
    for name in ("python", "python3", "py"):  # prefer a bare command: no spaces, works in every shell
        found = shutil.which(name)
        try:
            if found and name != "py" and os.path.samefile(found, sys.executable):
                py = name
                break
        except OSError:
            pass
    if " " in str(Path(__file__).resolve()):
        print("Tip: move this folder to a path without spaces (e.g. C:\\claude-relay) and run install again "
              "- it makes the Claude Code hooks more robust.")
    run = lambda sub: f'{q(py)} {q(cmd_path(__file__))} {sub}'
    settings = load_json(CLAUDE_SETTINGS, None)
    if settings is None and CLAUDE_SETTINGS.exists():
        print(f"{CLAUDE_SETTINGS} is not valid JSON - fix it first, nothing was changed.")
        sys.exit(1)
    settings = settings or {}
    if CLAUDE_SETTINGS.exists() and "relay.py" not in CLAUDE_SETTINGS.read_text(encoding="utf-8"):
        backup = CLAUDE_SETTINGS.with_name("settings.relay-backup.json")
        if not backup.exists():
            shutil.copy2(CLAUDE_SETTINGS, backup)
            print(f"Backup of your Claude settings: {backup}")
    old_sl = settings.get("statusLine")
    if old_sl and "relay.py" not in json.dumps(old_sl):
        print("Note: your previous status line was replaced (it is kept in the backup).")
    settings["statusLine"] = {"type": "command", "command": run("statusline"), "padding": 0}
    hooks = settings.setdefault("hooks", {})
    hooks["StopFailure"] = [h for h in hooks.get("StopFailure", []) if "relay.py" not in json.dumps(h)] + [
        {"matcher": "rate_limit", "hooks": [{"type": "command", "command": run("on-limit"), "timeout": 60}]}]
    save_json(CLAUDE_SETTINGS, settings)
    cmd_dir = CLAUDE_DIR / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    (cmd_dir / "handoff.md").write_text(
        "---\ndescription: Hand this task over to Google Antigravity (Claude Relay)\n---\n"
        "Hand the current task over to Google Antigravity:\n"
        f"1. Run with the Bash tool: {run('handoff')}\n"
        "2. Open `.relay/HANDOFF.md` and, under 'Where the previous agent stopped', add a short summary "
        "(max 8 bullets): what is done, what is in progress, the exact next step, and any gotchas.\n"
        f"3. Run with the Bash tool: {run('launch')}\n"
        "4. Tell me the work is now open in Antigravity.\n", encoding="utf-8")
    (cmd_dir / "ask-antigravity.md").write_text(
        "---\ndescription: Ask Google Antigravity a question or give it a sub-task, without leaving this session\n"
        "argument-hint: <question or sub-task>\n---\n"
        "Use Google Antigravity as a second opinion / helper, then continue here.\n"
        "Request: $ARGUMENTS\n\n"
        "1. Write the request above to `.relay/ask.md` with the Write tool. Add any short context Antigravity needs "
        "(relevant file paths, what you already tried). If the request is empty, ask me what to send.\n"
        f"2. Run with the Bash tool (timeout 600000 ms): {run('ask --file .relay/ask.md')}\n"
        "3. Read Antigravity's answer, tell me the key points in a few lines, and say whether you agree. "
        "Do not apply any change it suggests until I confirm.\n", encoding="utf-8")
    print(f"Installed Claude Relay {VERSION}. Mode: {cfg['mode']}, handoff prepared at {cfg['prepare_at']}%.")
    print(f"  Claude settings : {CLAUDE_SETTINGS}\n  Slash commands  : /handoff, /ask-antigravity\n  Relay config    : {CONFIG_FILE}")
    print("Restart Claude Code, then run: relay.py doctor")


def cmd_uninstall(_args):
    settings = load_json(CLAUDE_SETTINGS, {})
    if "relay.py" in json.dumps(settings.get("statusLine", {})):
        backup = load_json(CLAUDE_SETTINGS.with_name("settings.relay-backup.json"), {})
        if backup.get("statusLine") and "relay.py" not in json.dumps(backup["statusLine"]):
            settings["statusLine"] = backup["statusLine"]
        else:
            settings.pop("statusLine", None)
    hooks = settings.get("hooks", {})
    if "StopFailure" in hooks:
        hooks["StopFailure"] = [h for h in hooks["StopFailure"] if "relay.py" not in json.dumps(h)]
        if not hooks["StopFailure"]:
            hooks.pop("StopFailure")
    save_json(CLAUDE_SETTINGS, settings)
    for name in ("handoff.md", "ask-antigravity.md"):
        try:
            (CLAUDE_DIR / "commands" / name).unlink()
        except FileNotFoundError:
            pass
    print("Claude Relay removed from Claude Code. Restart Claude Code.")


def main(argv=None):
    p = argparse.ArgumentParser(description="Claude Code → Antigravity relay")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install")
    i.add_argument("--mode", choices=["auto", "manual"]); i.add_argument("--threshold", type=int)
    i.add_argument("--model")
    for name in ("uninstall", "statusline", "on-limit", "status", "doctor"):
        sub.add_parser(name)
    h = sub.add_parser("handoff"); h.add_argument("--launch", action="store_true")
    h.add_argument("--cwd"); h.add_argument("--transcript")
    l = sub.add_parser("launch"); l.add_argument("--cwd")
    a = sub.add_parser("ask"); a.add_argument("question", nargs="?"); a.add_argument("--file")
    a.add_argument("--cwd"); a.add_argument("--allow-edits", action="store_true")
    a.add_argument("--timeout", type=int, default=ASK_TIMEOUT)
    b = sub.add_parser("back"); b.add_argument("--launch", action="store_true"); b.add_argument("--cwd")
    s = sub.add_parser("simulate"); s.add_argument("--live", action="store_true"); s.add_argument("--cwd")
    args = p.parse_args(argv)
    {"install": cmd_install, "uninstall": cmd_uninstall, "statusline": cmd_statusline, "on-limit": cmd_on_limit,
     "status": cmd_status, "doctor": cmd_doctor, "handoff": cmd_handoff, "launch": cmd_launch, "ask": cmd_ask,
     "back": cmd_back, "simulate": cmd_simulate}[args.cmd](args)


if __name__ == "__main__":
    main()
