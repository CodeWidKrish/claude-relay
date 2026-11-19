#!/usr/bin/env python3
"""
Claude Relay - continue your Claude Code or Codex CLI work in Google Antigravity when the
5-hour usage limit is reached (automatically or manually), then come back when it resets.

Pure Python standard library (3.8+). Windows, macOS and Linux.

Commands
  relay.py doctor                 check that everything needed is installed
  relay.py install [--mode auto|manual] [--threshold 90] [--model "Claude Sonnet 4.6"] [--codex]
  relay.py uninstall              remove Relay from Claude Code (and Codex, if installed there)
  relay.py statusline             used by Claude Code's status line (JSON on stdin)
  relay.py on-limit               used by Claude Code's StopFailure hook (JSON on stdin)
  relay.py codex-hook             used by Codex CLI's Stop and PostModelRequest hooks (JSON on stdin)
  relay.py handoff [--launch] [--agent claude|codex]   manual switch: write the handoff (and open Antigravity)
  relay.py launch                 open Antigravity on the existing handoff
  relay.py ask "question" | --file F   ask Antigravity from inside a Claude Code session
  relay.py back [--launch] [--to claude|codex]   after the reset: hand the work (and what changed) back
  relay.py watch --agent codex    poll Codex usage and switch at the limit without hooks
  relay.py simulate [--live]      demo the automatic switch without using up your limit
  relay.py status [--json] [--agent codex]   usage, reset countdown, checkpoints in this project, recent log
  relay.py config [KEY [VALUE]]   show or change a setting (mode, prepare_at, target_model, target_mode, ...)
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
import re
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

VERSION = "1.6.0"
IS_WIN = platform.system() == "Windows"
HOME = Path.home()
STATE_DIR = Path(os.environ.get("RELAY_HOME", HOME / ".claude-relay"))
STATE_FILE = STATE_DIR / "state.json"
CONFIG_FILE = STATE_DIR / "config.json"
LOG_FILE = STATE_DIR / "relay.log"
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude"))
CLAUDE_SETTINGS = CLAUDE_DIR / "settings.json"
CLAUDE_PROJECTS = CLAUDE_DIR / "projects"
CODEX_DIR = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
HANDOFF_DIR, HANDOFF_NAME = ".relay", "HANDOFF.md"
SWITCH_COOLDOWN = 15 * 60  # seconds; StopFailure can fire many times for one limit
CODEX_CHECK_INTERVAL = 45  # seconds between Codex usage reads triggered by the Stop hook
AGENTS = {"claude": ("Claude Code", "Claude"), "codex": ("Codex CLI", "Codex")}

DEFAULT_CONFIG = {
    "mode": "auto",          # auto: switch when the limit is hit; manual: only on /handoff
    "warn_at": 80,           # status line turns yellow
    "prepare_at": 90,        # handoff written in advance (while Claude can still answer)
    "target_cmd": "agy",     # Antigravity CLI
    "target_model": "",      # e.g. "Claude Sonnet 4.6" (exact name from `agy models`)
    "target_mode": "default",  # Antigravity mode after a handoff: default, accept-edits, auto or plan
    "color": True,
    "dry_run": False,        # print launch commands instead of running them
    "redact": True,          # mask API keys, tokens and passwords in checkpoints
    "diff_lines": 200,       # max lines of uncommitted diff copied into a checkpoint (0 = none)
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
        try:
            state = load_json(path, {})
            yield state
            save_json(path, state)
        finally:
            if IS_WIN:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def clean_session(value):
    """A slash command whose ${CLAUDE_SESSION_ID} was not substituted must not become a bogus session."""
    if not value or "${" in value or value.startswith("$"):
        return None
    return value


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


def _codex_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content
                     if isinstance(block, dict) and block.get("type") in ("input_text", "output_text", "text")
                     and isinstance(block.get("text"), str))


def _patch_files(patch):
    return re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$", patch or "", re.M)


def parse_codex_transcript(path, last_message=None):
    """Same summary as parse_transcript, from a Codex session file (JSON lines, one record per line)."""
    info = {"first_request": "", "recent_requests": [], "last_answer": "",
            "files_changed": [], "commands": [], "todos": []}
    requests, commands, files = deque(maxlen=5), deque(maxlen=8), OrderedDict()

    def touch(name):
        files[name] = None
        files.move_to_end(name)
        if len(files) > 25:
            files.popitem(last=False)

    for ev in transcript_events(path):
        item = ev.get("payload") if isinstance(ev.get("payload"), dict) else ev
        kind = item.get("type")
        if kind == "message":
            text = _codex_text(item.get("content")).strip()
            if item.get("role") == "user" and text and not text.startswith("<"):
                info["first_request"] = info["first_request"] or text
                requests.append(text)
            elif item.get("role") == "assistant" and text:
                info["last_answer"] = text
        elif kind in ("function_call", "custom_tool_call"):
            raw = item.get("arguments") if kind == "function_call" else item.get("input")
            try:
                args = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("{") else raw
            except ValueError:
                args = raw
            name = item.get("name")
            if name in ("shell", "shell_command", "exec_command", "local_shell") and isinstance(args, dict):
                cmd = args.get("command") or args.get("cmd")
                if isinstance(cmd, list):
                    cmd = " ".join(str(c) for c in cmd)
                if isinstance(cmd, str) and cmd:
                    commands.append(cmd)
            elif name == "apply_patch":
                for f in _patch_files(args.get("input") if isinstance(args, dict) else args):
                    touch(f)
            elif name == "update_plan" and isinstance(args, dict) and isinstance(args.get("plan"), list):
                info["todos"] = [{"content": s.get("step", ""), "status": s.get("status", "")}
                                 for s in args["plan"] if isinstance(s, dict)]
    if last_message and isinstance(last_message, str):
        info["last_answer"] = last_message.strip()
    info["recent_requests"] = list(requests)
    info["files_changed"] = list(files)
    info["commands"] = list(commands)
    return info


class CodexCLI:
    """OpenAI Codex CLI: live usage through its app-server (JSON-RPC over stdio) and interactive launch."""
    executable = "codex"

    @classmethod
    def argv(cls, *extra):
        exe = shutil.which(cls.executable)
        if not exe:
            return None
        argv = [exe, *extra]
        return ["cmd.exe", "/c"] + argv if IS_WIN and exe.lower().endswith((".cmd", ".bat")) else argv

    @classmethod
    def read_usage(cls, timeout=30):
        """Return the 5-hour and weekly usage of the signed-in Codex account, or None when unavailable."""
        simulated = os.environ.get("RELAY_SIMULATED_CODEX_USAGE")  # set only by `relay.py simulate --agent codex`
        if simulated:
            return json.loads(simulated)
        argv = cls.argv("app-server")
        if not argv:
            return None
        import threading
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError:
            return None
        killer = threading.Timer(timeout, proc.kill)
        killer.start()

        def send(message):
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()

        def reply(request_id):
            for line in proc.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == request_id:
                    return message
            return None

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"clientInfo": {"name": "claude-relay", "version": VERSION}}})
            if not reply(1):
                return None
            send({"jsonrpc": "2.0", "method": "initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": None})
            answer = reply(2) or {}
        except (OSError, ValueError):
            return None
        finally:
            killer.cancel()
            cls._stop(proc)
        result = answer.get("result")
        if not isinstance(result, dict):
            return None
        limits = result.get("rateLimits") or {}
        primary, weekly = limits.get("primary") or {}, limits.get("secondary") or {}
        if to_float(primary.get("usedPercent")) is None:
            return None
        return {"pct": to_float(primary.get("usedPercent")), "resets": to_epoch(primary.get("resetsAt")),
                "weekly_pct": to_float(weekly.get("usedPercent")), "weekly_resets": to_epoch(weekly.get("resetsAt")),
                "plan": limits.get("planType"), "reached": limits.get("rateLimitReachedType"),
                "allowed": result.get("ordinaryUsageAllowed")}

    @staticmethod
    def _stop(proc):
        """Close stdin so the app-server exits on its own; force-stop the whole process tree if it doesn't."""
        with contextlib.suppress(OSError, ValueError):
            proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if IS_WIN:  # a .cmd launcher's child would survive killing cmd.exe alone
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
            else:
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        with contextlib.suppress(OSError, ValueError):
            proc.stdout.close()

    @staticmethod
    def limit_reached(usage):
        return bool(usage and (usage.get("reached") or usage.get("allowed") is False or (usage.get("pct") or 0) >= 100))

    @classmethod
    def interactive(cls, prompt):
        return [cls.executable, prompt]


SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
     "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b(sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,})"), "[REDACTED]"),
    (re.compile(r"\b((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[REDACTED]"),
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), "[REDACTED]"),
    (re.compile(r"\b(AIza[0-9A-Za-z_-]{35})"), "[REDACTED]"),
    (re.compile(r"\b(xox[abprs]-[A-Za-z0-9-]{10,})"), "[REDACTED]"),
    (re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"), "[REDACTED]"),
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\1[REDACTED]"),
    (re.compile(r"([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+@"), r"\1[REDACTED]@"),
    # NAME=value where NAME ends in a credential word (OPENAI_API_KEY=..., password: ...), not max_tokens=1024.
    (re.compile(r"(?i)\b((?:[a-z0-9]+[_-])*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
                r"|private[_-]?key|client[_-]?secret)\s*[:=]\s*[\"']?)(?![0-9]+\b)(?!\[REDACTED)"
                r"([^\s\"',;]{4,})"), r"\1[REDACTED]"),
]


def redact(text):
    """Mask common credentials so a checkpoint can be shared with another agent safely."""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def git(cwd, *args, timeout=10):
    """Run git in cwd; returns stdout ('' on any failure or when git is missing)."""
    if not shutil.which("git"):
        return ""
    try:
        r = subprocess.run(["git"] + list(args), cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def git_diff_excerpt(cwd, max_lines):
    """Uncommitted changes (staged + unstaged), cut to max_lines so the checkpoint stays readable."""
    if max_lines <= 0:
        return ""
    lines = git(cwd, "diff", "HEAD", "--no-color", "--unified=2").splitlines()
    if not lines:
        lines = git(cwd, "diff", "--no-color", "--unified=2").splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines: run git diff to see everything)"]
    return "\n".join(lines)


