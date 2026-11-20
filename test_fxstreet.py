from datetime import datetime, timedelta, timezone

import pytest

import fxstreet as cb

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def ev(name="CPI m/m", ccy="USD", actual=0.5, forecast=0.3, previous=0.1, impact="High", days_ago=0.0,
       deviation=None, better=None, ev_id=None):
    return {"id": ev_id or f"{name}-{ccy}-{days_ago}", "date_utc": (NOW - timedelta(days=days_ago)).isoformat(),
            "currency": ccy, "name": name, "impact": impact, "actual": actual, "forecast": forecast,
            "previous": previous, "unit": "%", "deviation": deviation, "better": better}


# Mocked from FXStreet eventDates response
RAW = {
    "id": "7778882c-f48b-4ec0-8a6f-d701a8afd900", "dateUtc": "2026-11-10T13:30:00Z",
    "actual": 0.4, "revised": None, "consensus": 0.3, "ratioDeviation": 0.83, "previous": 0.6,
    "isBetterThanExpected": True, "name": "Consumer Price Index (MoM)", "countryCode": "US",
    "currencyCode": "USD", "unit": "%", "potency": None, "volatility": "HIGH", "isSpeech": False,
}


# ---------------------------------------------------------------- normalise

def test_normalise_fxstreet_event():
    e = cb.normalise(RAW)
    assert e == {"id": RAW["id"], "date_utc": "2026-11-10T13:30:00+00:00", "currency": "USD",
                 "name": "Consumer Price Index (MoM)", "impact": "High", "actual": 0.4, "forecast": 0.3,
                 "previous": 0.6, "unit": "%", "deviation": 0.83, "better": True}


def test_normalise_prefers_revised_previous():
    assert cb.normalise({**RAW, "revised": 0.5})["previous"] == 0.5


@pytest.mark.parametrize("vol,want", [("HIGH", "High"), ("MEDIUM", "Medium"), ("LOW", "Low"), ("NONE", "Nil")])
def test_normalise_impact(vol, want):
    assert cb.normalise({**RAW, "volatility": vol})["impact"] == want


def test_normalise_skips_speeches_and_bad_rows():
    assert cb.normalise({**RAW, "isSpeech": True}) is None
    assert cb.normalise({**RAW, "dateUtc": "garbage"}) is None
    assert cb.normalise({"name": "no id"}) is None


# ---------------------------------------------------------------- store

def test_upsert_fills_actual_without_duplicating():
    store = {}
    assert cb.upsert(store, [ev(actual=None)]) == (1, 0)
    assert cb.upsert(store, [ev(actual=None)]) == (0, 0)
    assert cb.upsert(store, [ev(actual=0.5)]) == (0, 1)
    assert len(store) == 1 and next(iter(store.values()))["actual"] == 0.5


def test_upsert_never_erases_known_actual():
    store = {}
    cb.upsert(store, [ev(actual=0.5)])
    cb.upsert(store, [ev(actual=None)])
    assert next(iter(store.values()))["actual"] == 0.5


def test_store_roundtrip(tmp_path):
    path = tmp_path / "d" / "cal.json"
    cb.save_store({"k": ev()}, path)
    assert cb.load_store(path) == {"k": ev()}
    assert cb.load_store(tmp_path / "missing.json") == {}


# ---------------------------------------------------------------- scoring

@pytest.mark.parametrize("name,want", [
    ("RBA Interest Rate Decision", ("rates", 1)),
    ("Consumer Price Index (MoM)", ("inflation", 1)),
    ("Harmonized Index of Consumer Prices (YoY)", ("inflation", 1)),
    ("Unemployment Rate s.a.", ("employment", -1)),
    ("Initial Jobless Claims", ("employment", -1)),
    ("Nonfarm Payrolls", ("employment", 1)),
    ("Employment Change s.a.", ("employment", 1)),
    ("Gross Domestic Product (QoQ)", ("growth", 1)),
    ("ZEW Survey - Economic Sentiment", ("sentiment", 1)),
    ("Bank Holiday", None),
])
def test_classify(name, want):
    assert cb.classify(name) == want


