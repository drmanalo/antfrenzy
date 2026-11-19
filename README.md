# antfrenzy

A small command-line tool that places an IG market order (BUY or SELL) with a stop and a 1:1 limit,
sized so that a stop loses about 1% of your capital.

```
size (per point) = risk / stop_pts        loss at stop = size * stop_pts
risk             = CAPITAL * RISK_APPETITE / 100
```

> **Warning:** this tool can place real orders. It uses the DEMO account unless you pass `--live`.
> Always run with `--dry-run` first, and test on DEMO before going live.

## Clone

```
git clone git@github.com:drmanalo/antfrenzy.git
cd antfrenzy
```

## Install dependencies

Requires Python 3.10 or newer.

```
python -m venv .venv
source .venv/bin/activate        # fish: source .venv/bin/activate.fish
pip install -r requirements.txt
pip install pytest               # only needed to run the tests
```

## Configure

Copy the `.env.example` to `.env` file and fill in your IG credentials:

| Variable        | Required | Description                                                     |
|-----------------|----------|-----------------------------------------------------------------|
| `IG_USERNAME`   | yes      | IG username                   |
| `IG_PASSWORD`   | yes      | IG password. Wrap it in single quotes if it contains `#` or `$` |
| `IG_API_KEY`    | yes      | IG API key. Demo and live accounts have different keys          |
| `IG_ACC_NUMBER` | no       | Account number, if you have more than one                       |
| `CAPITAL`       | no       | Capital used for sizing (default `10000`)                       |
| `RISK_APPETITE` | no       | Risk per trade in percent (default `1`)                         |

Values in `.env` take priority over variables already set in your shell.

## Run the tests

The tests mock IG, so they need no network, credentials or account.

```
python -m pytest -v
```

## Usage

Find the epic for a market. The script takes an epic, not a ticker:

```
python tradeig.py --search NVDA
```

Dry run: it computes the size and the stop and limit and prints them, but sends nothing:

```
python tradeig.py --epic CS.D.EURUSD.MINI.IP --stop-pts 20 --direction sell --dry-run
```

Example output:

```
DEMO | EUR/USD Mini (CS.D.EURUSD.MINI.IP)
SELL 5.0 USD/pt @ ~1.09727
Stop  1.09927 (-USD100.00)
Limit 1.09527 (+USD100.00)
Note: spread (0.9 pts) and slippage are not included in risk.
```

Remove `--dry-run` to place the order. The script asks for confirmation (`Send order? [y/N]`).

### Options

| Option                        | Description                                                         |
|-------------------------------|---------------------------------------------------------------------|
| `--epic EPIC`                 | IG market epic                                                      |
| `--stop-pts N`                | Stop distance in points                                             |
| `--direction {buy,sell}`      | Default `buy`                                                       |
| `--size N`                    | Size per point. Overrides the risk-based size                       |
| `--currency CODE`             | Deal currency, e.g. `GBP`                                           |
| `--dry-run`                   | Compute and print only                                              |
| `--yes`                       | Skip the confirmation prompt                                        |
| `--live`                      | Use the LIVE account (default is DEMO)                              |
| `--search TEXT`               | Search markets and exit                                             |

## Notes and limits

- Stops are normal stops, not guaranteed. Spread and slippage are not included in the risk, and
  a fast market can fill a stop beyond its level.
- Points are not the same size on every market: 1 point is 0.0001 for EUR/USD and 0.01 for US shares.
- If IG returns no bid or offer for a market (for example some 24-hour share markets out of hours),
  the script still sizes the trade but prints distances instead of price levels, and IG may reject the order.
- The risk figure is in the deal currency and is not converted to your account currency.
- Each run opens a new position (`force_open`), so running a command twice opens two positions.
