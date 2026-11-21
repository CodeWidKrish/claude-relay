# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub's **[Report a vulnerability](https://github.com/CodeWidKrish/claude-relay/security/advisories/new)** form rather than in a public issue. Include the steps to reproduce and the version (`python relay.py --version`). You can expect a first reply within a week.

## Supported versions

Only the latest release receives fixes.

## What Relay touches

Knowing this helps you judge any report:

| Location | What Relay does |
|---|---|
| `~/.claude/settings.json` | Adds a status line and `StopFailure` and `Stop` hooks. Install records exactly what it added, and `uninstall` removes only that. A broken file is never overwritten. |
| `~/.claude/commands/` | Adds `/handoff` and `/ask-antigravity`, and never replaces a command you edited |
| `~/.codex/hooks.json`, `~/.codex/skills/relay-handoff/` | With `--codex`: adds two hooks and a skill, keeping your own hooks. Codex runs them only after you approve them with `/hooks`. |
| `~/.claude-relay/` | Settings, usage readings, session locks and a log that rotates at 512 KB |
| `<project>/.relay/` | Checkpoints and saved answers. The folder ignores itself in git. |

Relay sends nothing to any server of its own. It starts the Claude Code, Codex and Antigravity CLIs you already have, and those services process what they read.

## Built-in protections

- Secrets that match common formats (API keys, tokens, private keys, passwords in URLs and `NAME=value`) are masked before a checkpoint is written. This is pattern-based, so check a checkpoint with `relay.py preview` before sharing it.
- `.env` files, keys, credential files, large files and files outside the project are never attached to `/ask-antigravity`.
- `/ask-antigravity` runs Antigravity in plan mode unless you pass `--allow-edits`.
- Hooks catch every error and log it, so a Relay failure can't break your agent session.
