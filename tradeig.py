#!/usr/bin/env python3
"""Quick IG CFD order: market BUY/SELL with stop and 1:1 limit, sized to risk 1% of capital.

size (GBP/pt) = risk / stop_pts   (loss at stop = size * stop_pts)

Usage:
  python ig_trade.py --search Anthropic
  python ig_trade.py --epic <EPIC> --stop-pts 260 [--direction sell] [--live] [--yes] [--dry-run]
"""
import argparse
import math
import os
import sys

from dotenv import load_dotenv
from trading_ig import IGService


def login(live: bool) -> IGService:
    load_dotenv()
    try:
        user, pw, key = os.environ["IG_USERNAME"], os.environ["IG_PASSWORD"], os.environ["IG_API_KEY"]
    except KeyError as e:
        sys.exit(f"Missing env var {e}. See .env.example")
    ig = IGService(user, pw, key, "LIVE" if live else "DEMO",
                   acc_number=os.environ.get("IG_ACC_NUMBER") or None)
    ig.create_session()
    return ig


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--epic", help="IG market epic")
    p.add_argument("--stop-pts", type=float, help="stop distance in points (limit = same, 1:1)")
    p.add_argument("--direction", choices=["buy", "sell"], default="buy")
    p.add_argument("--live", action="store_true", help="use LIVE account (default: DEMO)")
    p.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    p.add_argument("--dry-run", action="store_true", help="compute and print, do not send")
    p.add_argument("--search", metavar="TEXT", help="search markets for an epic and exit")
    a = p.parse_args()

    ig = login(a.live)

    if a.search:
        df = ig.search_markets(a.search)
        print(df[["epic", "instrumentName", "instrumentType", "marketStatus", "bid", "offer"]].to_string()
              if len(df) else "No markets found")
        return

    if not a.epic or not a.stop_pts:
        p.error("--epic and --stop-pts are required")
    if a.stop_pts <= 0:
        p.error("--stop-pts must be > 0")

    capital = float(os.environ.get("CAPITAL", 10000))
    risk_pct = float(os.environ.get("RISK_APPETITE", 1))
    risk = capital * risk_pct / 100

    m = ig.fetch_market_by_epic(a.epic)
    inst, snap, rules = m["instrument"], m["snapshot"], m["dealingRules"]
    if snap["marketStatus"] != "TRADEABLE":
        sys.exit(f"Market not tradeable: {snap['marketStatus']}")

    bid, offer = snap["bid"], snap["offer"]
    min_size = rules["minDealSize"]["value"]
    min_stop = rules["minNormalStopOrLimitDistance"]["value"]
    if rules["minNormalStopOrLimitDistance"].get("unit") == "PERCENTAGE":
        min_stop = min_stop / 100 * (offer if a.direction == "buy" else bid)
    currency = (inst.get("currencies") or [{}])[0].get("code", "GBP")

    if a.stop_pts < min_stop:
        sys.exit(f"stop-pts {a.stop_pts} below broker minimum {min_stop:.1f}")

    size = math.floor(risk / a.stop_pts * 100) / 100  # round DOWN: never exceed risk
    if size < min_size:
        need = math.ceil(risk / min_size)
        sys.exit(f"Size {size} < min deal size {min_size}. Use --stop-pts >= {need} to stay within "
                 f"{currency}{risk:.0f} risk.")

    entry = offer if a.direction == "buy" else bid
    sgn = 1 if a.direction == "buy" else -1
    stop_lvl, limit_lvl = entry - sgn * a.stop_pts, entry + sgn * a.stop_pts
    print(f"{'LIVE' if a.live else 'DEMO'} | {inst['name']} ({a.epic})\n"
          f"{a.direction.upper()} {size} {currency}/pt @ ~{entry}\n"
          f"Stop  {stop_lvl:.1f} (-{currency}{size * a.stop_pts:.2f})\n"
          f"Limit {limit_lvl:.1f} (+{currency}{size * a.stop_pts:.2f})\n"
          f"Note: spread ({offer - bid:.1f} pts) and slippage are not included in risk.")

    if a.dry_run:
        return
    if not a.yes and input("Send order? [y/N] ").strip().lower() != "y":
        sys.exit("Cancelled")

    r = ig.create_open_position(
        currency_code=currency, direction=a.direction.upper(), epic=a.epic,
        expiry=inst.get("expiry") or "-", force_open=True, guaranteed_stop=False,
        level=None, limit_distance=a.stop_pts, limit_level=None, order_type="MARKET",
        size=size, quote_id=None, stop_distance=a.stop_pts, stop_level=None,
        trailing_stop=False, trailing_stop_increment=None)
    print(f"{r.get('dealStatus')} ref={r.get('dealReference')} id={r.get('dealId')} "
          f"level={r.get('level')} stop={r.get('stopLevel')} limit={r.get('limitLevel')}")
    if r.get("dealStatus") != "ACCEPTED":
        sys.exit(f"Rejected: {r.get('reason')}")


if __name__ == "__main__":
    main()
