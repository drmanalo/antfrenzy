import sys
import types
from unittest.mock import MagicMock

import pytest

# Allow running without the real dependencies installed.
for _name, _attr in (("dotenv", "load_dotenv"), ("trading_ig", "IGService")):
    try:
        __import__(_name)
    except ImportError:
        _m = types.ModuleType(_name)
        setattr(_m, _attr, MagicMock())
        sys.modules[_name] = _m

import tradeig  # noqa: E402


def make_market(bid=100.0, offer=101.0, status="TRADEABLE", min_size=0.5,
                min_stop=10, stop_unit="POINTS", currencies=None, expiry="-"):
    inst = {"name": "Test Market", "expiry": expiry}
    if currencies is not None:
        inst["currencies"] = currencies
    return {
        "instrument": inst,
        "snapshot": {"marketStatus": status, "bid": bid, "offer": offer},
        "dealingRules": {
            "minDealSize": {"value": min_size},
            "minNormalStopOrLimitDistance": {"value": min_stop, "unit": stop_unit},
        },
    }


ACCEPTED = {"dealStatus": "ACCEPTED", "dealReference": "REF", "dealId": "ID", 
            "level": 101.0, "stopLevel": 91.0, "limitLevel": 111.0}

ENV = {"IG_USERNAME": "u", "IG_PASSWORD": "p", "IG_API_KEY": "k", "CAPITAL": "10000", "RISK_APPETITE": "1"}

