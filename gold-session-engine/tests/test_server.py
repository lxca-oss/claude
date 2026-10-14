import time

import pytest
from fastapi.testclient import TestClient

from gse.journal import Journal
from gse.models import ClosedTrade, Position
from gse.terminal.server import create_app


@pytest.fixture()
def client():
    with TestClient(create_app(autostart=False)) as c:
        yield c


def test_status_settings_schedule(client):
    st = client.get("/api/status").json()
    assert st["running"] is False and st["settings_mode"] == "paper"
    s = client.get("/api/settings").json()
    assert s["settings"]["risk"]["risk_per_trade_pct"] == 0.3 and "$defs" in s["schema"]
    body = s["settings"]
    body["risk"]["max_trades_per_day"] = 6
    r = client.put("/api/settings", json=body)
    assert r.status_code == 200 and r.json()["settings"]["risk"]["max_trades_per_day"] == 6
    body["risk"]["risk_per_trade_pct"] = -1
    assert client.put("/api/settings", json=body).status_code == 400
    sch = client.get("/api/schedule?day=2026-10-14").json()
    assert sch["rows"][2]["sydney"] == "23:20–02:00"
    assert "module_a_reproduction" in client.get("/api/presets").json()


def test_datasets_backtest_job_and_replay(client):
    meta = client.post("/api/datasets/synthetic", json={"start": "2024-02-05", "end": "2024-02-23", "dataset": "syn"}).json()
    assert meta["bars"] > 10_000 and meta["synthetic"] is True
    assert [d["id"] for d in client.get("/api/datasets").json()] == ["syn"]

    job = client.post("/api/backtests", json={"dataset": "syn", "random_iterations": 5, "use_news": False}).json()["id"]
    for _ in range(120):
        st = client.get(f"/api/backtests/{job}/status").json()
        if st["state"] in ("done", "error"):
            break
        time.sleep(0.5)
    assert st["state"] == "done", st
    res = client.get(f"/api/backtests/{job}").json()
    assert res["summary"]["trades"] > 0 and res["gates"]
    assert client.get(f"/api/backtests/{job}/trades.csv").text.startswith("open_utc,close_utc,ref")
    assert client.get("/api/backtests").json()[0]["id"] == job
    client.delete(f"/api/backtests/{job}")
    assert client.get("/api/backtests").json() == []

    r = client.post("/api/bot/start", json={"kind": "replay", "dataset": "syn", "speed": 1e9})
    assert r.status_code == 200
    for _ in range(200):
        st = client.get("/api/status").json()
        if st["runner"] and st["runner"]["status"] == "finished":
            break
        time.sleep(0.1)
    assert st["runner"]["status"] == "finished"
    trades = client.get("/api/trades?mode=replay").json()
    assert trades["trades"] and trades["stats"]["all"]["trades"] == len([t for t in trades["trades"] if t["status"] == "closed"])
    assert client.get("/api/trades.csv?mode=replay").text.count("\n") == len(trades["trades"]) + 1
    assert client.post("/api/bot/stop").json() == {"ok": True}


def test_live_start_requires_live_mode_and_credentials(client):
    r = client.post("/api/bot/start", json={"kind": "live", "account_id": 1, "is_live": False})
    assert r.status_code == 400 and "live trading is disabled" in r.json()["detail"]
    assert client.get("/api/ctrader/accounts").status_code == 400  # no credentials yet
    client.post("/api/ctrader/credentials", json={"client_id": "abc", "client_secret": "s3cret"})
    cred = client.get("/api/ctrader/credentials").json()
    assert cred["client_id"] == "abc" and cred["has_secret"] and "s3cret" not in str(cred)
    assert "client_id=abc" in client.get("/api/ctrader/oauth-url").json()["url"]


def test_news_endpoints(client):
    csv = "datetime_utc,currency,impact,title\n2030-01-10 13:30,USD,High,Non-Farm Employment Change\n"
    assert client.post("/api/news/import", content=csv).json()["added"] == 1
    ev = client.post("/api/news/event", json={"datetime_utc": "2030-01-11 15:00", "title": "Manual"}).json()
    assert ev["currency"] == "USD"
    client.delete(f"/api/news/event?ts={ev['ts']}&title=Manual")
    assert client.get("/api/news").json()["total"] == 1


def test_password_protection(monkeypatch):
    monkeypatch.setenv("GSE_TERMINAL_PASSWORD", "pw")
    with TestClient(create_app(autostart=False)) as c:
        assert c.get("/api/status").status_code == 401
        assert c.post("/api/login", json={"password": "nope"}).status_code == 401
        assert c.post("/api/login", json={"password": "pw"}).status_code == 200
        assert c.get("/api/status").status_code == 200


def test_journal_open_close_and_sizing_check():
    j = Journal()
    p = Position(ref="A1", module="A", side=1, units=30, initial_units=30, entry=2400.0, sl=2390.0, tp=2425.0,
                 open_ts=1768374000, risk_money=305.1, r_dist=10.0, initial_sl=2390.0, planned_entry=2399.9)
    j.record_open(p, 1, "paper", "demo-1")
    assert j.trade_meta("A1")["status"] == "open" and j.trade_meta("A1")["r_dist"] == 10.0
    t = ClosedTrade(ref="A1", module="A", side=1, units=30, entry=2400.0, exit=2390.0, planned_entry=2399.9,
                    sl_initial=2390.0, tp=2425.0, open_ts=1768374000, close_ts=1768377600, pnl=-302.1, commission=2.1,
                    risk_money=305.1, r=-0.99, exit_reason="sl", mfe_r=0.2, mae_r=-1.0, partial_taken=False, be_moved=False)
    j.record_close(t, 1, "paper", "demo-1")
    rows = j.today_trades("2026-01-14", "paper", "demo-1")
    assert len(rows) == 1 and rows[0]["status"] == "closed"
    chk = j.sizing_check()
    assert chk["ref"] == "A1" and chk["ratio"] == pytest.approx(302.1 / 305.1)
    assert "A1" in j.trades_csv()
