#!/usr/bin/env python3
"""Economic-calendar bias: score released data (Actual vs Consensus) per currency, then per pair.

Monetary-policy logic: hot inflation / strong jobs / strong growth -> hawkish central bank
-> higher rates -> carry demand -> currency strengthens.

  event score    = surprise (-1..+1, + = good for the currency) * impact weight * category weight * time decay
  currency score = sum of its event scores over --days
  pair bias      = base score - quote score       (AUDNZD = AUD - NZD)

Data: the FXStreet economic calendar page, opened in a headless Playwright browser. The script reads the
calendar data the page loads for itself. Run `fetch` from cron; Actuals fill in as they are released.

Usage:
  python fxstreet.py fetch                         # this week (cron target)
  python fxstreet.py fetch --period last-month     # one-off backfill
  python fxstreet.py bias [--pair AUDNZD EURUSD] [--days 14] [--explain]
"""
import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PAGE = "https://www.fxstreet.com/economic-calendar"
STORE = Path(__file__).resolve().parent / "data" / "calendar.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/157.0.0.0 Safari/537.36")
PICKER = "button.fxs_c_datepicker_trigger_button"
# -- period value -> preset in the page's date picker 
# -- None = page default: last 2h to +4 days)
PERIODS = {"recent": None, "today": "Today", "yesterday": "Yesterday", "week": "This week",
           "last-week": "Previous week", "month": "This month", "last-month": "Previous month"}
MAJORS = ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF")
DEFAULT_PAIRS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "AUDNZD")

# ---- tunables
IMPACT_W = {"High": 3.0, "Medium": 2.0, "Low": 1.0, "Nil": 0.5}
IMPACTS = ("High", "Medium")  # default impact filter for fetch and bias (like the FXStreet page filter)
CAT_W = {"rates": 2.0, "inflation": 1.5, "employment": 1.2, "growth": 1.0, "sentiment": 0.5, "other": 0.5}
HALF_LIFE_DAYS = 7.0
BIAS_THRESHOLD = 2.0  # |pair score| at or above this -> Bullish / Bearish
DEV_SCALE = 1.0       # FXStreet deviation (~-7..+7) of 1.0 -> tanh(1) ~ 0.76
PCT_LIKE = 20.0       # fallback: |reference| below this is treated as a rate/percent, miss in points
PCT_SCALE = 0.2       # fallback: 0.2 pt miss on a percent figure -> tanh(1)
REL_SCALE = 0.10      # fallback: 10% miss on a level figure (NFP, claims, PMI) -> tanh(1)

# Order matters: first match wins ("unemployment" must come before "employment").
# direction: +1 = a higher number is good for the currency (hawkish), -1 = higher is bad.
CATEGORIES = [
    (("interest rate", "cash rate", "bank rate", "rate decision", "refinancing rate", "deposit facility"),
     "rates", 1),
    (("cpi", "ppi", "pce", "inflation", "price index", "consumer prices", "producer price", "hourly earnings",
      "wage", "labour cost", "labor cost"), "inflation", 1),
    (("unemployment", "jobless", "claims", "claimant"), "employment", -1),
    (("employment", "non-farm", "nonfarm", "payroll", "adp", "jobs", "job openings"), "employment", 1),
    (("gdp", "gross domestic product", "retail sales", "pmi", "trade balance", "industrial production",
      "manufacturing production", "durable goods", "housing starts", "building permits", "factory orders",
      "current account"), "growth", 1),
    (("confidence", "sentiment", "zew", "ifo", "tankan", "economic optimism"), "sentiment", 1),
]


def normalise(raw: dict) -> dict | None:
    """FXStreet eventDate -> store record. Speeches and malformed rows return None."""
    try:
        when = datetime.fromisoformat(raw["dateUtc"].replace("Z", "+00:00")).astimezone(timezone.utc)
        ev_id = raw["id"]
    except (KeyError, AttributeError, ValueError):
        return None
    if raw.get("isSpeech"):
        return None
    vol = str(raw.get("volatility") or "").upper()
    return {
        "id": ev_id,
        "date_utc": when.isoformat(),
        "currency": str(raw.get("currencyCode") or "").upper(),
        "name": str(raw.get("name") or "").strip(),
        "impact": {"HIGH": "High", "MEDIUM": "Medium", "LOW": "Low"}.get(vol, "Nil"),
        "actual": raw.get("actual"),
        "forecast": raw.get("consensus"),
        "previous": raw["revised"] if raw.get("revised") is not None else raw.get("previous"),
        "unit": raw.get("unit"),
        "deviation": raw.get("ratioDeviation"),
        "better": raw.get("isBetterThanExpected"),
    }


def upsert(store: dict, events: list[dict]) -> tuple[int, int]:
    """Merge events into store (keyed by FXStreet id). Never erase a known value. Returns (added, updated)."""
    added = updated = 0
    for ev in events:
        old = store.get(ev["id"])
        if old is None:
            store[ev["id"]] = ev
            added += 1
            continue
        new = {**old, **{f: v for f, v in ev.items() if v is not None}}
        if new != old:
            store[ev["id"]] = new
            updated += 1
    return added, updated


