# Changelog

All notable changes to Claude Relay. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [semantic versioning](https://semver.org/).

## [1.9.0] - 2026-11-21

### Added
- Installable package: `pipx install git+https://github.com/CodeWidKrish/claude-relay` gives a `claude-relay` command.
- `doctor` reports invalid settings by name.
- CI: lint (ruff), shellcheck, PowerShell parsing of the installer, a coverage gate (80%), a package build check, and Python 3.8, 3.10, 3.12 and 3.13.
- `SECURITY.md`, `CONTRIBUTING.md`, issue and pull request templates, Dependabot for GitHub Actions.

### Fixed
- `purge-answers` never removed anything: its file pattern didn't match the real answer names (`YYYYMMDD-HHMMSS-<id>.md`).
- A mistyped setting in `config.json` could make every hook fail silently. Invalid values now fall back to their defaults.
- `relay.log` grew without limit; it now rotates at 512 KB.

### Changed
- `--help` lists every command with a description; hook-only commands stay hidden.

## [1.8.0] - 2026-11-21

### Added
- One-line installers: `install.ps1` (Windows) and `install.sh` (macOS/Linux).
- `python relay.py` with no command runs `setup`: installs into Claude Code and, when found, Codex CLI, then runs `doctor`.

## [1.7.0] - 2026-11-21

### Added
- At 90%, a `Stop` hook asks Claude Code (and Codex) once per usage window to write its own summary into a new "Summary from the previous agent" section.
- Automatic return: after an automatic switch, a background waiter reopens the source agent once the limit resets (`config auto_return`).

### Fixed
- Pasted text could break the handoff's structure: headings and code fences in copied text are now escaped, and code blocks use a fence longer than any backticks inside.

## [1.6.0] - 2026-11-19

### Added
- Codex CLI support: usage from the Codex app-server, `Stop` and `PostModelRequest` hooks, the `$relay-handoff` skill, `watch --agent codex`, `simulate --agent codex`, and `back` returning to Codex.
- `config target_mode` to open Antigravity in `accept-edits`, `auto` or `plan` mode.

### Fixed
- npm-installed `.cmd` launchers in folders with spaces (for example `C:\Users\First Last\...`) failed to start.
- A more focused Antigravity launch instruction, so it works on the project instead of inspecting Relay.

## [1.5.0] - 2026-11-17

### Added
- Secret redaction in checkpoints, a diff excerpt and the starting commit in each checkpoint, a "what changed" summary on `back`, file attachment and retry for `/ask-antigravity`, a readable `status`, and `config`.

## [1.4.3] - 2026-11-17

### Fixed
- Empty answers from headless Antigravity were reported as success.
- `.relay/` now ignores itself in git; handoff instructions point at real sections; session locks are always released.

## [1.4.0] - 2026-11-17

### Added
- Session-scoped checkpoints, a launch cooldown per project and session, and safer install and uninstall.

## [1.2.1] - 2026-11-14

### Added
- `/ask-antigravity`: use Antigravity from inside a Claude Code session.

## [1.1.1] - 2026-11-14

### Added
- First release: automatic switch from Claude Code to Antigravity at the 5-hour limit, manual `/handoff`, and `back`.

[1.9.0]: https://github.com/CodeWidKrish/claude-relay/releases/tag/v1.9.0
