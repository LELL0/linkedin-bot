"""Health rules for the Docker HEALTHCHECK. All data is synthetic."""

import json

from bot import health

NOW = 10_000.0


def status(**fields):
    return {"at": NOW - 10, "state": "running", "last_ok": NOW - 10, **fields}


def test_running_bot_is_healthy():
    assert health.problem(status(), NOW) is None


def test_missing_or_stale_status_is_unhealthy():
    assert health.problem(None, NOW) == "no status file"
    assert "not updated" in health.problem(status(at=NOW - 500), NOW)


def test_signed_out_is_unhealthy():
    assert "log in" in health.problem(status(state="needs_login"), NOW)


def test_no_successful_check_is_unhealthy():
    assert "no successful" in health.problem(status(last_ok=NOW - health.stale_poll_s() - 1), NOW)


def test_waiting_the_longest_poll_gap_is_healthy():
    gap = health.CONFIG.poll_s[1] + health.CONFIG.min_check_gap_s
    assert health.problem(status(last_ok=NOW - gap), NOW) is None


def test_starting_bot_is_not_judged_on_polls_yet():
    assert health.problem(status(state="starting", last_ok=NOW - 600), NOW) is None


def test_check_reads_the_status_file(tmp_path, monkeypatch):
    import dataclasses

    config = dataclasses.replace(health.CONFIG, status_path=tmp_path / "status.json")
    monkeypatch.setattr(health, "CONFIG", config)
    assert health.check() == 1
    health.write_status("running", 9e12, accepted=1, replies=2)
    data = json.loads(config.status_path.read_text())
    data["at"] = 9e12
    config.status_path.write_text(json.dumps(data))
    monkeypatch.setattr(health.time, "time", lambda: 9e12)
    assert health.check() == 0


def test_alerts_are_not_repeated_within_the_hour(monkeypatch):
    sent = []
    monkeypatch.setenv("TG_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_CHAT_ID", "c")
    monkeypatch.setattr(health.httpx, "post", lambda *a, **k: sent.append(k["json"]["text"]))
    monkeypatch.setattr(health.shutil, "which", lambda name: None)
    health._last_sent.clear()
    health.alert("signed out")
    health.alert("signed out")
    assert sent == ["LinkedIn bot: signed out"]


def test_healthcheck_uses_the_limit_the_bot_wrote():
    assert health.problem(status(last_ok=NOW - 5000, stale_poll_s=6000), NOW) is None
    assert "no successful" in health.problem(status(last_ok=NOW - 5000, stale_poll_s=4000), NOW)