ALL_ENV_KEYS = ["IG_USERNAME", "IG_PASSWORD", "IG_API_KEY", "IG_ACC_NUMBER", "CAPITAL", "RISK_APPETITE"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ALL_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def set_env(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)


# ---------------------------------------------------------------- login

@pytest.fixture
def svc(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(tradeig, "IGService", mock)
    monkeypatch.setattr(tradeig, "load_dotenv", MagicMock())
    return mock


def test_demo_login(monkeypatch, svc):
    set_env(monkeypatch, {**ENV, "IG_ACC_NUMBER": ""})
    ig = tradeig.login(False)
    svc.assert_called_once_with("u", "p", "k", "DEMO", acc_number=None)
    svc.return_value.create_session.assert_called_once()
    assert ig is svc.return_value


def test_live_login_with_account(monkeypatch, svc):
    set_env(monkeypatch, {**ENV, "IG_ACC_NUMBER": "ABC123"})
    tradeig.login(True)
    svc.assert_called_once_with("u", "p", "k", "LIVE", acc_number="ABC123")


def test_missing_env_exits(monkeypatch, svc):
    monkeypatch.setenv("IG_USERNAME", "u")
    with pytest.raises(SystemExit) as exc:
        tradeig.login(False)
    assert "IG_PASSWORD" in str(exc.value)
    svc.assert_not_called()


# ----------------------------------------------------------------- main

@pytest.fixture
def run(monkeypatch, capsys):
    """run(argv, market=..., env=..., answer=..., result=...) -> (ig, stdout, exit_exc)."""
    def _run(argv, market=None, env=None, answer="y", result=ACCEPTED):
        ig = MagicMock()
        ig.fetch_market_by_epic.return_value = market or make_market()
        ig.create_open_position.return_value = result
        monkeypatch.setattr(tradeig, "login", MagicMock(return_value=ig))
        monkeypatch.setattr(sys, "argv", ["tradeig.py", *argv])
        monkeypatch.setattr("builtins.input", lambda *_: answer)
        set_env(monkeypatch, ENV if env is None else env)
        exc = None
        try:
            tradeig.main()
        except SystemExit as e:
            exc = e
        return ig, capsys.readouterr().out, exc
    return _run


BASE = ["--epic", "X", "--stop-pts", "10"]


# argument validation
def test_requires_epic_and_stop(run):
    _, _, exc = run(["--epic", "X"])
    assert exc.code == 2


def test_rejects_non_positive_stop(run):
    _, _, exc = run(["--epic", "X", "--stop-pts", "-5"])
    assert exc.code == 2


# search
def test_search_with_results(monkeypatch, capsys):
    ig = MagicMock()
    df = MagicMock()
    df.__len__.return_value = 1
    df.__getitem__.return_value.to_string.return_value = "instrumentName table"
    ig.search_markets.return_value = df
    monkeypatch.setattr(tradeig, "login", MagicMock(return_value=ig))
    monkeypatch.setattr(sys, "argv", ["x", "--search", "foo"])
    tradeig.main()
    ig.search_markets.assert_called_once_with("foo")
    assert "instrumentName" in capsys.readouterr().out
    ig.create_open_position.assert_not_called()


def test_search_no_results(monkeypatch, capsys):
    ig = MagicMock()
    df = MagicMock()
    df.__len__.return_value = 0
    ig.search_markets.return_value = df
    monkeypatch.setattr(tradeig, "login", MagicMock(return_value=ig))
    monkeypatch.setattr(sys, "argv", ["x", "--search", "foo"])
    tradeig.main()
    assert "No markets found" in capsys.readouterr().out


# market checks
def test_market_not_tradeable(run):
    ig, _, exc = run(["--epic", "X", "--stop-pts", "100"], market=make_market(status="CLOSED"))
    assert "not tradeable" in str(exc.code)
    ig.create_open_position.assert_not_called()


def test_stop_below_broker_minimum(run):
    ig, _, exc = run(["--epic", "X", "--stop-pts", "5"], market=make_market(min_stop=10))
    assert "below broker minimum" in str(exc.code)
    ig.create_open_position.assert_not_called()


def test_percentage_min_stop_uses_offer_for_buy(run):
    # 10% of offer 101 = 10.1 -> stop 10 is too tight
    _, _, exc = run(BASE, market=make_market(min_stop=10, stop_unit="PERCENTAGE"))
    assert "below broker minimum 10.1" in str(exc.code)


def test_percentage_min_stop_uses_bid_for_sell(run):
    # 10% of bid 100 = 10.0 -> stop 10 is OK
    _, _, exc = run([*BASE, "--direction", "sell", "--dry-run"],
                    market=make_market(min_stop=10, stop_unit="PERCENTAGE"))
    assert exc is None


def test_size_below_min_deal_size(run):
    # risk 100 / 500 pts = 0.2 < 0.5 -> need 200 pts
    ig, _, exc = run(["--epic", "X", "--stop-pts", "500"], market=make_market(min_size=0.5))
    assert "Use --stop-pts >= 200" in str(exc.code)
    ig.create_open_position.assert_not_called()


# sizing
def test_size_rounds_down(run):
    # risk 100 / 30 = 3.333.. -> 3.33
    ig, _, _ = run(["--epic", "X", "--stop-pts", "30", "--yes"])
    assert ig.create_open_position.call_args.kwargs["size"] == 3.33


def test_risk_from_env(run):
    env = {**ENV, "CAPITAL": "20000", "RISK_APPETITE": "0.5"}  # risk = 100
    ig, _, _ = run(["--epic", "X", "--stop-pts", "50", "--yes"], env=env)
    assert ig.create_open_position.call_args.kwargs["size"] == 2.0


def test_default_capital_and_risk(run):
    env = {k: v for k, v in ENV.items() if k not in ("CAPITAL", "RISK_APPETITE")}
    ig, _, _ = run(["--epic", "X", "--stop-pts", "50", "--yes"], env=env)
    assert ig.create_open_position.call_args.kwargs["size"] == 2.0  # 10000 * 1% / 50


# order levels & payload
def test_buy_levels_and_order(run):
    ig, out, exc = run([*BASE, "--yes"])
    assert exc is None
    kw = ig.create_open_position.call_args.kwargs
    assert kw["direction"] == "BUY"
    assert kw["stop_distance"] == 10
    assert kw["limit_distance"] == 10
    assert kw["size"] == 10.0
    assert kw["order_type"] == "MARKET"
    assert kw["currency_code"] == "GBP"
    assert kw["expiry"] == "-"
    assert kw["guaranteed_stop"] is False
    assert "Stop  91.0" in out    # offer 101 - 10
    assert "Limit 111.0" in out   # offer 101 + 10
    assert "DEMO" in out
    assert "ACCEPTED" in out


def test_sell_levels_use_bid(run):
    ig, out, _ = run([*BASE, "--direction", "sell", "--yes"])
    assert ig.create_open_position.call_args.kwargs["direction"] == "SELL"
    assert "Stop  110.0" in out   # bid 100 + 10
    assert "Limit 90.0" in out    # bid 100 - 10


def test_currency_and_expiry_from_instrument(run):
    m = make_market(currencies=[{"code": "USD"}], expiry="DFB")
    ig, _, _ = run([*BASE, "--yes"], market=m)
    kw = ig.create_open_position.call_args.kwargs
    assert kw["currency_code"] == "USD"
    assert kw["expiry"] == "DFB"


def test_live_flag(run):
    _, out, _ = run([*BASE, "--live", "--dry-run"])
    tradeig.login.assert_called_once_with(True)
    assert "LIVE" in out


# confirmation / dry run
def test_dry_run_sends_nothing(run):
    ig, out, exc = run([*BASE, "--dry-run"])
    assert exc is None
    ig.create_open_position.assert_not_called()
    assert "Stop" in out


def test_confirmation_declined(run):
    ig, _, exc = run(BASE, answer="n")
    assert exc.code == "Cancelled"
    ig.create_open_position.assert_not_called()


def test_confirmation_accepted(run):
    ig, _, exc = run(BASE, answer=" Y ")
    assert exc is None
    ig.create_open_position.assert_called_once()


# result handling
def test_rejected_order_exits(run):
    res = {"dealStatus": "REJECTED", "reason": "INSUFFICIENT_FUNDS"}
    _, _, exc = run([*BASE, "--yes"], result=res)
    assert "INSUFFICIENT_FUNDS" in str(exc.code)