def git_summary(cwd):
    if not shutil.which("git"):
        return ""
    try:
        kw = dict(cwd=str(cwd), capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
        st = subprocess.run(["git", "status", "--short", "--branch"], **kw)
        if st.returncode != 0:
            return ""
        diff = subprocess.run(["git", "diff", "--stat"], **kw)
        stat = diff.stdout.strip("\n").rstrip()  # keep the leading space git uses to align the stat
        return (st.stdout.strip() + ("\n\n" + stat if stat else "")).strip("\n")
    except Exception:
        return ""


# ------------------------------------------------------------------- handoff file
RELAY_GITIGNORE = ("# Created by Claude Relay. Checkpoints and answers can contain prompts, commands and paths,\n"
                   "# so this folder is never committed.\n*\n")


def relay_folder(cwd):
    """Create <project>/.relay with its own .gitignore, so nothing Relay writes can be committed by accident."""
    folder = Path(cwd).resolve() / HANDOFF_DIR
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_text(RELAY_GITIGNORE, encoding="utf-8")
    return folder


def checkpoint(cwd, session=None):
    folder = Path(cwd).resolve() / HANDOFF_DIR
    if session:
        key = hashlib.sha256(session.encode()).hexdigest()[:24]
        return folder / "sessions" / key / HANDOFF_NAME
    return folder / HANDOFF_NAME


def build_handoff(cwd, transcript=None, reason="manual", resets_at=None, session=None, source="claude",
                  last_message=None):
    cwd = Path(cwd).resolve()
    agent, short = AGENTS.get(source, AGENTS["claude"])
    if source == "codex":
        transcript = Path(transcript) if transcript and Path(transcript).is_file() else None
        info = parse_codex_transcript(transcript, last_message)
    else:
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
         f"- **From → to:** {agent} → Antigravity",
         f"- **{short} limit resets at:** {fmt_time(resets_at)}",
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
        head = git(cwd, "rev-parse", "HEAD").strip()
        if head:
            L += ["", f"- **Git commit at handoff:** {head}"]
    cfg = config()
    excerpt = git_diff_excerpt(cwd, int(cfg.get("diff_lines") or 0))
    if excerpt:
        L += ["", "## Uncommitted changes (excerpt)", "```diff", excerpt, "```"]
    L += ["", "## Instructions for the next agent",
          "1. Read this file and the files listed above before changing anything.",
          "2. Continue from 'Last recorded agent update' and the task list. Do not redo finished work.",
          "3. As you work, add short notes to 'Progress since handoff' below, so the work can move back.",
          "", "## Progress since handoff", keep_notes or "- (next agent: add notes here)", ""]
    relay_folder(cwd)
    text = "\n".join(L)
    save_text(out, redact(text) if cfg.get("redact", True) else text)
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
        # One line, no double quotes: it has to survive cmd.exe, PowerShell and POSIX shells unchanged.
        prompt = (f"Read {relative} and continue the coding task it describes, starting from its next steps and task "
                  "list. Work on the project itself: that file is a handoff note written by Claude Relay, so you "
                  "do not need to inspect Relay's own code or its setup notes. When you stop, add short notes under "
                  "Progress since handoff in that file.")
        mode = str(cfg.get("target_mode") or "default")
        return cls.command(cfg) + (["--mode", mode] if mode != "default" else []) + ["-i", prompt]


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
                window = resets or "unknown-" + str(int(time.time() // (5 * 3600)))
                with session_state(cwd, session) as local:
                    if local.get("prepared_for") != window:
                        build_handoff(cwd, transcript, reason=f"5-hour usage at {pct:.0f}%", resets_at=resets,
                                      session=session)
                        local["prepared_for"] = window
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


def codex_state_file():
    return STATE_DIR / "codex-state.json"


def usage_line(usage):
    pct = usage["pct"]
    bar = "█" * int(min(max(pct, 0), 100) // 10)
    week = usage.get("weekly_pct")
    return (f"{dt.datetime.now():%H:%M}  Codex 5h {bar}{'░' * (10 - len(bar))} {pct:.0f}% "
            f"resets {fmt_time(usage.get('resets'))}" + (f" | week {week:.0f}%" if week is not None else "")
            + (f" | {usage['reached']}" if usage.get("reached") else ""))


def codex_check(cwd, session=None, transcript=None, last_message=None, trigger="hook", usage=None):
    """Prepare a checkpoint at prepare_at and, in auto mode, open Antigravity once the Codex limit is reached."""
    cfg = config()
    usage = usage or CodexCLI.read_usage()
    if not usage:
        log(f"codex {trigger}: usage unavailable")
        return ""
    save_json(codex_state_file(), dict(usage, updated=time.time()))
    pct, resets, reached = usage["pct"], usage.get("resets"), CodexCLI.limit_reached(usage)
    window = resets or "unknown-" + str(int(time.time() // (5 * 3600)))
    with session_state(cwd, "codex:" + (session or "manual")) as state:
        if reached and cfg["mode"] == "auto":
            if time.time() - state.get("last_switch", 0) < SWITCH_COOLDOWN:
                return ""
            label = usage.get("reached") or f"{pct:.0f}%"
            path = build_handoff(cwd, transcript, f"Codex usage limit reached ({label})", resets, session, "codex",
                                 last_message)
            state["prepared_for"] = window
            if open_in_new_terminal(Antigravity.interactive(cfg, cwd, path), cwd, cfg):
                if not cfg["dry_run"]:
                    state["last_switch"] = time.time()
                msg = "Codex limit reached. " + ("Dry run: Antigravity launch simulated." if cfg["dry_run"]
                                                 else "Antigravity launch requested.")
            else:
                msg = "Codex limit reached. Antigravity launch failed. Retry with relay.py launch."
            if not cfg["dry_run"]:
                notify("Claude Relay", msg)
            log(f"codex {trigger}: {msg}")
            return f"{msg} ({path})"
        if (pct >= cfg["prepare_at"] or reached) and state.get("prepared_for") != window:
            path = build_handoff(cwd, transcript, f"Codex 5-hour usage at {pct:.0f}%", resets, session, "codex",
                                 last_message)
            state["prepared_for"] = window
            log(f"codex {trigger}: checkpoint prepared at {pct:.0f}%")
            return f"Codex usage at {pct:.0f}%: checkpoint prepared ({path})."
    return ""


def cmd_codex_hook(_args):
    """Codex CLI hook entry point (Stop after every turn, PostModelRequest after every model request)."""
    data = read_stdin_json()
    event = str(data.get("hook_event_name") or "").lower()
    simulated = bool(os.environ.get("RELAY_SIMULATED_CODEX_USAGE"))
    try:
        if event == "postmodelrequest" and data.get("status") != "failed":
            return  # the common case: return immediately
        if event == "stop" and not simulated:
            cached = load_json(codex_state_file(), {})
            fresh = time.time() - float(cached.get("updated") or 0) < CODEX_CHECK_INTERVAL
            if fresh and not CodexCLI.limit_reached(cached) and (to_float(cached.get("pct")) or 0) < config()["prepare_at"]:
                return  # usage was read moments ago and is well below the threshold
        message = codex_check(data.get("cwd") or os.getcwd(), clean_session(data.get("session_id")),
                              data.get("transcript_path"), data.get("last_assistant_message"), event or "hook")
        if simulated and message:
            print(message, file=sys.stderr)
    except Exception as error:  # a hook must never break the Codex session
        log(f"codex-hook error: {error}")
    finally:
        if event == "stop":
            print("{}")  # Codex expects JSON from a Stop hook


def cmd_watch(args):
    if args.agent != "codex":
        raise ValueError("watch supports --agent codex. Claude Code reports usage through Relay's status line.")
    cwd = str(Path(args.cwd or os.getcwd()).resolve())
    session = clean_session(args.session)
    print(f"Watching Codex usage for {cwd} every {args.interval}s. Press Ctrl+C to stop.")
    try:
        while True:
            usage = CodexCLI.read_usage()
            if usage:
                print(usage_line(usage), flush=True)
                message = codex_check(cwd, session, None, None, "watch", usage)
                if message:
                    print("  " + message, flush=True)
            else:
                print("Codex usage unavailable. Is the Codex CLI installed and signed in?", flush=True)
            if args.once:
                return
            time.sleep(max(15, args.interval))
    except KeyboardInterrupt:
        print("Stopped watching.")


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
    if args.agent == "codex":
        with session_state(cwd, "codex:" + (session or "manual")):
            path = build_handoff(cwd, args.transcript, session=session, source="codex")
    else:
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
        pf = relay_folder(cwd) / ("ask-prompt-" + uuid.uuid4().hex + ".md")
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
    out, err = (r.stdout or "").strip(), (r.stderr or "").strip()[:1500]
    if r.returncode != 0:
        return False, f"Antigravity exited with code {r.returncode}: {err}\n{out}"
    if not out:
        # Headless mode cannot prompt for permissions: a denied tool ends the turn with no answer and exit 0.
        return False, "Antigravity returned an empty answer." + (f"\n{err}" if err else "")
    return True, out


ASK_FILE_LIMIT, ASK_TOTAL_LIMIT, ASK_MAX_FILES = 40_000, 120_000, 8
NEVER_ATTACH = re.compile(r"(?i)(^|[\\/])(\.env[^\\/]*|.*\.(pem|key|p12|pfx|keystore)|id_(rsa|dsa|ecdsa|ed25519)[^\\/]*"
                          r"|credentials[^\\/]*|secrets?\.[a-z]+)$")


def referenced_files(question, cwd):
    """Project files named in the request, read here so Antigravity needs no tools to see them."""
    cwd, found, total = Path(cwd).resolve(), [], 0
    for token in re.findall(r"[\w./\\-]+\.[A-Za-z0-9]{1,8}\b", question):
        token = token[2:] if token.startswith(("./", ".\\")) else token
        try:
            path = (cwd / token).resolve()
            relative = path.relative_to(cwd).as_posix()
        except (OSError, ValueError):
            continue
        if (relative in [name for name, _ in found] or relative.split("/")[0] in (HANDOFF_DIR, ".git")
                or NEVER_ATTACH.search(relative) or not path.is_file() or path.stat().st_size > ASK_FILE_LIMIT):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if total + len(text) > ASK_TOTAL_LIMIT:
            break
        found.append((relative, redact(text)))
        total += len(text)
        if len(found) == ASK_MAX_FILES:
            break
    return found


def ask_prompt(question, files, allow_edits, no_tools=False):
    lines = ["You are helping another coding agent (Claude Code) that is working in this project folder.",
             "Do not modify any files; answer only." if not allow_edits else "",
             "Shell commands cannot be approved in this headless run. Read files with your file-reading tools, "
             "or use the file contents attached below." if not no_tools else
             "Do not call any tools. Answer only from the request and the file contents attached below.",
             "Answer the request clearly and concisely.", "", "REQUEST:", question]
    if files:
        lines += ["", "ATTACHED FILES (current contents, already read for you):"]
        for name, text in files:
            lines += ["", f"--- {name} ---", text.rstrip(), f"--- end of {name} ---"]
    return "\n".join(line for line in lines if line is not None)


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
    files = referenced_files(question, cwd)
    prompt = ask_prompt(question, files, args.allow_edits)
    if cfg.get("dry_run"):
        attached = ", ".join(name for name, _ in files) or "none"
        print(f"[dry-run] would run Antigravity headless in {cwd} with: {question[:200]}\n"
              f"[dry-run] files attached: {attached}")
        return
    print(f"Asking Antigravity ({'may edit files' if args.allow_edits else 'plan mode'}"
          f"{', ' + str(len(files)) + ' file(s) attached' if files else ''})...", file=sys.stderr)
    ok, answer = run_antigravity_headless(cfg, prompt, cwd, args.allow_edits, args.timeout)
    if not ok and "permission" in answer.lower():
        # Headless mode cannot approve tools. Ask once more for an answer from the attached content alone.
        print("Antigravity needed a tool it cannot use headless; retrying without tools...", file=sys.stderr)
        ok, answer = run_antigravity_headless(cfg, ask_prompt(question, files, args.allow_edits, no_tools=True),
                                              cwd, args.allow_edits, args.timeout)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = cwd / HANDOFF_DIR / ASK_DIR / f"{stamp}-{uuid.uuid4().hex}.md"
    try:
        relay_folder(cwd)
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


def changes_since_handoff(cwd, handoff_text):
    """Summarise commits and file changes made after the checkpoint was written (by Antigravity or anyone)."""
    marker = next((line.split("** ", 1)[1].strip() for line in handoff_text.splitlines()
                   if line.startswith("- **Git commit at handoff:** ")), "")
    if not re.fullmatch(r"[0-9a-f]{7,40}", marker or ""):
        return ""
    commits = git(cwd, "log", "--oneline", "--no-decorate", "-n", "20", marker + "..HEAD").strip()
    stat = git(cwd, "diff", "--stat", marker).strip("\n").rstrip()
    untracked = git(cwd, "ls-files", "--others", "--exclude-standard").strip()
    parts = ["### Commits since handoff", "```", commits or "(none)", "```",
             "### Files changed since handoff (committed and uncommitted)", "```", stat or "(none)", "```"]
    if untracked:
        parts += ["### New untracked files", "```", untracked, "```"]
    return "\n".join(parts) + "\n"


def cmd_back(args):
    cfg, cwd = config(), Path(args.cwd or os.getcwd()).resolve()
    handoff = selected_checkpoint(cwd, args.session)
    if not handoff.exists():
        print(f"No checkpoint at {handoff} - nothing to hand back. Run relay.py checkpoints to list them.")
        return
    text = handoff.read_text(encoding="utf-8", errors="replace")
    target = getattr(args, "to", None) or ("codex" if "**From → to:** Codex CLI" in text else "claude")
    agent = AGENTS[target][0]
    if args.resume and (target != "claude" or not args.launch or not args.session or args.session == "manual"):
        raise ValueError("--resume requires --launch, --to claude and an explicit Claude --session ID.")
    command = None
    if args.launch:
        command = ([CodexCLI.executable] if target == "codex"
                   else ClaudeCode.resume_command(args.session if args.resume else None))
    g = git_summary(cwd)
    checkpoint_session = args.session
    if checkpoint_session is None and handoff.parent.name != HANDOFF_DIR:
        checkpoint_session = source_session(handoff)
    since = changes_since_handoff(cwd, text)
    with session_state(cwd, None if checkpoint_session == "manual" else checkpoint_session):
        with handoff.open("a", encoding="utf-8") as stream:
            stream.write(f"\n## Returned to {agent} – {dt.datetime.now():%d %b %Y, %H:%M}\n")
            if since:
                stream.write(since)
            elif g:
                stream.write(f"```\n{g}\n```\n")
    print(f"Handoff updated: {handoff}")
    if since:
        for line in since.splitlines():  # same summary, without the Markdown fences
            if line != "```":
                print(line[4:] + ":" if line.startswith("### ") else "  " + line)
    if command:
        relative = handoff.relative_to(cwd).as_posix()
        command.append(f"Read {relative}, especially Progress since handoff and the Returned to {agent} "
                       "summary of what changed, then continue.")
        if not open_in_new_terminal(command, cwd, cfg):
            sys.exit(2)


def cmd_simulate(args):
    """Feed Relay exactly what Claude Code sends, so the switch can be shown without using the limit."""
    cwd = Path(args.cwd or os.getcwd()).resolve()
    run = simulate_codex if getattr(args, "agent", "claude") == "codex" else simulate_hooks
    if args.live:
        run(cwd, dict(os.environ))
        return
    with tempfile.TemporaryDirectory(prefix="relay-demo-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        state = root / "state"
        save_json(state / "config.json", {**config(), "mode": "auto", "dry_run": True})
        env = dict(os.environ, RELAY_HOME=str(state), CLAUDE_CONFIG_DIR=str(root / "claude"),
                   RELAY_DRY_RUN="1")
        run(workspace, env)
    print("Simulation complete. The temporary checkpoint was removed.")


def simulate_codex(cwd, env):
    """Feed Relay the payloads Codex's hooks send, with simulated usage, so the switch can be shown on demand."""
    cwd = str(cwd)
    session = "relay-demo-" + uuid.uuid4().hex
    me = [sys.executable, str(Path(__file__).resolve())]
    reset = int(time.time()) + 2 * 3600
    print("Simulating Codex CLI turns while 5-hour usage climbs (usage values are simulated) ...\n")
    for pct in (45, 85, 92, 100):
        usage = {"pct": pct, "resets": reset, "weekly_pct": 12, "weekly_resets": None, "plan": "plus",
                 "reached": "rate_limit_reached" if pct >= 100 else None, "allowed": pct < 100}
        payload = {"hook_event_name": "Stop", "session_id": session, "cwd": cwd, "transcript_path": None,
                   "last_assistant_message": "Added Light.dim() with tests. Next: Room.apply_scene()."}
        out = subprocess.run(me + ["codex-hook"], input=json.dumps(payload), capture_output=True, text=True,
                             encoding="utf-8", cwd=cwd, env=dict(env, RELAY_SIMULATED_CODEX_USAGE=json.dumps(usage)))
        result = out.stderr.strip() or "no action (below the 90% threshold)"
        print(f"Codex turn ends at {pct:>3}%  ->  {usage_line(usage).split('  ', 1)[1]}\n    {result}")
    print(f"\nHandoff file: {checkpoint(cwd, session)}")


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
    here = Path(__file__).resolve().parent
    if " " in str(here) or "onedrive" in str(here).lower():
        print(f"  [TIP] Relay lives in {here}\n"
              "        -> a path without spaces and outside OneDrive (e.g. C:\\claude-relay) makes hooks more robust")
    s = read_settings()
    manifest = load_json(installation_manifest(), {})
    check("Status line installed", manifest and s.get("statusLine") == manifest["status"], "Run: relay.py install")
    check("StopFailure hook installed", manifest and manifest["hook"] in s.get("hooks", {}).get("StopFailure", []),
          "Run: relay.py install")
    check("/handoff command installed", (CLAUDE_DIR / "commands" / "handoff.md").exists(), "Run: relay.py install")
    check("/ask-antigravity command installed", (CLAUDE_DIR / "commands" / "ask-antigravity.md").exists(),
          "Run: relay.py install")
    codex = shutil.which(CodexCLI.executable)
    codex_manifest = load_json(codex_manifest_path(), {})
    if codex or codex_manifest:
        codex_version = version([codex, "--version"]) if codex else None
        check(f"Codex CLI: {codex_version or 'unavailable'}", codex_version, "Install the Codex CLI and sign in")
        if codex_manifest:
            hooks = (load_json(codex_hooks_file(), {}) or {}).get("hooks", {})
            installed = all(group in hooks.get(event, []) for event, group in codex_manifest.get("groups", {}).items())
            check("Codex Stop and PostModelRequest hooks installed", installed, "Run: relay.py install --codex")
            print("        → approve them once in Codex with /hooks (Codex skips hooks you haven't trusted)")
        else:
            print("  [TIP] Codex CLI found → run relay.py install --codex to switch Codex to Antigravity at its limit too")
    st = load_json(STATE_FILE, {})
    if st.get("five_hour_pct") is not None:
        print(f"\n  Last seen 5-hour usage: {st['five_hour_pct']:.0f}% (resets {fmt_time(st.get('resets_at'))})")
    else:
        print("\n  No usage data yet: send one message in Claude Code (Pro/Max plans only).")
    print(f"  Mode: {cfg['mode']} | prepare at {cfg['prepare_at']}% | model in Antigravity: "
          f"{cfg['target_model'] or 'Antigravity default'} | Antigravity mode: {cfg.get('target_mode') or 'default'}")
    print("\nAll good." if ok_all else "\nFix the items marked FIX, then run doctor again.")


def human_delta(seconds):
    seconds = int(abs(seconds))
    hours, minutes = seconds // 3600, seconds % 3600 // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def cmd_status(args):
    cfg, state, codex = config(), load_json(STATE_FILE, {}), load_json(codex_state_file(), {})
    if getattr(args, "agent", None) == "codex":
        live = CodexCLI.read_usage()
        if live:
            codex = dict(live, updated=time.time())
            save_json(codex_state_file(), codex)
    if getattr(args, "json", False):
        print(json.dumps({"version": VERSION, "config": cfg, "state": state, "codex": codex}, indent=2))
        return
    now = time.time()
    print(f"Claude Relay {VERSION}   mode: {cfg['mode']}   prepare at {cfg['prepare_at']}%   "
          f"Antigravity model: {cfg['target_model'] or 'default'}")
    pct, resets = to_float(state.get("five_hour_pct")), to_epoch(state.get("resets_at"))
    if pct is None:
        print("5-hour usage : no data yet (send one message in Claude Code; Pro/Max plans only)")
    else:
        bar = "█" * int(min(max(pct, 0), 100) // 10)
        seen = human_delta(now - float(state.get("updated") or now))
        print(f"5-hour usage : {bar}{'░' * (10 - len(bar))} {pct:.0f}%   (seen {seen} ago)")
    if resets:
        if resets > now:
            print(f"Resets       : {fmt_time(resets)}, in {human_delta(resets - now)}")
        else:
            print(f"Resets       : {fmt_time(resets)}, {human_delta(now - resets)} ago. "
                  "Claude is available again: run relay.py back --launch in the project.")
    if to_float(codex.get("pct")) is not None:
        seen = human_delta(now - float(codex.get("updated") or now))
        print(f"Codex usage  : {usage_line(codex).split('  ', 1)[1]}   (seen {seen} ago)")
    cwd = Path(getattr(args, "cwd", None) or os.getcwd()).resolve()
    root = cwd / HANDOFF_DIR
    found = sorted(root.glob("sessions/*/" + HANDOFF_NAME)) + ([root / HANDOFF_NAME] if (root / HANDOFF_NAME).exists() else [])
    print(f"Checkpoints  : {len(found)} in {cwd}")
    for path in found:
        updated = dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%d %b %H:%M")
        label = "manual" if path.parent == root else (source_session(path) or "unknown")
        print(f"  {label}  (updated {updated})\n    {path.relative_to(cwd).as_posix()}")
    if LOG_FILE.exists():
        print("\nRecent activity:")
        for line in LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]:
            stamp, _, message = line.partition("  ")
            message = message.split(" (reason=", 1)[0].replace(str(cwd), ".")
            print(f"  {stamp[11:16] or stamp}  {message[:96] + ' …' if len(message) > 97 else message}")


CONFIG_RULES = {
    "mode": ("choice", ("auto", "manual")),
    "warn_at": ("int", (1, 99)),
    "prepare_at": ("int", (50, 99)),
    "target_cmd": ("text", None),
    "target_model": ("text", None),
    "target_mode": ("choice", ("default", "accept-edits", "auto", "plan")),
    "color": ("bool", None),
    "redact": ("bool", None),
    "diff_lines": ("int", (0, 5000)),
}


def parse_setting(key, value):
    kind, rule = CONFIG_RULES[key]
    if kind == "choice":
        if value not in rule:
            raise ValueError(f"{key} must be one of: {', '.join(rule)}")
        return value
    if kind == "int":
        try:
            number = int(value)
        except ValueError:
            raise ValueError(f"{key} must be a whole number") from None
        if not rule[0] <= number <= rule[1]:
            raise ValueError(f"{key} must be between {rule[0]} and {rule[1]}")
        return number
    if kind == "bool":
        if value.lower() not in ("true", "false", "on", "off", "1", "0", "yes", "no"):
            raise ValueError(f"{key} must be true or false")
        return value.lower() in ("true", "on", "1", "yes")
    return value


def cmd_config(args):
    stored = load_json(CONFIG_FILE, {})
    cfg = dict(DEFAULT_CONFIG, **stored)
    if not args.key:
        for key in CONFIG_RULES:
            print(f"{key:<13} {json.dumps(cfg.get(key))}")
        print(f"\nChange one with: relay.py config KEY VALUE   (file: {CONFIG_FILE})")
        return
    if args.key not in CONFIG_RULES:
        raise ValueError(f"Unknown setting {args.key!r}. Known: {', '.join(CONFIG_RULES)}")
    if args.value is None:
        print(json.dumps(cfg.get(args.key)))
        return
    stored[args.key] = parse_setting(args.key, args.value)
    merged = dict(DEFAULT_CONFIG, **stored)
    if merged["warn_at"] >= merged["prepare_at"]:
        raise ValueError("warn_at must be lower than prepare_at")
    save_json(CONFIG_FILE, stored)
    print(f"{args.key} = {json.dumps(stored[args.key])}")


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
        "(what you already tried, and the relevant files as project-relative paths such as src/app.py: Relay attaches "
        "those files for Antigravity). If the request is empty, ask me what to send.\n"
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
    codex_hooks = read_codex_hooks() if getattr(args, "codex", False) else None  # validate before writing anything
    save_json(manifest_path, manifest)
    save_json(CLAUDE_SETTINGS, settings)
    save_json(CONFIG_FILE, cfg)
    print(f"Installed Claude Relay {VERSION}. Mode: {cfg['mode']}, handoff prepared at {cfg['prepare_at']}%.")
    print(f"  Claude settings : {CLAUDE_SETTINGS}\n  Slash commands  : /handoff, /ask-antigravity\n  Relay config    : {CONFIG_FILE}")
    if codex_hooks is not None:
        windows_py = "py -3" if shutil.which("py") else q(py)  # Codex hook commands on Windows: unquoted first token
        install_codex(codex_hooks, run("codex-hook"), f"{windows_py} {q(cmd_path(__file__))} codex-hook", run)
    print("Restart Claude Code, then run: relay.py doctor")


CODEX_SKILL = "relay-handoff"


def codex_hooks_file():
    return CODEX_DIR / "hooks.json"


def codex_manifest_path():
    return STATE_DIR / ("codex-install-" + hashlib.sha256(str(codex_hooks_file().resolve()).encode()).hexdigest() + ".json")


def read_codex_hooks():
    path = codex_hooks_file()
    data = load_json(path, None) if path.exists() else {}
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError(f"Invalid Codex hooks file at {path}.")
    if any(not isinstance(v, list) for v in data.get("hooks", {}).values()):
        raise ValueError(f"Every event in {path} must hold a list of hook groups.")
    return data


def install_codex(data, command, command_windows, run):
    previous = load_json(codex_manifest_path(), {})
    hooks = data.setdefault("hooks", {})
    groups = {}
    for event in ("Stop", "PostModelRequest"):
        group = {"hooks": [{"type": "command", "command": command, "commandWindows": command_windows,
                            "timeout": 60, "async": True,
                            "statusMessage": "Claude Relay: checking Codex usage"}]}
        entries = hooks.setdefault(event, [])
        owned = previous.get("groups", {}).get(event)
        if owned in entries:
            entries.remove(owned)
        if group not in entries:
            entries.append(group)
        groups[event] = group
    skill_path = CODEX_DIR / "skills" / CODEX_SKILL / "SKILL.md"
    skill = (
        "---\n"
        f"name: {CODEX_SKILL}\n"
        "description: Hand the current Codex task over to Google Antigravity with Claude Relay. Use when the user asks "
        "to hand off, to switch to Antigravity, or says the Codex usage limit is close.\n"
        "---\n\n"
        "# Hand this task over to Google Antigravity\n\n"
        "In PowerShell, put `& ` in front of a command that starts with a quoted path.\n\n"
        f"1. Run: {run('handoff --agent codex')}\n"
        "2. Open the printed checkpoint and replace the text under 'Last recorded agent update' with a short summary "
        "(max 8 bullets): what is done, what is in progress, the exact next step, and any gotchas.\n"
        f"3. Run: {run('launch --session manual')}\n"
        "4. Report the launch result.\n")
    kept_skill = skill_path.exists() and skill_path.read_text(encoding="utf-8") != previous.get("skill")
    if kept_skill:
        print(f"Kept existing Codex skill: {skill_path}")
    else:
        save_text(skill_path, skill)
    save_json(codex_hooks_file(), data)
    save_json(codex_manifest_path(), {"groups": groups, "skill": None if kept_skill else skill})
    print(f"  Codex hooks     : {codex_hooks_file()} (Stop, PostModelRequest)")
    print(f"  Codex skill     : ${CODEX_SKILL}")
    print("  Next: open Codex and run /hooks to approve the two Relay hooks (Codex skips untrusted hooks).")


def uninstall_codex():
    manifest = load_json(codex_manifest_path(), {})
    if not manifest:
        return False
    data = read_codex_hooks()
    hooks = data.get("hooks", {})
    for event, group in manifest.get("groups", {}).items():
        entries = hooks.get(event, [])
        if group in entries:
            entries.remove(group)
            if not entries:
                hooks.pop(event)
    save_json(codex_hooks_file(), data)
    skill_path = CODEX_DIR / "skills" / CODEX_SKILL / "SKILL.md"
    if manifest.get("skill") and skill_path.is_file() and skill_path.read_text(encoding="utf-8") == manifest["skill"]:
        skill_path.unlink()
        with contextlib.suppress(OSError):
            skill_path.parent.rmdir()
    codex_manifest_path().unlink()
    print("Claude Relay removed from Codex CLI.")
    return True


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
    removed_codex = uninstall_codex()
    manifest_path = installation_manifest()
    manifest = load_json(manifest_path, {})
    if not manifest:
        if not removed_codex:
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
    i.add_argument("--codex", action="store_true", help="also add the automatic switch to Codex CLI")
    for name in ("uninstall", "statusline", "on-limit", "doctor", "codex-hook"):
        sub.add_parser(name)
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    status.add_argument("--cwd")
    status.add_argument("--agent", choices=["claude", "codex"])
    w = sub.add_parser("watch")
    w.add_argument("--agent", choices=["codex"], required=True)
    w.add_argument("--interval", type=int, default=120)
    w.add_argument("--once", action="store_true")
    w.add_argument("--cwd")
    w.add_argument("--session")
    conf = sub.add_parser("config")
    conf.add_argument("key", nargs="?")
    conf.add_argument("value", nargs="?")
    h = sub.add_parser("handoff"); h.add_argument("--launch", action="store_true")
    h.add_argument("--cwd"); h.add_argument("--transcript")
    h.add_argument("--agent", choices=["claude", "codex"], default="claude")
    l = sub.add_parser("launch"); l.add_argument("--cwd")
    a = sub.add_parser("ask"); a.add_argument("question", nargs="?"); a.add_argument("--file")
    a.add_argument("--cwd"); a.add_argument("--allow-edits", action="store_true")
    a.add_argument("--timeout", type=int, default=ASK_TIMEOUT)
    b = sub.add_parser("back"); b.add_argument("--launch", action="store_true"); b.add_argument("--cwd")
    s = sub.add_parser("simulate"); s.add_argument("--live", action="store_true"); s.add_argument("--cwd")
    s.add_argument("--agent", choices=["claude", "codex"], default="claude")
    b.add_argument("--resume", action="store_true")
    b.add_argument("--to", choices=["claude", "codex"])
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
    if getattr(args, "session", None) and clean_session(args.session) is None:
        print(f"Relay: ignoring unexpanded session ID {args.session!r}.", file=sys.stderr)
        args.session = None
    try:
        {"install": cmd_install, "uninstall": cmd_uninstall, "statusline": cmd_statusline, "on-limit": cmd_on_limit,
     "status": cmd_status, "doctor": cmd_doctor, "handoff": cmd_handoff, "launch": cmd_launch, "ask": cmd_ask,
     "back": cmd_back, "simulate": cmd_simulate, "preview": cmd_preview, "checkpoints": cmd_list,
         "purge-answers": cmd_purge, "config": cmd_config, "codex-hook": cmd_codex_hook,
         "watch": cmd_watch}[args.cmd](args)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f"Relay: {error}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
