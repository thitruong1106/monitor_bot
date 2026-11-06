"""Shared test setup.

Every test runs with state.json, alerts.jsonl and the runtime env vars pointed
at a temp dir / cleared, so no test can ever write into the real repo files
(which is how 5 junk rows reached alerts.jsonl in PR #1).
"""
import pytest

import alert_log
import state as st


@pytest.fixture(autouse=True)
def _isolate_repo_files(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(alert_log, "ALERTS_FILE", str(tmp_path / "alerts.jsonl"))
    for var in ("DISCORD_ROLE_ID", "HEARTBEAT_URL", "WEBHOOKS_JSON"):
        monkeypatch.delenv(var, raising=False)
    yield
