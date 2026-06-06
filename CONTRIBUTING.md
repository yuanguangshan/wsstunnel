# Contributing

Thanks for considering contributing to wsstunnel.

## Quick start

```bash
git clone https://github.com/yuanguangshan/wsstunnel.git
cd wsstunnel
pip install -e ".[dev]"
pytest
```

## Before submitting

1. **Run tests** — `pytest` must pass. Currently 103+ tests.
2. **Check Python version** — wsstunnel requires Python ≥ 3.10.
3. **Keep backward compatibility** — protocol changes (especially `__*` control messages) should not break existing clients. If you must change a message format, bump the version accordingly and document it.

## Code style

- Format with [Black](https://github.com/psf/black) — default settings, line length 88.
- Import order with [isort](https://pycqa.github.io/isort/).
- Type hints — use `from __future__ import annotations` for forward references (already in `client.py` and `relay.py`).
- No mypy strict mode yet, but preferred for new files.

## Testing

- Tests are in `tests/` using pytest.
- Async tests use `pytest-asyncio` with `asyncio_mode = auto` (configured in `pyproject.toml`).
- Use `MockWebSocket` from `tests/test_relay.py` for relay tests; use `SyncMockWebSocket` from `tests/test_file_transfer.py` for client sync tests.

## Architecture overview

- **`relay.py`** — async WebSocket server (`websockets` library). Routes messages between frontends and backends. Core state machine is `RelayState`.
- **`client.py`** — sync WebSocket client (`websocket-client` library). PTY or pipe mode. Runs on the target machine.
- **`cli.py`** — Click-based CLI (`relay`, `client`, `put`, `get` subcommands).
- **`web/index.html`** — xterm.js web terminal served by the relay's HTTP handler.

## Protocol notes

- Frontend → Relay → Backend: text or binary frames.
- Backend → Relay → Frontend: text frames forwarded with optional `[@name]` tag; binary frames forwarded raw.
- Control messages start with `__` (e.g., `__RESIZE:`, `__SIGNAL:`, `__FILE_BEGIN:`).
- New control messages should also start with `__` and follow existing patterns.

## Release process

```bash
# 1. Update version in pyproject.toml
# 2. Commit
git commit -m "chore: bump to v0.x.y"
# 3. Tag
git tag -a v0.x.y -m "v0.x.y - summary"
# 4. Push
git push origin main --tags
```

## Questions

Open an issue on GitHub.
