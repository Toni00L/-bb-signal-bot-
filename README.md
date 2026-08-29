# BB Wick-Touch Mean Reversion — Telegram Signal Scanner

Ports the Pine Script v6 strategy **"BB Wick-Touch Mean Reversion + Directional Accuracy"**
(`bb_wick_touch_mr_v1`) into a standalone Python scanner that sends Telegram alerts.

**This is a signal scanner only — it never places real orders.** It reads public
market data via `ccxt` and posts to Telegram via `requests`.

## What it ports from the Pine script

| Pine feature | Status |
|---|---|
| Bollinger Bands (length 20, mult 2.0, 5 selectable MA types) | ✅ exact, incl. population/biased stdev |
| Entry trigger: Wick Touch / Close Beyond Band | ✅ exact |
| Band-Width (volatility) filter | ✅ exact |
| Band-Walk (trend-continuation guard) filter | ✅ exact state-machine port |
| One position at a time (`strategy.position_size == 0` gating) | ✅ via a tracked "virtual position" |
| TP at basis or fixed %, % stop-loss | ✅ used to compute alert levels & to know when the scanner is flat again |
| Directional Accuracy / Drawdown diagnostic tables | ➖ not ported — backtest-only reporting, irrelevant to a live alert bot |
| Actual order execution / position sizing / commission | ➖ out of scope — this is a scanner, not an execution system |

## Known simplifications (read before trusting the alerts blindly)

- **Entry price** in the alert is the *closing price of the signal candle*, used as an
  approximation of where `strategy.entry` would fill. A real fill will differ slightly.
- **Same-candle exit + re-entry**: if one candle's range hits your stop-loss *and* a
  new entry condition, the scanner closes the old virtual position and can open a new
  one in the same candle. This mirrors realistic intrabar behavior but is an
  approximation, since OHLCV data alone can't tell you which happened first.
- **TP/SL same-bar ambiguity**: if a single candle's range touches both your TP and SL
  levels, the scanner assumes the stop-loss was hit first (the conservative/worse-case
  assumption).

## Configuration

All settings are environment variables so you never hardcode secrets or have to edit
code to deploy. Only `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` are required — everything
else defaults to the Pine script's own default inputs.

| Env var | Default | Notes |
|---|---|---|
| `TELEGRAM_TOKEN` | *(required)* | from @BotFather |
| `TELEGRAM_CHAT_ID` | *(required)* | your numeric Telegram user/chat ID |
| `EXCHANGE_ID` | `binance` | any public ccxt exchange id |
| `SYMBOL` | `BTC/USDT` | ccxt unified symbol |
| `TIMEFRAME` | `1h` | one of `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d` |
| `BB_LENGTH` | `20` | |
| `MA_TYPE` | `SMA` | `SMA \| EMA \| SMMA \| WMA \| VWMA` |
| `BB_MULT` | `2.0` | |
| `ENTRY_MODE` | `wick_touch` | `wick_touch \| close_beyond` |
| `USE_BAND_WIDTH_FILTER` | `false` | |
| `MIN_BAND_WIDTH_PCT` | `1.0` | |
| `USE_BAND_WALK_FILTER` | `false` | |
| `USE_STOP_LOSS` | `true` | |
| `SL_PCT` | `3.0` | |
| `USE_TP` | `true` | |
| `USE_FIXED_TP` | `false` | `false` = TP at basis (midline), `true` = fixed % |
| `TP_PCT` | `1.5` | only used when `USE_FIXED_TP=true` |
| `STATE_FILE` | `bb_wickmr_state.json` | **must be on a persistent volume in the cloud** |

## Run locally first

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN="your-bot-token"
export TELEGRAM_CHAT_ID="your-chat-id"
export SYMBOL="BTC/USDT"
export TIMEFRAME="1h"
python3 bb_wickmr_signal_bot.py
```

You should see a "scanner started" message land in Telegram within a few seconds,
followed by log lines counting down to the next candle close.

See the deployment walkthrough for taking this to the cloud with persistent state.
