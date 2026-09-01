#!/usr/bin/env python3
"""
BB Wick-Touch Mean Reversion — Telegram Signal Scanner
=======================================================
Ports the Pine Script v6 strategy "BB Wick-Touch Mean Reversion + Directional
Accuracy" (bb_wick_touch_mr_v1) into a standalone Python signal scanner.

This is a SIGNAL SCANNER, NOT a live trading / order execution system.
It reads PUBLIC market data via ccxt (no API keys, no funds at risk) and only
sends Telegram alerts. It never places real orders.

Ported logic (mirrors the .pine file section by section):
  - Bollinger Bands: length 20, mult 2.0, selectable basis MA (SMA/EMA/SMMA/WMA/VWMA),
    using Pine's POPULATION (biased, ddof=0) stdev.
  - Entry trigger: "Wick Touch" (high/low pierces band) or "Close Beyond Band".
  - Optional Band-Width (volatility) filter.
  - Optional Band-Walk (trend-continuation guard) filter — exact state-machine port.
  - One virtual position at a time per symbol (mirrors `strategy.position_size == 0`
    gating), with TP at the basis (or a fixed %) and a % stop-loss, used only to
    decide when the scanner is "flat" again and free to alert on a fresh signal.

SECURITY: Never hardcode your Telegram bot token or chat ID in this file.
Set them as environment variables (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID) instead —
see the deployment guide for how to do this on your hosting platform.
"""

import os
import sys
import json
import time
import math
import pandas as pd
import pandas_ta as pta
import requests
import ccxt

# =====================================================================================
# CONFIG — mirrors the Pine script's input groups 1:1.
# Every value can be overridden with an environment variable of the same (upper) name.
# Only TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are required; everything else has a
# sensible default that matches the Pine script's own defaults.
# =====================================================================================

def _bool_env(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

CONFIG = {
    # --- Telegram (REQUIRED) ---
    "telegram_token":   os.environ.get("TELEGRAM_TOKEN", ""),
    "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),

    # --- Market ---
    "exchange_id": os.environ.get("EXCHANGE_ID", "binance"),
    "symbol":      os.environ.get("SYMBOL", "BTC/USDT"),
    "timeframe":   os.environ.get("TIMEFRAME", "1h"),  # ccxt style: 1m,5m,15m,1h,4h,1d...

    # --- Bollinger Band / Signal Timeframe group ---
    "bb_length": int(os.environ.get("BB_LENGTH", 20)),
    "ma_type":   os.environ.get("MA_TYPE", "SMA"),      # SMA, EMA, SMMA, WMA, VWMA
    "bb_mult":   float(os.environ.get("BB_MULT", 2.0)),

    # --- Entry Logic group ---
    "entry_mode": os.environ.get("ENTRY_MODE", "wick_touch"),  # wick_touch | close_beyond

    # --- Volatility Filter (Band Width) group ---
    "use_band_width_filter": _bool_env("USE_BAND_WIDTH_FILTER", False),
    "min_band_width_pct":    float(os.environ.get("MIN_BAND_WIDTH_PCT", 1.0)),

    # --- Band-Walk Filter (Trend Continuation Guard) group ---
    "use_band_walk_filter": _bool_env("USE_BAND_WALK_FILTER", False),

    # --- Risk Management group (used only to compute the TP/SL shown in alerts) ---
    "use_stop_loss": _bool_env("USE_STOP_LOSS", True),
    "sl_pct":        float(os.environ.get("SL_PCT", 3.0)),
    "use_tp":        _bool_env("USE_TP", True),
    "use_fixed_tp":  _bool_env("USE_FIXED_TP", False),
    "tp_pct":        float(os.environ.get("TP_PCT", 1.5)),

    # --- Runtime ---
    "state_file":          os.environ.get("STATE_FILE", "bb_wickmr_state.json"),
    "candles_fetch_limit": int(os.environ.get("CANDLES_FETCH_LIMIT", 300)),
    "poll_buffer_seconds": int(os.environ.get("POLL_BUFFER_SECONDS", 15)),
}

TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400,
}


def validate_config(cfg):
    if not cfg["telegram_token"] or not cfg["telegram_chat_id"]:
        sys.exit(
            "FATAL: TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set as environment "
            "variables. Never hardcode them in this file."
        )
    if cfg["timeframe"] not in TIMEFRAME_SECONDS:
        sys.exit(f"FATAL: unsupported TIMEFRAME '{cfg['timeframe']}'. "
                  f"Choose one of: {', '.join(TIMEFRAME_SECONDS)}")
    if cfg["entry_mode"] not in ("wick_touch", "close_beyond"):
        sys.exit("FATAL: ENTRY_MODE must be 'wick_touch' or 'close_beyond'.")
    if cfg["ma_type"] not in ("SMA", "EMA", "SMMA", "WMA", "VWMA"):
        sys.exit("FATAL: MA_TYPE must be one of SMA, EMA, SMMA, WMA, VWMA.")


