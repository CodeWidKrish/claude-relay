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
from collections import OrderedDict, deque
import contextlib
import hashlib
import tempfile
import uuid
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

VERSION = "1.4.2"
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
    except (OSError, ValueError):
        return default


@contextlib.contextmanager
def atomic_text(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            tmp = Path(stream.name)
            yield stream
        os.replace(tmp, path)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()


def save_json(path, data):
    with atomic_text(path) as stream:
        json.dump(data, stream, indent=2)


def save_text(path, content):
    with atomic_text(path) as stream:
        stream.write(content)


@contextlib.contextmanager
def session_state(cwd, session):
    key = hashlib.sha256(json.dumps([os.path.normcase(str(Path(cwd).resolve())), session]).encode()).hexdigest()
    folder = STATE_DIR / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (key + ".json")
    with (folder / (key + ".lock")).open("a+b") as lock:
        if IS_WIN:
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_json(path, {})
        yield state
        save_json(path, state)


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


def select_transcript(cwd, supplied=None, session=None):
    folder = project_dir_for(cwd).resolve()
    if supplied:
        path = Path(supplied).resolve()
        if path.parent == folder and path.suffix == ".jsonl" and path.is_file():
            if not session or path.stem == session:
                return path
    if session:
        path = (folder / (session + ".jsonl")).resolve()
        return path if path.parent == folder and path.is_file() else None
    files = [p for p in folder.glob("*.jsonl") if p.resolve().parent == folder and p.is_file()]
    return files[0] if len(files) == 1 else None


class ClaudeCode:
    executable = "claude"

    @staticmethod
    def context(data):
        workspace = data.get("workspace") or {}
        cwd = workspace.get("project_dir") or data.get("cwd") or workspace.get("current_dir") or os.getcwd()
        session = data.get("session_id")
        transcript = select_transcript(cwd, data.get("transcript_path"), session)
        return cwd, session or (transcript.stem if transcript else None), transcript

    @staticmethod
    def resume_command(session=None):
        command = [ClaudeCode.executable]
        if session:
            result = subprocess.run(command + ["--help"], capture_output=True, text=True, timeout=10)
            if result.returncode or "--resume" not in result.stdout:
                raise ValueError("This Claude CLI does not advertise --resume. Use back --launch for a new session.")
            command += ["--resume", session]
        return command


def _text_of(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content
                     if isinstance(block, dict) and block.get("type") == "text"
                     and isinstance(block.get("text"), str))


def transcript_events(path):
    if not path:
        return
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(event, dict) and not event.get("isMeta") and not event.get("isSidechain"):
                    yield event
    except OSError:
        return


def parse_transcript(path):
    info = {"first_request": "", "recent_requests": [], "last_answer": "",
            "files_changed": [], "commands": [], "todos": []}
    requests, commands = deque(maxlen=5), deque(maxlen=8)
    files = OrderedDict()
    for ev in transcript_events(path):
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        role, content = msg.get("role") or ev.get("type"), msg.get("content")
        if role == "user":
            is_tool_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            text = _text_of(content).strip()
            if text and not is_tool_result and not text.startswith("<") and "[Request interrupted" not in text:
                info["first_request"] = info["first_request"] or text
                requests.append(text)
        elif role == "assistant":
            text = _text_of(content).strip()
            if text and not text.startswith("API Error") and "usage limit" not in text.lower():
                info["last_answer"] = text
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name, inp = b.get("name"), b.get("input")
                if not isinstance(inp, dict):
                    continue
                file_path, command = inp.get("file_path"), inp.get("command")
                if name in ("Edit", "Write", "MultiEdit", "NotebookEdit") and isinstance(file_path, str) and file_path:
                    files[file_path] = None
                    files.move_to_end(file_path)
                    if len(files) > 25:
                        files.popitem(last=False)
                elif name in ("Bash", "PowerShell") and isinstance(command, str) and command:
                    commands.append(command)
                elif name == "TodoWrite" and isinstance(inp.get("todos"), list):
                    info["todos"] = inp["todos"]
    info["recent_requests"] = list(requests)
    info["files_changed"] = list(files)
    info["commands"] = list(commands)
    return info


def git_summary(cwd):
    if not shutil.which("git"):
        return ""
    try:
        kw = dict(cwd=str(cwd), capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
        st = subprocess.run(["git", "status", "--short", "--branch"], **kw)
        if st.returncode != 0:
            return ""
        diff = subprocess.run(["git", "diff", "--stat"], **kw)
        return (st.stdout.strip() + ("\n\n" + diff.stdout.strip() if diff.stdout.strip() else "")).strip()
    except Exception:
        return ""


# ------------------------------------------------------------------- handoff file
def checkpoint(cwd, session=None):
    folder = Path(cwd).resolve() / HANDOFF_DIR
    if session:
        key = hashlib.sha256(session.encode()).hexdigest()[:24]
        return folder / "sessions" / key / HANDOFF_NAME
    return folder / HANDOFF_NAME


def build_handoff(cwd, transcript=None, reason="manual", resets_at=None, session=None):
    cwd = Path(cwd).resolve()
    transcript = select_transcript(cwd, transcript, session)
    info = parse_transcript(transcript)
    clip = lambda s, n: (s[:n] + " …") if len(s) > n else s
    out = checkpoint(cwd, session)
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
         f"- **Project folder:** `{cwd}`",
         f"- **Source session:** {session or 'unidentified'}",
         f"- **Transcript:** {'available' if transcript else 'unavailable'}", "",
         "## Original goal", clip(info["first_request"] or "(not found in transcript – ask the user)", 2000), "",
         "## Most recent requests (oldest → newest)"]
    L += [f"{i}. {clip(r, 600)}" for i, r in enumerate(info["recent_requests"], 1)] or ["- (none)"]
    L += ["", "## Last recorded agent update (including reported tests and next steps)", clip(info["last_answer"] or "(no answer recorded)", 2500), ""]
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
    save_text(out, "\n".join(L))
    log(f"handoff written: {out} (reason={reason}, transcript={transcript})")
    return out


class Antigravity:
    @staticmethod
    def command(cfg):
        command = [cfg["target_cmd"]]
        if cfg.get("target_model"):
            command += ["--model", cfg["target_model"]]
        return command

    @classmethod
    def interactive(cls, cfg, cwd, path):
        relative = path.relative_to(Path(cwd).resolve()).as_posix()
        prompt = f"Read {relative} and continue the task. Add short notes under Progress since handoff."
        return cls.command(cfg) + ["-i", prompt]


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
        cwd, session, transcript = ClaudeCode.context(data)
        model = (data.get("model") or {}).get("display_name") or "Claude"
        if pct is not None:
            save_json(STATE_FILE, {"five_hour_pct": pct, "resets_at": resets, "updated": time.time()})
            if pct >= cfg["prepare_at"]:
                with session_state(cwd, session) as local:
                    if local.get("prepared_for") != (resets or "unknown"):
                        build_handoff(cwd, transcript, reason=f"5-hour usage at {pct:.0f}%", resets_at=resets,
                                      session=session)
                        local["prepared_for"] = resets or "unknown"
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


def cmd_on_limit(_args):
    data = read_stdin_json()
    if (data.get("error_type") or data.get("error")) != "rate_limit":
        return
    try:
        cfg = config()
        cwd, session, transcript = ClaudeCode.context(data)
        resets = load_json(STATE_FILE, {}).get("resets_at")
        with session_state(cwd, session) as state:
            if time.time() - state.get("last_switch", 0) < SWITCH_COOLDOWN:
                return
            path = build_handoff(cwd, transcript, "Claude usage limit reached (rate_limit)", resets, session)
            msg = "Handoff saved."
            if cfg["mode"] == "auto":
                if open_in_new_terminal(Antigravity.interactive(cfg, cwd, path), cwd, cfg):
                    if cfg["dry_run"]:
                        msg += " Dry run: Antigravity launch simulated."
                    else:
                        state["last_switch"] = time.time()
                        msg += " Antigravity launch requested."
                else:
                    msg += " Antigravity launch failed. Retry with relay.py launch."
            if not cfg["dry_run"]:
                notify("Claude Relay", msg)
            print(json.dumps({"systemMessage": f"Claude Relay: {msg} ({path})"}))
    except Exception as error:
        log(f"on-limit error: {error}")


def selected_checkpoint(cwd, session):
    if not session:
        default = checkpoint(cwd)
        candidates = list((Path(cwd) / HANDOFF_DIR / "sessions").glob("*/" + HANDOFF_NAME))
        if default.exists():
            candidates.append(default)
        if len(candidates) == 1:
            return candidates[0].resolve()
        if len(candidates) > 1:
            raise ValueError("Multiple checkpoints found. Specify --session SESSION_ID or --session manual.")
    return checkpoint(cwd, None if session == "manual" else session)


def cmd_handoff(args):
    cwd = args.cwd or os.getcwd()
    session = None if args.session == "manual" else args.session
    transcript = select_transcript(cwd, args.transcript, session)
    with session_state(cwd, session):
        path = build_handoff(cwd, transcript, session=session)
    print(f"Handoff written: {path}")
    if args.launch:
        request_launch(cwd, path)


def cmd_launch(args):
    cwd = args.cwd or os.getcwd()
    path = selected_checkpoint(cwd, args.session)
    if not path.is_file():
        raise ValueError("No handoff available. Run relay.py handoff first.")
    request_launch(cwd, path)


def request_launch(cwd, path):
    cfg = config()
    if not open_in_new_terminal(Antigravity.interactive(cfg, cwd, path), cwd, cfg):
        sys.exit(2)
    print("Antigravity launch requested.")


def cmd_preview(args):
    path = selected_checkpoint(args.cwd or os.getcwd(), args.session)
    if not path.is_file():
        raise ValueError(f"No checkpoint at {path}")
    print(path.read_text(encoding="utf-8"))


def source_session(path):
    return next((line.split("** ", 1)[1] for line in path.read_text(encoding="utf-8").splitlines()
                 if line.startswith("- **Source session:** ")), None)


def cmd_list(args):
    root = Path(args.cwd or os.getcwd()) / HANDOFF_DIR
    for path in sorted(root.glob("sessions/*/" + HANDOFF_NAME)):
        print(f"{source_session(path) or 'unknown'}  {path}")
    if (root / HANDOFF_NAME).exists():
        print(f"manual  {root / HANDOFF_NAME}")


ASK_DIR = "answers"
ASK_TIMEOUT = 600  # seconds


def run_antigravity_headless(cfg, prompt, cwd, allow_edits=False, timeout=ASK_TIMEOUT):
    """Run `agy -p` (headless: one prompt, print the answer, exit). Returns (ok, text)."""
    exe = shutil.which(cfg["target_cmd"])
    if not exe:
        return False, (f"'{cfg['target_cmd']}' (Antigravity CLI) was not found on PATH. "
                       "Install it, sign in once by running `agy`, then try again.")
    argv = [exe] + Antigravity.command(cfg)[1:]
    pf = None
    # Antigravity controls the permissions of plan mode.
    via_cmd = IS_WIN and exe.lower().endswith((".cmd", ".bat"))
    if via_cmd or (IS_WIN and "\n" in prompt) or os.environ.get("RELAY_PROMPT_FILE") == "1":
        # cmd.exe cuts arguments at line breaks and expands % ^ & |, so a long/multi-line request is
        # written to a file and Antigravity gets a short single-line instruction to read it.
        pf = Path(cwd) / HANDOFF_DIR / ("ask-prompt-" + uuid.uuid4().hex + ".md")
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(prompt, encoding="utf-8")
        prompt = (f"Read the file .relay/{pf.name} in this folder and do exactly what it asks. "
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
    finally:
        if pf is not None and pf.exists():
            pf.unlink()
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        return False, f"Antigravity exited with code {r.returncode}: {(r.stderr or '').strip()[:1500]}\n{out}"
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
    print(f"Asking Antigravity ({'may edit files' if args.allow_edits else 'plan mode'})...", file=sys.stderr)
    ok, answer = run_antigravity_headless(cfg, prompt, cwd, args.allow_edits, args.timeout)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = cwd / HANDOFF_DIR / ASK_DIR / f"{stamp}-{uuid.uuid4().hex}.md"
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
    cfg, cwd = config(), Path(args.cwd or os.getcwd()).resolve()
    handoff = selected_checkpoint(cwd, args.session)
    if not handoff.exists():
        print("No .relay/HANDOFF.md in this folder - nothing to hand back.")
        return
    if args.resume and (not args.launch or not args.session or args.session == "manual"):
        raise ValueError("--resume requires --launch and an explicit Claude --session ID.")
    command = ClaudeCode.resume_command(args.session if args.resume else None) if args.launch else None
    g = git_summary(cwd)
    checkpoint_session = args.session
    if checkpoint_session is None and handoff.parent.name != HANDOFF_DIR:
        checkpoint_session = source_session(handoff)
    with session_state(cwd, None if checkpoint_session == "manual" else checkpoint_session):
        with handoff.open("a", encoding="utf-8") as stream:
            stream.write(f"\n## Returned to Claude Code – {dt.datetime.now():%d %b %Y, %H:%M}\n")
            if g:
                stream.write(f"```\n{g}\n```\n")
    print(f"Handoff updated: {handoff}")
    if command:
        relative = handoff.relative_to(cwd).as_posix()
        command.append(f"Read {relative}, especially Progress since handoff, then continue.")
        if not open_in_new_terminal(command, cwd, cfg):
            sys.exit(2)


def cmd_simulate(args):
    """Feed Relay exactly what Claude Code sends, so the switch can be shown without using the limit."""
    cwd = Path(args.cwd or os.getcwd()).resolve()
    if args.live:
        simulate_hooks(cwd, dict(os.environ))
        return
    with tempfile.TemporaryDirectory(prefix="relay-demo-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        state = root / "state"
        save_json(state / "config.json", {**config(), "mode": "auto", "dry_run": True})
        env = dict(os.environ, RELAY_HOME=str(state), CLAUDE_CONFIG_DIR=str(root / "claude"),
                   RELAY_DRY_RUN="1")
        simulate_hooks(workspace, env)
    print("Simulation complete. The temporary checkpoint was removed.")


def simulate_hooks(cwd, env):
    cwd = str(cwd)
    session = "relay-demo-" + uuid.uuid4().hex
    reset = int(time.time()) + 2 * 3600
    me = [sys.executable, str(Path(__file__).resolve())]
    for pct in (45, 85, 92):
        payload = {"session_id": session, "model": {"display_name": "Claude Sonnet"}, "workspace": {"current_dir": cwd},
                   "rate_limits": {"five_hour": {"used_percentage": pct, "resets_at": reset}}}
        out = subprocess.run(me + ["statusline"], input=json.dumps(payload), capture_output=True,
                             text=True, encoding="utf-8", cwd=cwd, env=env)
        print(f"status line at {pct}%:  {out.stdout.strip()}")
    print("\nSimulating Claude Code's StopFailure hook (error_type = rate_limit) ...")
    out = subprocess.run(me + ["on-limit"], input=json.dumps(
        {"hook_event_name": "StopFailure", "error_type": "rate_limit", "cwd": cwd, "session_id": session}),
        capture_output=True, text=True, encoding="utf-8", cwd=cwd, env=env)
    print(out.stdout.strip() or "(hook ran)")
    print(f"\nHandoff file: {checkpoint(cwd, session)}")


def cmd_doctor(_args):
    cfg, ok_all = config(), True

    def check(label, ok, fix=""):
        nonlocal ok_all
        ok_all &= bool(ok)
        print(f"  [{'OK ' if ok else 'FIX'}] {label}" + ("" if ok else f"\n        → {fix}"))

    def version(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace")
            return ((r.stdout or r.stderr).strip() or "?").splitlines()[0] if r.returncode == 0 else None
        except Exception:
            return None

    print(f"Claude Relay {VERSION} - system check ({platform.system()} {platform.release()})\n")
    check(f"Python {platform.python_version()}", sys.version_info >= (3, 8), "Install Python 3.8 or newer")
    claude = shutil.which("claude")
    claude_version = version([claude, "--version"]) if claude else None
    check(f"Claude Code CLI: {claude_version or 'unavailable'}", claude_version,
          "Install Claude Code and sign in with your Claude Pro account")
    agy = shutil.which(cfg["target_cmd"])
    agy_version = version([agy, "--version"]) if agy else None
    check(f"Antigravity CLI ({cfg['target_cmd']}): {agy_version or 'unavailable'}", agy_version,
          "Install the Antigravity CLI, sign in, then run `agy` once")
    check(f"git: {'found' if shutil.which('git') else 'not found (optional)'}", True)
    s = read_settings()
    manifest = load_json(installation_manifest(), {})
    check("Status line installed", manifest and s.get("statusLine") == manifest["status"], "Run: relay.py install")
    check("StopFailure hook installed", manifest and manifest["hook"] in s.get("hooks", {}).get("StopFailure", []),
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
    settings = read_settings()
    manifest_path = installation_manifest()
    previous = load_json(manifest_path, {})
    status = {"type": "command", "command": run("statusline"), "padding": 0}
    hook = {"matcher": "rate_limit", "hooks": [
        {"type": "command", "command": run("on-limit"), "timeout": 60}]}
    old_status = settings.get("statusLine")
    original_status = previous.get("original_status") if old_status == previous.get("status") else old_status
    if old_status == status and not previous:
        original_status = load_json(CLAUDE_SETTINGS.with_name("settings.relay-backup.json"), {}).get("statusLine")
    settings["statusLine"] = status
    hooks = settings.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("StopFailure", [])
    owned_hook = previous.get("hook")
    if owned_hook in stop_hooks:
        stop_hooks.remove(owned_hook)
    if hook not in stop_hooks:
        stop_hooks.append(hook)
    manifest = {"status": status, "original_status": original_status, "hook": hook, "commands": {}}
    cmd_dir = CLAUDE_DIR / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    handoff_command = (
        "---\ndescription: Hand this task over to Google Antigravity (Claude Relay)\n---\n"
        "Hand the current task over to Google Antigravity:\n"
        f"1. Run with the Bash tool: {run('handoff')} --session ${{CLAUDE_SESSION_ID}}\n"
         "2. Open the printed checkpoint and add a short summary "
        "(max 8 bullets): what is done, what is in progress, the exact next step, and any gotchas.\n"
        f"3. Run with the Bash tool: {run('launch')} --session ${{CLAUDE_SESSION_ID}}\n"
        "4. Report the launch result.\n")
    ask_command = (
        "---\ndescription: Ask Google Antigravity a question or give it a sub-task, without leaving this session\n"
        "argument-hint: <question or sub-task>\n---\n"
        "Use Google Antigravity as a second opinion / helper, then continue here.\n"
        "Request: $ARGUMENTS\n\n"
        "1. Write the request above to a unique file under `.relay/` with the Write tool. Add any short context Antigravity needs "
        "(relevant file paths, what you already tried). If the request is empty, ask me what to send.\n"
        f"2. Run with the Bash tool (timeout 600000 ms): {run('ask --file PATH_TO_REQUEST')}\n"
        "3. Read Antigravity's answer, tell me the key points in a few lines, and say whether you agree. "
        "Do not apply any change it suggests until I confirm.\n")
    for name, content in {"handoff.md": handoff_command, "ask-antigravity.md": ask_command}.items():
        path = cmd_dir / name
        if path.exists() and path.read_text(encoding="utf-8") != previous.get("commands", {}).get(name):
            print(f"Kept existing command: {path}. Use relay.py directly for the v1.4 workflow.")
            continue
        save_text(path, content)
        manifest["commands"][name] = content
    save_json(manifest_path, manifest)
    save_json(CLAUDE_SETTINGS, settings)
    save_json(CONFIG_FILE, cfg)
    print(f"Installed Claude Relay {VERSION}. Mode: {cfg['mode']}, handoff prepared at {cfg['prepare_at']}%.")
    print(f"  Claude settings : {CLAUDE_SETTINGS}\n  Slash commands  : /handoff, /ask-antigravity\n  Relay config    : {CONFIG_FILE}")
    print("Restart Claude Code, then run: relay.py doctor")


def installation_manifest():
    return STATE_DIR / ("install-" + hashlib.sha256(str(CLAUDE_SETTINGS.resolve()).encode()).hexdigest() + ".json")


def read_settings():
    settings = load_json(CLAUDE_SETTINGS, None) if CLAUDE_SETTINGS.exists() else {}
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict):
        raise ValueError(f"Invalid Claude settings at {CLAUDE_SETTINGS}.")
    if not isinstance(settings.get("hooks", {}).get("StopFailure", []), list):
        raise ValueError("Claude StopFailure settings must be a list.")
    return settings


def cmd_uninstall(_args):
    manifest_path = installation_manifest()
    manifest = load_json(manifest_path, {})
    if not manifest:
        print("No v1.4 Relay installation recorded.")
        return
    settings = read_settings()
    if settings.get("statusLine") == manifest["status"]:
        if manifest["original_status"] is None:
            settings.pop("statusLine", None)
        else:
            settings["statusLine"] = manifest["original_status"]
    hooks = settings.get("hooks", {})
    stop_hooks = hooks.get("StopFailure", [])
    if manifest["hook"] in stop_hooks:
        stop_hooks.remove(manifest["hook"])
        if not stop_hooks:
            hooks.pop("StopFailure")
    save_json(CLAUDE_SETTINGS, settings)
    for name, content in manifest["commands"].items():
        path = CLAUDE_DIR / "commands" / name
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            path.unlink()
    manifest_path.unlink()
    print("Claude Relay removed from Claude Code. Restart Claude Code.")


def cmd_purge(args):
    if args.older_than_days < 1:
        raise ValueError("--older-than-days must be at least 1.")
    cutoff = time.time() - args.older_than_days * 86400
    folder = Path(args.cwd or os.getcwd()) / HANDOFF_DIR / ASK_DIR
    for path in folder.glob("????-*.md"):
        if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
            path.unlink()
            print(f"Removed {path}")


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
    b.add_argument("--resume", action="store_true")
    preview = sub.add_parser("preview")
    preview.add_argument("--cwd")
    for parser in (h, l, b, preview):
        parser.add_argument("--session")
    listing = sub.add_parser("checkpoints")
    listing.add_argument("--cwd")
    cleanup = sub.add_parser("purge-answers")
    cleanup.add_argument("--cwd")
    cleanup.add_argument("--older-than-days", type=int, required=True)
    args = p.parse_args(argv)
    try:
        {"install": cmd_install, "uninstall": cmd_uninstall, "statusline": cmd_statusline, "on-limit": cmd_on_limit,
     "status": cmd_status, "doctor": cmd_doctor, "handoff": cmd_handoff, "launch": cmd_launch, "ask": cmd_ask,
     "back": cmd_back, "simulate": cmd_simulate, "preview": cmd_preview, "checkpoints": cmd_list,
         "purge-answers": cmd_purge}[args.cmd](args)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f"Relay: {error}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
