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

## Economic calendar bias

`fxstreet.py` turns released economic data into a strength score for each currency and a bias for each pair.
It follows the monetary-policy chain: hot inflation, strong jobs or strong growth make a central bank hawkish,
which means higher rates, carry-trade demand and a stronger currency. Misses work the other way.

```
event score    = surprise (-1..+1, + = good for the currency) * impact weight * category weight * time decay
currency score = sum of the currency's event scores over --days (default 14; half-life 7 days)
pair bias      = base score - quote score         e.g. AUDNZD = AUD - NZD
```

- **Surprise** uses FXStreet's own deviation score for its size, and FXStreet's
  better/worse-than-expected verdict for its sign, both squashed with `tanh`. When FXStreet gives no verdict,
  the script compares Actual with Consensus (or with Previous) itself. Higher counts as bullish, except for
  unemployment rate and jobless claims, where higher counts as bearish.
- **Weights**: impact High 3, Medium 2. Category rates 2.0, inflation 1.5, employment 1.2, growth 1.0,
  sentiment and other 0.5. These are constants at the top of the script, so tune them as you learn.
- **Label**: Bullish at a score of +2 or more, Bearish at -2 or less, otherwise Neutral.

### Data source

The data comes from the [FXStreet economic calendar](https://www.fxstreet.com/economic-calendar). A headless
Chromium Playwright browser opens the page, picks a date preset, and keeps the calendar data
that the page loads for itself. No API key or account is needed. Install the browser once:

```
pip install -r requirements.txt
python -m playwright install chromium
```

### Usage

```
python fxstreet.py fetch                        # this week: schedule + released actuals
python fxstreet.py fetch --period last-month    # one-off backfill
python fxstreet.py fetch --period recent        # last 2 hours to next 4 days
python fxstreet.py bias --pair AUDNZD EURUSD --explain
```

The `--period` values are `recent`, `today`, `yesterday`, `week`, `last-week`, `month` and `last-month`.

Both `fetch` and `bias` keep only Medium and High impact events by default.
Use `--impact` to change this, for example `--impact high` or `--impact high medium low`. Change the
`IMPACTS` constant to change the default.

Events are stored in `data/calendar.json`, which git ignores. `fetch` merges new data into it by FXStreet event ID, so running it again fills in Actuals as they are released and never adds duplicates.

To keep the store current, run it from cron (`crontab -e`). This runs every 4 hours for this week's actuals, plus once a day for the upcoming days:

```
15 */4 * * * cd /path/to/antfrenzy && .venv/bin/python fxstreet.py fetch >> data/fetch.log 2>&1
45 6 * * *   cd /path/to/antfrenzy && .venv/bin/python fxstreet.py fetch --period recent >> data/fetch.log 2>&1
```

### Caveats

- The bias is fundamental context, not an entry signal. Confirm it with price action.
- This reads FXStreet's public page, not an official API, so it breaks if they change the page or the data
  behind it. `fetch` exits with an error rather than saving nothing quietly.
- Speeches are not scored, because FXStreet's speech scores are locked for free users. Events that FXStreet gives no verdict for and that match no keyword, such as holidays, are not scored either.
