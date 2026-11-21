# Contributing to Claude Relay

Thanks for helping. Relay is one Python file with no dependencies, and it should stay that way: anyone can read it, audit it and run it with only Python 3.8+.

## Set up

```bash
git clone https://github.com/CodeWidKrish/claude-relay.git
cd claude-relay
python tests/test_relay.py
python tests/test_regressions.py
```

The tests never touch your real Claude Code or Codex settings. They use temporary folders, a fake `agy` and a fake Codex app-server.

## Before you open a pull request

1. **Add a test** for the change in `tests/test_regressions.py`. A bug fix should include a test that fails without it.
2. **Run both test scripts.** CI also runs them on Windows, macOS and Linux with Python 3.8 to 3.13.
3. **Lint:** `pip install ruff` once, then `ruff check .` (the rules are in `pyproject.toml`).
4. **Keep Python 3.8 compatibility:** no `match`, no `str.removeprefix`, no `X | Y` type unions at runtime.
5. **Bump the version** in both `relay.py` (`VERSION`) and `pyproject.toml` when you change behavior, and add a `CHANGELOG.md` entry.
6. **Use Conventional Commits:** `feat: …`, `fix: …`, `docs: …`, `test: …`.

## Design rules

- **Hooks must never break the agent.** Every hook entry point catches errors and logs them.
- **Install is reversible.** Record what you add, and remove only that on `uninstall`.
- **Text copied into a checkpoint goes through `md_text()` or `fenced()`**, so it can't change the file's structure.
- **Don't send data anywhere new.** Relay only starts CLIs the user already has.

## Releasing (maintainer)

1. Bump `VERSION` and `pyproject.toml`, add the `CHANGELOG.md` entry, push to `main` and wait for CI.
2. Tag and publish a GitHub release: `git tag -a vX.Y.Z -m vX.Y.Z`, `git push origin vX.Y.Z`, then `gh release create vX.Y.Z`.
3. Publishing the release also publishes to PyPI once this one-time setup is done:
   - On [pypi.org](https://pypi.org/manage/account/publishing/), add a *pending trusted publisher*: project `claude-relay-antigravity`, owner `CodeWidKrish`, repository `claude-relay`, workflow `publish.yml`, environment `pypi`.
   - In the GitHub repository settings, add a variable `PYPI_PUBLISH` with the value `true`.

   No API token is needed. The `claude-relay` name on PyPI belongs to another project, which is why the package is called `claude-relay-antigravity`.

## Reporting a bug

Use the bug report template and include the output of `python relay.py doctor` and `python relay.py --version`. For security problems, see [SECURITY.md](SECURITY.md).
