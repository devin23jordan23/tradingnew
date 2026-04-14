"""
schwab_data.py
--------------
All market data fetching via Schwab API.
Drop-in replacement for data.py (Polygon).
Same function names and return formats — scanner.py needs zero changes.
"""

import requests
import time
from datetime import datetime, date, timedelta
import pytz
from scanner.schwab_auth import get_access_token

ET   = pytz.timezone("America/New_York")
BASE = "https://api.schwabapi.com/marketdata/v1"


def _headers():
    """Build auth headers with fresh token."""
    token = get_access_token()
    return {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json"
    }


def _get(endpoint, params=None):
    """Base GET with auto-retry on 401 (expired token mid-session)."""
    for attempt in range(2):
        try:
            r = requests.get(
                f"{BASE}{endpoint}",
                headers=_headers(),
                params=params or {},
                timeout=10
            )
            if r.status_code == 401 and attempt == 0:
                # Token expired mid-session — force refresh and retry
                print("[DATA] Token expired mid-session, refreshing...")
                from scanner.schwab_auth import refresh_access_token, _load_tokens
                refresh_access_token(_load_tokens())
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            print(f"[DATA] Request error: {e}")
            if attempt == 0:
                time.sleep(2)
    return {}


# ─────────────────────────────────────────────
# PRICE HISTORY (candles)
# ─────────────────────────────────────────────

def get_candles(ticker, timespan="minute", multiplier=5, limit=60):
    """
    Fetch recent 5-minute candles for a ticker.
    Returns list of dicts: {open, high, low, close, volume, timestamp}
    """
    # Schwab frequency map
    freq_map = {1: "1min", 5: "5min", 15: "15min", 30: "30min", 60: "60min"}
    frequency = freq_map.get(multiplier, "5min")

    # Schwab needs millisecond epoch timestamps
    now       = datetime.now(ET)
    start     = now.replace(hour=4, minute=0, second=0, microsecond=0)
    start_ms  = int(start.timestamp() * 1000)
    end_ms    = int(now.timestamp() * 1000)

    params = {
        "periodType":     "day",
        "period":         1,
        "frequencyType":  "minute",
        "frequency":      multiplier,
        "startDate":      start_ms,
        "endDate":        end_ms,
        "needExtendedHoursData": "true"
    }

    try:
        data = _get(f"/pricehistory?symbol={ticker}", params)
        candles_raw = data.get("candles", [])
        candles = []
        for c in candles_raw:
            ts = datetime.fromtimestamp(c["datetime"] / 1000, tz=ET)
            candles.append({
                "open":      c["open"],
                "high":      c["high"],
                "low":       c["low"],
                "close":     c["close"],
                "volume":    c["volume"],
                "timestamp": ts
            })
        return candles
    except Exception as e:
        print(f"[DATA] Error fetching candles for {ticker}: {e}")
        return []


# ─────────────────────────────────────────────
# CURRENT PRICE
# ─────────────────────────────────────────────

def get_current_price(ticker):
    """Get latest quote price."""
    try:
        data = _get(f"/quotes/{ticker}")
        quote = data.get(ticker, {}).get("quote", {})
        # use last price, fall back to mark price
        return quote.get("lastPrice") or quote.get("mark")
    except Exception as e:
        print(f"[DATA] Error fetching price for {ticker}: {e}")
        return None


# ─────────────────────────────────────────────
# PREMARKET HIGH
# ─────────────────────────────────────────────

def get_premarket_high(ticker):
    """
    Compute premarket high from 4:00 AM to 9:30 AM ET
    using 1-minute candles.
    """
    try:
        now      = datetime.now(ET)
        start    = now.replace(hour=4, minute=0, second=0, microsecond=0)
        mkt_open = now.replace(hour=9, minute=30, second=0, microsecond=0)

        start_ms = int(start.timestamp() * 1000)
        end_ms   = int(mkt_open.timestamp() * 1000)

        params = {
            "periodType":    "day",
            "period":         1,
            "frequencyType": "minute",
            "frequency":      1,
            "startDate":      start_ms,
            "endDate":        end_ms,
            "needExtendedHoursData": "true"
        }
        data    = _get(f"/pricehistory?symbol={ticker}", params)
        candles = data.get("candles", [])
        if not candles:
            return None
        return max(c["high"] for c in candles)
    except Exception as e:
        print(f"[DATA] Error fetching PM high for {ticker}: {e}")
        return None


# ─────────────────────────────────────────────
# PRIOR DAY DATA
# ─────────────────────────────────────────────