def test_fxstreet_verdict_sets_sign_and_deviation_sets_size():
    assert cb.surprise(ev(deviation=0.83, better=True)) == pytest.approx(0.6805, abs=1e-3)
    # FXStreet's verdict wins even if the keyword direction would say otherwise
    assert cb.surprise(ev("Unemployment Rate", actual=4.1, forecast=4.3, deviation=-1.2, better=True)) > 0
    assert cb.surprise(ev(deviation=2.0, better=False)) < 0


def test_inline_release_scores_zero():
    assert cb.surprise(ev(actual=0.3, forecast=0.3, deviation=0.0, better=None)) == 0.0


def test_fallback_cpi_beat_is_bullish_miss_bearish():
    assert cb.event_score(ev(actual=0.5, forecast=0.3), NOW) > 0
    assert cb.event_score(ev(actual=0.1, forecast=0.3), NOW) < 0


def test_fallback_higher_unemployment_is_bearish():
    assert cb.event_score(ev("Unemployment Rate", actual=4.3, forecast=4.1), NOW) < 0


def test_fallback_uses_previous_without_consensus():
    assert cb.surprise(ev(actual=0.5, forecast=None, previous=0.1)) > 0
    assert cb.surprise(ev(actual=None)) is None


def test_fallback_level_figures_use_relative_miss():
    assert cb.surprise(ev("Nonfarm Payrolls", actual=250, forecast=150)) > 0.95


def test_unknown_event_without_verdict_is_skipped_but_with_verdict_scores_as_other():
    assert cb.event_score(ev("Mystery Index"), NOW) is None
    s = cb.event_score(ev("Mystery Index", deviation=1.0, better=True), NOW)
    assert s == pytest.approx(cb.surprise(ev(deviation=1.0, better=True)) * 3 * cb.CAT_W["other"])


def test_decay_halves_at_half_life():
    fresh = cb.event_score(ev(), NOW)
    old = cb.event_score(ev(days_ago=cb.HALF_LIFE_DAYS), NOW)
    assert old == pytest.approx(fresh / 2)


def test_impact_weighting():
    assert cb.event_score(ev(impact="High"), NOW) == pytest.approx(3 * cb.event_score(ev(impact="Low"), NOW))


def test_impact_filter_defaults_to_high_and_medium():
    events = [ev(impact="High", ev_id="h"), ev(impact="Medium", ev_id="m"), ev(impact="Low", ev_id="l"),
              ev(impact="Nil", ev_id="n")]
    kept = {e["id"] for _, e in cb.currency_scores(events, NOW)["USD"]["drivers"]}
    assert kept == {"h", "m"}
    kept = {e["id"] for _, e in cb.currency_scores(events, NOW, impacts=("High",))["USD"]["drivers"]}
    assert kept == {"h"}


def test_currency_scores_window_and_unknown_skipped():
    events = [ev(ccy="AUD"), ev(ccy="AUD", days_ago=30), ev(ccy="CNY"), ev("Bank Holiday", ccy="AUD")]
    s = cb.currency_scores(events, NOW, days=14)
    assert len(s["AUD"]["drivers"]) == 1 and "CNY" not in s


def test_pair_bias_is_base_minus_quote():
    s = cb.currency_scores([ev(ccy="AUD"), ev(ccy="NZD", actual=0.1)], NOW)
    d, label = cb.pair_bias("AUD/NZD", s)
    assert d == pytest.approx(s["AUD"]["score"] - s["NZD"]["score"])
    assert label == "Bullish"
    assert cb.pair_bias("NZDAUD", s)[1] == "Bearish"


def test_pair_bias_rejects_unknown():
    with pytest.raises(ValueError):
        cb.pair_bias("XAUUSD", cb.currency_scores([], NOW))


def test_unreleased_goes_to_watch_not_score():
    upcoming = {**ev(actual=None), "date_utc": (NOW + timedelta(hours=5)).isoformat()}
    assert cb.currency_scores([upcoming], NOW)["USD"]["drivers"] == []
    assert cb.watch_list([upcoming], NOW) == [upcoming]


def test_report_mentions_pair_and_lean():
    upcoming = {**ev(actual=None, ev_id="up"), "date_utc": (NOW + timedelta(hours=5)).isoformat()}
    out = cb.report({"a": ev(ccy="AUD"), "b": upcoming}, ["AUDNZD"], 14, True, NOW)
    assert "AUDNZD" in out and "Bullish" in out and "expected +USD" in out and "A 0.5%" in out