# =====================================================================================
# TELEGRAM
# =====================================================================================

def send_telegram(cfg, message: str):
    url = f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage"
    payload = {
        "chat_id": cfg["telegram_chat_id"],
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"[telegram] non-200 response: {r.status_code} {r.text}")
    except Exception as e:
        print(f"[telegram] send failed: {e}")


# =====================================================================================
# STATE PERSISTENCE (survives restarts / redeploys — see deployment guide)
# =====================================================================================

def load_state(cfg):
    default_state = {
        "last_candle_ts": None,      # ms timestamp of the last fully processed candle
        "short_walk_state": "idle",  # band-walk filter state (Rule 1)
        "long_walk_state": "idle",   # band-walk filter state (Rule 2)
        "block_short": False,
        "block_long": False,
        "position": None,            # None | {"side","entry","tp","sl","opened_ts"}
    }
    if os.path.exists(cfg["state_file"]):
        try:
            with open(cfg["state_file"], "r") as f:
                default_state.update(json.load(f))
        except Exception as e:
            print(f"[state] failed to load existing state, starting fresh: {e}")
    return default_state


def save_state(cfg, state):
    tmp_path = cfg["state_file"] + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, cfg["state_file"])


# =====================================================================================
# BOLLINGER BAND CALC — matches Pine's ta.stdev (population, ddof=0), not pandas' default
# =====================================================================================

def compute_ma(series: pd.Series, length: int, ma_type: str) -> pd.Series:
    if ma_type == "SMA":
        return series.rolling(length).mean()
    if ma_type == "EMA":
        return series.ewm(span=length, adjust=False, min_periods=length).mean()
    if ma_type == "SMMA":  # Pine's "SMMA (RMA)"
        return pta.rma(series, length=length)
    if ma_type == "WMA":
        return pta.wma(series, length=length)
    raise ValueError(f"compute_ma: unsupported ma_type '{ma_type}' (VWMA handled separately)")


def compute_bollinger(df: pd.DataFrame, length: int, mult: float, ma_type: str) -> pd.DataFrame:
    df = df.copy()
    if ma_type == "VWMA":
        df["basis"] = pta.vwma(df["close"], df["volume"], length=length)
    else:
        df["basis"] = compute_ma(df["close"], length, ma_type)
    df["stdev"] = df["close"].rolling(length).std(ddof=0)  # population stdev, matches Pine
    df["upper"] = df["basis"] + mult * df["stdev"]
    df["lower"] = df["basis"] - mult * df["stdev"]
    return df


# =====================================================================================
# DATA FETCH — only fully closed candles are ever evaluated (no repaint on the forming bar)
# =====================================================================================

def fetch_closed_candles(exchange, symbol, timeframe, limit):
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    now_ms = exchange.milliseconds()
    tf_ms = TIMEFRAME_SECONDS[timeframe] * 1000
    df = df[df["ts"] + tf_ms <= now_ms].reset_index(drop=True)
    return df


# =====================================================================================
# CORE SIGNAL LOGIC — advances the state machine by exactly one closed candle.
# Ported section-by-section from the .pine file's ENTRY SIGNALS / BAND-WALK FILTER /
# order-execution blocks.
# =====================================================================================

def fmt_price(p):
    return f"{p:,.6g}"