def get_prior_day_data(ticker):
    """
    Get prior trading day OHLCV and approximate VWAP.
    Returns dict: {high, low, close, vwap, volume}
    """
    try:
        params = {
            "periodType":    "day",
            "period":         2,
            "frequencyType": "daily",
            "frequency":      1,
            "needExtendedHoursData": "false"
        }
        data    = _get(f"/pricehistory?symbol={ticker}", params)
        candles = data.get("candles", [])

        today_date = datetime.now(ET).date()
        prior = None
        for c in reversed(candles):
            c_date = datetime.fromtimestamp(c["datetime"] / 1000, tz=ET).date()
            if c_date < today_date:
                prior = c
                break

        if not prior:
            return {}

        vwap_approx = (prior["high"] + prior["low"] + prior["close"]) / 3
        return {
            "high":   prior["high"],
            "low":    prior["low"],
            "close":  prior["close"],
            "vwap":   round(vwap_approx, 2),
            "volume": prior["volume"]
        }
    except Exception as e:
        print(f"[DATA] Error fetching prior day data for {ticker}: {e}")
        return {}


# ─────────────────────────────────────────────
# OPEN PRICE
# ─────────────────────────────────────────────

def get_open_price(ticker):
    """Get today's opening price (9:30 AM candle)."""
    try:
        candles = get_candles(ticker, multiplier=1, limit=100)
        for c in candles:
            ts = c.get("timestamp")
            if ts and ts.hour == 9 and ts.minute == 30:
                return c["open"]
        # fallback — first regular hours candle
        for c in candles:
            ts = c.get("timestamp")
            if ts and (ts.hour > 9 or (ts.hour == 9 and ts.minute >= 30)):
                return c["open"]
        return None
    except Exception as e:
        print(f"[DATA] Error fetching open price for {ticker}: {e}")
        return None


# ─────────────────────────────────────────────
# CALCULATIONS (same as Polygon version)
# ─────────────────────────────────────────────

def calculate_vwap(candles):
    """
    Calculate intraday VWAP from candles (regular hours only).
    VWAP = sum(typical_price * volume) / sum(volume)
    """
    total_tpv = 0
    total_vol = 0
    for c in candles:
        ts = c.get("timestamp")
        if ts and (ts.hour < 9 or (ts.hour == 9 and ts.minute < 30)):
            continue
        typical    = (c["high"] + c["low"] + c["close"]) / 3
        total_tpv += typical * c["volume"]
        total_vol += c["volume"]
    if total_vol == 0:
        return None
    return total_tpv / total_vol


def calculate_ema(values, period=9):
    """Calculate EMA for a list of closing prices."""
    if len(values) < period:
        return None
    k   = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def calculate_atr(ticker, period=14):
    """Calculate ATR using daily candles."""
    try:
        params = {
            "periodType":    "day",
            "period":         1,
            "frequencyType": "daily",
            "frequency":      1,
        }
        # fetch enough daily bars
        data    = _get(f"/pricehistory?symbol={ticker}", params)
        results = data.get("candles", [])

        if len(results) < 2:
            return None
        trs = []
        for i in range(1, len(results)):
            high       = results[i]["high"]
            low        = results[i]["low"]
            prev_close = results[i-1]["close"]
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low  - prev_close)
            )
            trs.append(tr)
        return sum(trs[-period:]) / min(len(trs), period)
    except Exception as e:
        print(f"[DATA] Error calculating ATR for {ticker}: {e}")
        return None


def get_average_volume(candles, lookback=20):
    """Average volume over last N regular-hours candles."""
    rh = [
        c for c in candles
        if c.get("timestamp") and
           (c["timestamp"].hour > 9 or
           (c["timestamp"].hour == 9 and c["timestamp"].minute >= 30))
    ]
    if not rh:
        return None
    vols = [c["volume"] for c in rh[-lookback:]]
    return sum(vols) / len(vols) if vols else None


def get_session_high(candles):
    """Get the highest high from regular hours candles today."""
    rh = [
        c for c in candles
        if c.get("timestamp") and
           (c["timestamp"].hour > 9 or
           (c["timestamp"].hour == 9 and c["timestamp"].minute >= 30))
    ]
    if not rh:
        return None
    return max(c["high"] for c in rh)


# ─────────────────────────────────────────────
# OPTIONS QUOTE
# ─────────────────────────────────────────────

def get_option_quote(option_ticker):
    """
    Get latest price for an options contract.
    option_ticker: Schwab format e.g. NVDA  260328C00180000
    Returns mid price.
    """
    try:
        data  = _get(f"/quotes/{option_ticker}")
        quote = data.get(option_ticker, {}).get("quote", {})
        bid   = quote.get("bid", 0)
        ask   = quote.get("ask", 0)
        if bid and ask:
            return (bid + ask) / 2
        return quote.get("lastPrice")
    except Exception as e:
        print(f"[DATA] Error fetching option quote for {option_ticker}: {e}")
        return None