def load_store(path: Path = STORE) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_store(store: dict, path: Path = STORE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=1, sort_keys=True))
    tmp.replace(path)


def browse(period: str = "week", timeout_s: float = 30) -> list[dict]:
    """Open the FXStreet calendar like a visitor, pick a date preset, return the events the page loaded."""
    from playwright.sync_api import Error as PwError, sync_playwright

    loaded: list[list] = []

    def on_response(r):
        if "/eventDates/" in r.url and r.status == 200:
            try:
                loaded.append(r.json())
            except PwError:
                pass

    def wait_for(n: int, secs: float) -> bool:
        for _ in range(int(secs * 4)):
            if len(loaded) >= n:
                return True
            pg.wait_for_timeout(250)
        return len(loaded) >= n

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            pg = browser.new_page(user_agent=UA, locale="en-GB", timezone_id="UTC",
                                  viewport={"width": 1400, "height": 1000})
            pg.on("response", on_response)
            # A promo dialog pops up at a random moment; dismiss it whenever it blocks a click.
            pg.add_locator_handler(pg.get_by_role("button", name="Continue to FXStreet"), lambda b: b.click())
            pg.goto(PAGE, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            if not wait_for(1, timeout_s):
                sys.exit("FXStreet page loaded but no calendar data arrived (page changed or blocked?)")
            label = PERIODS[period]
            if label:
                pg.locator(PICKER).click(timeout=timeout_s * 1000)
                pg.get_by_text(label, exact=True).first.click(timeout=timeout_s * 1000)
                if not wait_for(2, 5):
                    apply = pg.get_by_text("Apply", exact=True)
                    if apply.count() and apply.first.is_visible():
                        apply.first.click()
                    if not wait_for(2, timeout_s):
                        sys.exit(f"Picked '{label}' but no calendar data arrived")
        finally:
            browser.close()
    return loaded[-1]


# ---------------------------------------------------------------- scoring

def classify(name: str) -> tuple[str, int] | None:
    n = name.lower()
    for words, cat, direction in CATEGORIES:
        if any(w in n for w in words):
            return cat, direction
    return None


def surprise(ev: dict) -> float | None:
    """Signed surprise -1..+1, positive = good for the currency.

    Uses FXStreet's own deviation and better/worse-than-expected verdict when present. Otherwise compares
    Actual with Consensus (fallback Previous) and applies the keyword direction."""
    a = ev.get("actual")
    if a is None:
        return None
    dev, better = ev.get("deviation"), ev.get("better")
    if dev is not None and better is not None:
        return math.copysign(math.tanh(abs(dev) / DEV_SCALE), 1 if better else -1)
    if dev == 0:
        return 0.0
    c = classify(ev["name"])
    ref = ev.get("forecast") if ev.get("forecast") is not None else ev.get("previous")
    if c is None or ref is None:
        return None
    diff = a - ref
    if abs(ref) < PCT_LIKE and abs(a) < PCT_LIKE:  # percent / rate figure: miss in points
        s = math.tanh(diff / PCT_SCALE)
    else:                                           # level figure: relative miss
        s = math.tanh(diff / max(abs(ref), 1e-9) / REL_SCALE)
    return s * c[1]


def event_score(ev: dict, now: datetime) -> float | None:
    s = surprise(ev)
    if s is None:
        return None
    age = (now - datetime.fromisoformat(ev["date_utc"])).total_seconds() / 86400
    if age < 0:
        return None
    cat = (classify(ev["name"]) or ("other", 1))[0]
    return s * IMPACT_W.get(ev.get("impact"), 1.0) * CAT_W[cat] * 0.5 ** (age / HALF_LIFE_DAYS)


def currency_scores(events: list[dict], now: datetime, days: float = 14,
                    impacts: tuple[str, ...] = IMPACTS) -> dict:
    """{CCY: {"score": float, "drivers": [(score, ev), ...] sorted by |score|}} for all majors."""
    out = {c: {"score": 0.0, "drivers": []} for c in MAJORS}
    since = now - timedelta(days=days)
    for ev in events:
        if (ev.get("currency") not in out or ev.get("impact") not in impacts
                or datetime.fromisoformat(ev["date_utc"]) < since):
            continue
        s = event_score(ev, now)
        if s:
            out[ev["currency"]]["score"] += s
            out[ev["currency"]]["drivers"].append((s, ev))
    for v in out.values():
        v["drivers"].sort(key=lambda t: -abs(t[0]))
    return out


def pair_bias(pair: str, scores: dict) -> tuple[float, str]:
    p = pair.upper().replace("/", "")
    base, quote = p[:3], p[3:]
    if len(p) != 6 or base not in scores or quote not in scores:
        raise ValueError(f"Unsupported pair {pair!r}; use two of {', '.join(MAJORS)}")
    d = scores[base]["score"] - scores[quote]["score"]
    label = "Bullish" if d >= BIAS_THRESHOLD else "Bearish" if d <= -BIAS_THRESHOLD else "Neutral"
    return d, label


def watch_list(events: list[dict], now: datetime, hours: float = 48) -> list[dict]:
    """Upcoming High-impact events for majors in the next `hours`."""
    until = now + timedelta(hours=hours)
    return sorted((ev for ev in events
                   if ev.get("currency") in MAJORS and ev.get("impact") == "High"
                   and now < datetime.fromisoformat(ev["date_utc"]) <= until),
                  key=lambda ev: ev["date_utc"])


# ---------------------------------------------------------------- output

def num(x: float | None, unit: str | None = None) -> str:
    return "-" if x is None else f"{x:g}{unit if unit == '%' else ''}"


def report(store: dict, pairs: list[str], days: float, explain: bool, now: datetime,
           impacts: tuple[str, ...] = IMPACTS) -> str:
    events = list(store.values())
    scores = currency_scores(events, now, days, impacts)
    lines = [f"Currency strength (last {days:g} days, half-life {HALF_LIFE_DAYS:g}d, "
             f"impact {'+'.join(impacts)})"]
    for c, v in sorted(scores.items(), key=lambda kv: -kv[1]["score"]):
        bar = ("+" if v["score"] > 0 else "-") * min(20, round(abs(v["score"])))
        lines.append(f"  {c}  {v['score']:+6.2f}  {bar}")
    lines.append("\nPair bias")
    for p in pairs:
        d, label = pair_bias(p, scores)
        b, q = p.upper()[:3], p.upper()[3:]
        lines.append(f"  {p.upper()}  {d:+6.2f}  {label:<8} ({b} {scores[b]['score']:+.2f} vs "
                     f"{q} {scores[q]['score']:+.2f})")
    if explain:
        shown = {c for p in pairs for c in (p.upper()[:3], p.upper()[3:])}
        lines.append("\nDrivers")
        for c in [c for c in MAJORS if c in shown]:
            drivers = scores[c]["drivers"][:5]
            lines.append(f"  {c}" + ("" if drivers else "  (no scored events)"))
            for s, ev in drivers:
                u = ev.get("unit")
                lines.append(f"    {s:+5.2f}  {ev['date_utc'][:16].replace('T', ' ')}  {ev['name']}  "
                             f"A {num(ev['actual'], u)}  C {num(ev['forecast'], u)}  P {num(ev['previous'], u)}")
    watch = watch_list(events, now)
    lines.append("\nWatch (next 48h, High impact, UTC)")
    if not watch:
        lines.append("None in store: `fetch --period recent` loads the next 4 days")
    for ev in watch:
        lean = ""
        c, f, pv, u = classify(ev["name"]), ev.get("forecast"), ev.get("previous"), ev.get("unit")
        if c and f is not None and pv is not None and f != pv:
            lean = f"  expected {'+' if (f - pv) * c[1] > 0 else '-'}{ev['currency']}"
        lines.append(f"  {ev['date_utc'][:16].replace('T', ' ')}  {ev['currency']}  {ev['name']}  "
                     f"C {num(f, u)}  P {num(pv, u)}{lean}")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="read the FXStreet calendar into data/calendar.json")
    f.add_argument("--period", choices=list(PERIODS), default="week",
                   help="date preset to load (default: week; recent = last 2h to +4 days)")
    b = sub.add_parser("bias", help="print currency strength and pair bias")
    b.add_argument("--pair", nargs="+", default=list(DEFAULT_PAIRS), help="e.g. AUDNZD EURUSD")
    b.add_argument("--days", type=float, default=14, help="look-back window (default 14)")
    b.add_argument("--explain", action="store_true", help="show the events behind each score")
    for sp in (f, b):
        sp.add_argument("--impact", nargs="+", type=str.capitalize, choices=list(IMPACT_W),
                        default=list(IMPACTS), help="impact levels to keep (default: high medium)")
    a = p.parse_args()
    now = datetime.now(timezone.utc)
    impacts = tuple(a.impact)

    if a.cmd == "fetch":
        events = [e for e in (normalise(r) for r in browse(a.period)) if e and e["impact"] in impacts]
        store = load_store()
        added, updated = upsert(store, events)
        save_store(store)
        released = sum(e["actual"] is not None for e in events)
        print(f"{now:%Y-%m-%d %H:%M} UTC  {a.period}: {len(events)} events ({released} released)  "
              f"added {added}  updated {updated}  store {len(store)}")
        return

    for pair in a.pair:
        try:
            pair_bias(pair, {c: {"score": 0} for c in MAJORS})
        except ValueError as e:
            p.error(str(e))
    store = load_store()
    if not store:
        sys.exit(f"No data in {STORE}. Run: python fxstreet.py fetch")
    print(report(store, a.pair, a.days, a.explain, now, impacts))


if __name__ == "__main__":
    main()