def process_candle(row, state, cfg):
    """Advance state by one closed candle. Returns a list of alert message strings."""
    alerts = []
    high, low, close = row["high"], row["low"], row["close"]
    basis, upper, lower = row["basis"], row["upper"], row["lower"]

    if any(pd.isna(x) for x in (basis, upper, lower)):
        return alerts  # not enough history yet to have a valid band on this bar

    # ---- entry trigger conditions (pine: longCond / shortCond) ----
    if cfg["entry_mode"] == "wick_touch":
        long_cond = low <= lower
        short_cond = high >= upper
    else:  # close_beyond
        long_cond = close < lower
        short_cond = close > upper

    band_width_pct = (upper - lower) / basis * 100 if basis else 0.0
    passes_band_width = (not cfg["use_band_width_filter"]) or (band_width_pct >= cfg["min_band_width_pct"])

    # ---- band-walk filter state machine (exact port, same order as the .pine file) ----
    touch_lower_now = low <= lower
    touch_upper_now = high >= upper

    # Rule 1 — builds toward block_short
    if not state["block_short"]:
        sws = state["short_walk_state"]
        if sws == "idle":
            if touch_lower_now:
                state["short_walk_state"] = "touchedLower"
        elif sws == "touchedLower":
            if touch_upper_now:
                state["short_walk_state"] = "touchedUpperOnce"
        elif sws == "touchedUpperOnce":
            if touch_upper_now:
                state["block_short"] = True
                state["short_walk_state"] = "idle"
            elif low <= basis:
                state["short_walk_state"] = "touchedLower" if touch_lower_now else "idle"

    # Rule 2 — mirror image, builds toward block_long
    if not state["block_long"]:
        lws = state["long_walk_state"]
        if lws == "idle":
            if touch_upper_now:
                state["long_walk_state"] = "touchedUpper"
        elif lws == "touchedUpper":
            if touch_lower_now:
                state["long_walk_state"] = "touchedLowerOnce"
        elif lws == "touchedLowerOnce":
            if touch_lower_now:
                state["block_long"] = True
                state["long_walk_state"] = "idle"
            elif high >= basis:
                state["long_walk_state"] = "touchedUpper" if touch_upper_now else "idle"

    # Resets — only a CLOSE back through the Basis clears a block
    if state["block_short"] and close < basis:
        state["block_short"] = False
        state["short_walk_state"] = "idle"
    if state["block_long"] and close > basis:
        state["block_long"] = False
        state["long_walk_state"] = "idle"

    passes_walk_short = (not cfg["use_band_walk_filter"]) or (not state["block_short"])
    passes_walk_long = (not cfg["use_band_walk_filter"]) or (not state["block_long"])

    ts_str = pd.to_datetime(row["ts"], unit="ms", utc=True).strftime("%Y-%m-%d %H:%M UTC")

    # ---- diagnostic line, printed every candle regardless of outcome (visible in Railway logs) ----
    print(
        f"[check] {ts_str} | O={row['open']:.2f} H={high:.2f} L={low:.2f} C={close:.2f} | "
        f"basis={basis:.2f} upper={upper:.2f} lower={lower:.2f} | "
        f"long_cond={long_cond} short_cond={short_cond} | "
        f"position={'flat' if state['position'] is None else state['position']['side']}"
    )

    # ---- 1) manage an already-open virtual position — check exit before any new entry ----
    pos = state["position"]
    if pos is not None:
        # TP is a MOVING target when not using a fixed % — it tracks the CURRENT bar's
        # basis every candle. This mirrors the Pine script re-issuing strategy.exit with
        # the live basis value every bar (a real behavior of the .pine file, not an
        # approximation). SL stays frozen at the entry price, matching
        # strategy.position_avg_price, which doesn't change since this script never pyramids.
        if not cfg["use_fixed_tp"]:
            pos["tp"] = basis

        if pos["side"] == "long":
            hit_tp = cfg["use_tp"] and high >= pos["tp"]
            hit_sl = cfg["use_stop_loss"] and low <= pos["sl"]
        else:
            hit_tp = cfg["use_tp"] and low <= pos["tp"]
            hit_sl = cfg["use_stop_loss"] and high >= pos["sl"]

        # If a single candle's range spans both levels, we can't know which was hit
        # first from OHLCV alone — assume the worse outcome (stop-loss) as a
        # conservative default.
        if hit_sl:
            pnl_pct = ((pos["sl"] - pos["entry"]) / pos["entry"] * 100) * (1 if pos["side"] == "long" else -1)
            alerts.append(
                f"\U0001F6D1 *STOP-LOSS HIT* — {cfg['symbol']} ({cfg['timeframe']})\n"
                f"Side: {pos['side'].upper()}\n"
                f"Entry: {fmt_price(pos['entry'])} -> Exit: {fmt_price(pos['sl'])}\n"
                f"Result: {pnl_pct:+.2f}%\n"
                f"Time: {ts_str}"
            )
            state["position"] = None
        elif hit_tp:
            pnl_pct = ((pos["tp"] - pos["entry"]) / pos["entry"] * 100) * (1 if pos["side"] == "long" else -1)
            alerts.append(
                f"\u2705 *TAKE-PROFIT HIT* — {cfg['symbol']} ({cfg['timeframe']})\n"
                f"Side: {pos['side'].upper()}\n"
                f"Entry: {fmt_price(pos['entry'])} -> Exit: {fmt_price(pos['tp'])}\n"
                f"Result: {pnl_pct:+.2f}%\n"
                f"Time: {ts_str}"
            )
            state["position"] = None

    # ---- 2) look for a new entry only while flat (pine: strategy.position_size == 0) ----
    if state["position"] is None:
        entry_side = None
        if long_cond and passes_band_width and passes_walk_long:
            entry_side = "long"
        elif short_cond and not long_cond and passes_band_width and passes_walk_short:
            entry_side = "short"

        if entry_side is not None:
            entry_px = close  # approximation of strategy.entry's fill price (see README)
            if entry_side == "long":
                tp = entry_px * (1 + cfg["tp_pct"] / 100) if cfg["use_fixed_tp"] else basis
                sl = entry_px * (1 - cfg["sl_pct"] / 100)
            else:
                tp = entry_px * (1 - cfg["tp_pct"] / 100) if cfg["use_fixed_tp"] else basis
                sl = entry_px * (1 + cfg["sl_pct"] / 100)

            state["position"] = {
                "side": entry_side, "entry": entry_px, "tp": tp, "sl": sl,
                "opened_ts": int(row["ts"]),
            }

            arrow = "\U0001F53C" if entry_side == "long" else "\U0001F53D"
            tp_label = "fixed %" if cfg["use_fixed_tp"] else "basis"
            alerts.append(
                f"{arrow} *{entry_side.upper()} SIGNAL* — {cfg['symbol']} ({cfg['timeframe']})\n"
                f"Trigger: {cfg['entry_mode'].replace('_', ' ').title()}\n"
                f"Entry (approx): {fmt_price(entry_px)}\n"
                f"Basis: {fmt_price(basis)}  Upper: {fmt_price(upper)}  Lower: {fmt_price(lower)}\n"
                f"Take-Profit: {fmt_price(tp)} ({tp_label})\n"
                f"Stop-Loss: {fmt_price(sl)}\n"
                f"Time: {ts_str}"
            )

    return alerts


