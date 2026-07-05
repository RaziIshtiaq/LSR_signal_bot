"""
Liquidity Sweep Reversal (LSR) Signal Bot
==========================================
Scans high-volume USDT-M Binance Futures pairs for liquidity-sweep
reversal setups and pushes signal alerts to Telegram.

THIS BOT DOES NOT TRADE FOR YOU. It only detects patterns and sends
notifications. You decide whether to act on them.
"""

import os
import time
import math
import json
import requests
from datetime import datetime, timezone

# ============================== CONFIG ==============================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

RUN_ONCE = os.environ.get("RUN_ONCE", "0") == "1"

STATE_FILE = "lsr_state.json"

BINANCE_FAPI = "https://fapi.binance.com"

TIMEFRAME = "5m"
LOOKBACK_CANDLES = 50
SWEEP_MIN_WICK_PCT = 0.05
VOLUME_SPIKE_MULT = 1.8
TOP_N_VOLUME_COINS = 100
SCAN_INTERVAL_SECONDS = 60
COOLDOWN_MINUTES = 30

MIN_LEVERAGE = 100
MAX_LEVERAGE = 200
RISK_PCT_OF_MARGIN = 5

# ============================== STATE ==============================

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            raw = json.load(f)
        return {tuple(k.split("|")): datetime.fromisoformat(v) for k, v in raw.items()}
    except Exception:
        return {}


def save_state(state):
    try:
        raw = {f"{k[0]}|{k[1]}": v.isoformat() for k, v in state.items()}
        with open(STATE_FILE, "w") as f:
            json.dump(raw, f)
    except Exception as e:
        print(f"[State save warning] {e}")


_last_signal_time = load_state()

# ============================== DATA FETCH ==============================

def get_top_volume_symbols(n=TOP_N_VOLUME_COINS):
    url = f"{BINANCE_FAPI}/fapi/v1/ticker/24hr"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    usdt_perps = [
        d for d in data
        if d["symbol"].endswith("USDT")
        and not any(x in d["symbol"] for x in ["_", "UP", "DOWN", "BEAR", "BULL"])
    ]
    usdt_perps.sort(key=lambda d: float(d["quoteVolume"]), reverse=True)
    return [d["symbol"] for d in usdt_perps[:n]]


def get_klines(symbol, interval=TIMEFRAME, limit=LOOKBACK_CANDLES + 5):
    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    raw = resp.json()

    candles = []
    for k in raw:
        candles.append({
            "open_time": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "taker_buy_base": float(k[9]),
        })
    return candles


# ============================== ANALYSIS ==============================

def compute_delta(candle):
    buy_vol = candle["taker_buy_base"]
    sell_vol = candle["volume"] - buy_vol
    return buy_vol - sell_vol


def detect_signal(symbol, candles):
    if len(candles) < LOOKBACK_CANDLES + 2:
        return None

    closed = candles[:-1]
    window = closed[-LOOKBACK_CANDLES:-1]
    trigger = closed[-1]

    swing_high = max(c["high"] for c in window)
    swing_low = min(c["low"] for c in window)
    avg_volume = sum(c["volume"] for c in window) / len(window)

    price_ref = trigger["close"]
    wick_thresh = price_ref * (SWEEP_MIN_WICK_PCT / 100)

    signal = None

    if trigger["high"] > swing_high + wick_thresh and trigger["close"] < swing_high:
        confirmations = []
        if trigger["volume"] > avg_volume * VOLUME_SPIKE_MULT:
            confirmations.append("Volume spike on sweep")
        if compute_delta(trigger) < 0:
            confirmations.append("Sell delta dominance (CVD)")
        wick_size = trigger["high"] - max(trigger["open"], trigger["close"])
        body_size = abs(trigger["close"] - trigger["open"]) or 1e-9
        if wick_size > body_size * 1.5:
            confirmations.append("Long upper wick rejection")

        if len(confirmations) >= 2:
            entry = trigger["close"]
            stop_loss = trigger["high"] * 1.0015
            target = (swing_high + swing_low) / 2
            signal = {
                "direction": "SHORT",
                "entry": entry,
                "stop_loss": stop_loss,
                "target": target,
                "confirmations": confirmations,
            }

    elif trigger["low"] < swing_low - wick_thresh and trigger["close"] > swing_low:
        confirmations = []
        if trigger["volume"] > avg_volume * VOLUME_SPIKE_MULT:
            confirmations.append("Volume spike on sweep")
        if compute_delta(trigger) > 0:
            confirmations.append("Buy delta dominance (CVD)")
        wick_size = min(trigger["open"], trigger["close"]) - trigger["low"]
        body_size = abs(trigger["close"] - trigger["open"]) or 1e-9
        if wick_size > body_size * 1.5:
            confirmations.append("Long lower wick rejection")

        if len(confirmations) >= 2:
            entry = trigger["close"]
            stop_loss = trigger["low"] * 0.9985
            target = (swing_high + swing_low) / 2
            signal = {
                "direction": "LONG",
                "entry": entry,
                "stop_loss": stop_loss,
                "target": target,
                "confirmations": confirmations,
            }

    if signal:
        signal["symbol"] = symbol
        signal["confidence"] = confidence_label(len(signal["confirmations"]))
        signal["stop_distance_pct"] = abs(signal["entry"] - signal["stop_loss"]) / signal["entry"] * 100
        signal["suggested_leverage"] = suggest_leverage(signal["stop_distance_pct"])
    return signal