# =====================================================================================
# MAIN LOOP
# =====================================================================================

def seconds_until_next_close(tf_seconds, buffer_s):
    now = time.time()
    next_close = (math.floor(now / tf_seconds) + 1) * tf_seconds
    return max(1, next_close - now + buffer_s)


def run():
    cfg = CONFIG
    validate_config(cfg)
    print(f"Starting BB Wick-Touch MR scanner | {cfg['symbol']} {cfg['timeframe']} "
          f"on {cfg['exchange_id']} | entry_mode={cfg['entry_mode']} ma_type={cfg['ma_type']}")

    exchange_class = getattr(ccxt, cfg["exchange_id"])
    exchange = exchange_class({"enableRateLimit": True})

    state = load_state(cfg)
    tf_seconds = TIMEFRAME_SECONDS[cfg["timeframe"]]

    send_telegram(
        cfg,
        f"\U0001F916 BB Wick-Touch MR scanner *started*\n"
        f"Symbol: {cfg['symbol']}\nTimeframe: {cfg['timeframe']}\n"
        f"Entry mode: {cfg['entry_mode'].replace('_', ' ').title()}\nMA type: {cfg['ma_type']}"
    )

    while True:
        try:
            df = fetch_closed_candles(exchange, cfg["symbol"], cfg["timeframe"], cfg["candles_fetch_limit"])
            df = compute_bollinger(df, cfg["bb_length"], cfg["bb_mult"], cfg["ma_type"])

            if state["last_candle_ts"] is None and len(df) > 1:
                # First-ever run: silently replay history so the band-walk filter and
                # virtual-position state are correctly primed, WITHOUT spamming Telegram
                # with historical signals. Only the most recent closed candle can alert.
                print(f"First run — silently backfilling state across {len(df) - 1} historical candles...")
                for _, row in df.iloc[:-1].iterrows():
                    process_candle(row, state, cfg)
                    state["last_candle_ts"] = int(row["ts"])
                save_state(cfg, state)
                new_rows = df.iloc[-1:]
            elif state["last_candle_ts"] is None:
                new_rows = df
            else:
                new_rows = df[df["ts"] > state["last_candle_ts"]]

            for _, row in new_rows.iterrows():
                alerts = process_candle(row, state, cfg)
                for msg in alerts:
                    print(f"[alert] {msg}")
                    send_telegram(cfg, msg)
                state["last_candle_ts"] = int(row["ts"])
                save_state(cfg, state)

        except ccxt.NetworkError as e:
            print(f"[network] {e} — will retry next cycle")
        except Exception as e:
            print(f"[error] unexpected: {e}")

        sleep_s = seconds_until_next_close(tf_seconds, cfg["poll_buffer_seconds"])
        print(f"Sleeping {sleep_s:.0f}s until next {cfg['timeframe']} candle close check...")
        time.sleep(sleep_s)


if __name__ == "__main__":
    run()