def confidence_label(num_confirmations):
    if num_confirmations >= 3:
        return "High"
    elif num_confirmations == 2:
        return "Medium"
    return "Low"


def suggest_leverage(stop_distance_pct):
    if stop_distance_pct <= 0:
        return MIN_LEVERAGE
    raw = RISK_PCT_OF_MARGIN / stop_distance_pct * 10
    lev = max(MIN_LEVERAGE, min(MAX_LEVERAGE, raw))
    return round(lev)


def liquidation_distance_pct(leverage):
    return round(100 / leverage, 2)


# ============================== TELEGRAM ==============================

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[Telegram error] {e}")


def format_signal_message(sig):
    liq_dist = liquidation_distance_pct(sig["suggested_leverage"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    msg = (
        f"<b>⚡ LSR SIGNAL — {sig['symbol']}</b>\n"
        f"Direction: <b>{sig['direction']}</b>\n"
        f"Confidence: <b>{sig['confidence']}</b> ({len(sig['confirmations'])} confirmations)\n"
        f"Time: {now}\n\n"
        f"Entry: {sig['entry']:.6f}\n"
        f"Stop-loss: {sig['stop_loss']:.6f}  ({sig['stop_distance_pct']:.2f}% away)\n"
        f"Target (range midpoint): {sig['target']:.6f}\n\n"
        f"Suggested leverage: {sig['suggested_leverage']}x\n"
        f"⚠️ Liquidation distance at this leverage: ~{liq_dist}%\n"
        f"(A {liq_dist}% adverse move = full liquidation. Size your margin accordingly.)\n\n"
        f"Confirmations:\n- " + "\n- ".join(sig["confirmations"]) + "\n\n"
        f"<i>Heuristic signal, not financial advice. Backtest not statistically validated.</i>"
    )
    return msg


# ============================== MAIN LOOP ==============================

def scan_once():
    try:
        symbols = get_top_volume_symbols()
    except Exception as e:
        print(f"[Error fetching symbol list] {e}")
        return

    for symbol in symbols:
        try:
            candles = get_klines(symbol)
            sig = detect_signal(symbol, candles)
            if not sig:
                continue

            key = (symbol, sig["direction"])
            last_time = _last_signal_time.get(key)
            now = datetime.now(timezone.utc)
            if last_time and (now - last_time).total_seconds() < COOLDOWN_MINUTES * 60:
                continue

            message = format_signal_message(sig)
            send_telegram_message(message)
            _last_signal_time[key] = now
            print(f"[Signal sent] {symbol} {sig['direction']} - {sig['confidence']}")

        except Exception as e:
            print(f"[Error scanning {symbol}] {e}")

        time.sleep(0.2)

    save_state(_last_signal_time)


def main():
    print("This bot ONLY sends signals. It never places trades.")
    if RUN_ONCE:
        print("RUN_ONCE mode: scanning once and exiting (GitHub Actions style).")
        scan_once()
    else:
        print("Loop mode: scanning every", SCAN_INTERVAL_SECONDS, "seconds.")
        while True:
            scan_once()
            time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
