"""
main.py - Andre's Trading Scanner v3.3
Single-file deployment.
Preserves the original Schwab + Telegram architecture, expands watchlist/arm commands,
keeps the prior core setups, adds:
- 15-minute ORB long
- ORB timing cutoffs
- Later-day HOD breakout long
- Armed-only 3-stage sweep logic on 2-minute bars:
    1) SWEEP_WATCH
    2) SWEEP_ACTIVE
    3) SWEEP_RECLAIM_LONG
"""

import os
import time
import json
import base64
import threading
from datetime import datetime
from collections import defaultdict
from urllib.parse import urlparse, parse_qs, urlencode

import pytz
import requests


# ──────────────────────────────────────────────────────────────
# ENV / GLOBALS
# ──────────────────────────────────────────────────────────────

SCHWAB_CLIENT_ID = os.environ.get("SCHWAB_CLIENT_ID", "")
SCHWAB_CLIENT_SECRET = os.environ.get("SCHWAB_CLIENT_SECRET", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

ET = pytz.timezone("America/New_York")
AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
REDIRECT = "https://127.0.0.1"
TOKEN_FILE = "schwab_tokens.json"
BASE = "https://api.schwabapi.com/marketdata/v1"

DEFAULT_WATCHLIST = [
    "NVDA", "AMD", "TSLA", "PLTR", "AMZN", "MU", "MSFT", "GOOGL",
    "AAPL", "AVGO", "META", "CVX", "DELL", "RKLB", "MRVL", "ANET",
    "CRDO", "LITE", "COHR", "COIN", "AAOI", "XOM", "ARM", "INTC"
]

MIN_SCORE = 60
COOLDOWN = 15

# Timing windows
ORB_5M_CUTOFF = (9, 45)      # no more 5m ORBs after 9:45 ET
ORB_15M_START = (9, 45)      # 15m ORB starts being valid after 9:45 ET
ORB_15M_CUTOFF = (10, 5)     # no more 15m ORBs after 10:05 ET
HOD_START = (10, 15)         # later-day HOD breakout watch starts here
HOD_END = (15, 30)

# Sweep-specific cooldowns
SWEEP_WATCH_COOLDOWN = 10
SWEEP_ACTIVE_COOLDOWN = 5
SWEEP_RECLAIM_COOLDOWN = 10

# Lightweight state persistence for runtime lists
WATCHLIST_FILE = "watchlist_state.json"
ARMED_FILE = "armed_state.json"

_pending_auth = False


# ──────────────────────────────────────────────────────────────
# GENERAL HELPERS
# ──────────────────────────────────────────────────────────────

def now_et():
    return datetime.now(ET)


def hhmm_gte(ts: datetime, h: int, m: int) -> bool:
    return (ts.hour, ts.minute) >= (h, m)


def hhmm_lte(ts: datetime, h: int, m: int) -> bool:
    return (ts.hour, ts.minute) <= (h, m)


def in_window(ts: datetime, start_h: int, start_m: int, end_h: int, end_m: int) -> bool:
    return hhmm_gte(ts, start_h, start_m) and hhmm_lte(ts, end_h, end_m)


def clamp_score(v: int) -> int:
    return max(0, min(int(v), 100))


def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[ALERT]\n{msg}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        ).raise_for_status()
        print(f"[SENT] {now_et().strftime('%H:%M')}")
    except Exception as e:
        print(f"[TELEGRAM ERR] {e}")


def load_json_file(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file(path: str, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[STATE SAVE ERR] {path}: {e}")


# ──────────────────────────────────────────────────────────────
# SCHWAB AUTH
# ──────────────────────────────────────────────────────────────

def _b64() -> str:
    return base64.b64encode(f"{SCHWAB_CLIENT_ID}:{SCHWAB_CLIENT_SECRET}".encode()).decode()


def _save_tokens(t: dict):
    t["saved_at"] = time.time()
    with open(TOKEN_FILE, "w") as f:
        json.dump(t, f, indent=2)


def _load_tokens() -> dict:
    if not os.path.exists(TOKEN_FILE):
        return {}
    try:
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _expired(t: dict) -> bool:
    if not t:
        return True
    return time.time() > t.get("saved_at", 0) + t.get("expires_in", 1800) - 300


def _refresh_tokens(t: dict) -> dict:
    headers = {
        "Authorization": f"Basic {_b64()}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    r = requests.post(
        TOKEN_URL,
        headers=headers,
        data={"grant_type": "refresh_token", "refresh_token": t.get("refresh_token", "")},
        timeout=15,
    )
    r.raise_for_status()
    new_t = r.json()
    if "refresh_token" not in new_t:
        new_t["refresh_token"] = t.get("refresh_token")
    _save_tokens(new_t)
    return new_t


def _login():
    global _pending_auth
    _pending_auth = True
    url = f"{AUTH_URL}?{urlencode({'response_type': 'code', 'client_id': SCHWAB_CLIENT_ID, 'redirect_uri': REDIRECT, 'scope': 'readonly'})}"
    print(f"[AUTH] Login required. URL: {url}")
    send_telegram(
        "🔐 <b>Schwab Authorization Required</b>\n"
        + "━" * 30
        + f"\n<b>Step 1:</b> Open this link:\n<code>{url}</code>\n\n"
        + "<b>Step 2:</b> Log in and approve access\n\n"
        + "<b>Step 3:</b> Copy the full redirect URL from the browser bar\n\n"
        + "<b>Step 4:</b> Send it like this:\n"
        + "<code>/auth https://127.0.0.1/?code=FULL_URL_HERE</code>"
    )
    start = time.time()
    while _pending_auth:
        if time.time() - start > 600:
            send_telegram("⏰ Auth timed out. Retry with /reauth")
            return None
        time.sleep(2)
    return _load_tokens()


def _complete_auth(full_redirect_url: str) -> bool:
    global _pending_auth
    try:
        parsed = urlparse(full_redirect_url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if not code:
            send_telegram("❌ Could not find auth code in that URL.")
            return False

        headers = {
            "Authorization": f"Basic {_b64()}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        r = requests.post(
            TOKEN_URL,
            headers=headers,
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT},
            timeout=15,
        )
        r.raise_for_status()
        t = r.json()
        _save_tokens(t)
        _pending_auth = False
        send_telegram("✅ <b>Schwab connected successfully!</b>")
        print("[AUTH] Tokens saved successfully.")
        return True
    except Exception as e:
        print(f"[AUTH ERROR] {e}")
        send_telegram(f"❌ Auth failed: {e}")
        return False


def tok() -> str:
    t = _load_tokens()
    if not t:
        t = _login()
        if not t:
            return ""
    elif _expired(t):
        t = _refresh_tokens(t)
    return t.get("access_token", "")


def _hdr() -> dict:
    return {"Authorization": f"Bearer {tok()}", "Accept": "application/json"}


def _get(ep: str, params=None):
    for i in range(2):
        try:
            r = requests.get(f"{BASE}{ep}", headers=_hdr(), params=params or {}, timeout=10)
            if r.status_code == 401 and i == 0:
                _refresh_tokens(_load_tokens())
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[DATA ERR] {ep}: {e}")
            if i == 0:
                time.sleep(2)
    return {}


# ──────────────────────────────────────────────────────────────
# MARKET DATA HELPERS
# ──────────────────────────────────────────────────────────────

def candles(ticker: str, m: int = 5):
    """
    Fetch intraday candles from Schwab.
    Uses period=1 day without explicit startDate/endDate to avoid
    timezone-related 400 errors that occur with 2-minute bars.
    Falls back gracefully if the first attempt fails.
    """
    # Primary: let Schwab determine the date range using period=1
    # This avoids the timestamp conversion bug that causes 400 errors
    # on 2-minute bars specifically
    params_primary = {
        "periodType": "day",
        "period": 1,
        "frequencyType": "minute",
        "frequency": m,
        "needExtendedHoursData": "true",
    }

    data = _get(f"/pricehistory?symbol={ticker}", params_primary)

    out = []
    for c in data.get("candles", []):
        out.append(
            {
                "o": c["open"],
                "h": c["high"],
                "l": c["low"],
                "c": c["close"],
                "v": c["volume"],
                "ts": datetime.fromtimestamp(c["datetime"] / 1000, tz=ET),
            }
        )
    return out


def price(ticker: str):
    try:
        # Schwab quotes API requires symbol as query param, not URL path
        # Correct: /quotes?symbols=NVDA
        # Wrong:   /quotes/NVDA  (returns 404)
        d = _get("/quotes", {"symbols": ticker})
        q = d.get(ticker, {}).get("quote", {})
        return q.get("lastPrice") or q.get("mark") or q.get("closePrice")
    except Exception:
        return None


def rh(cs):
    return [
        c
        for c in cs
        if c.get("ts") and (c["ts"].hour > 9 or (c["ts"].hour == 9 and c["ts"].minute >= 30))
    ]


def av(cs, n: int = 20):
    r = rh(cs)
    vols = [c["v"] for c in r[-n:]]
    return sum(vols) / len(vols) if vols else None


def vwap(cs):
    total_val = 0
    total_vol = 0
    for c in cs:
        ts = c.get("ts")
        if ts and (ts.hour < 9 or (ts.hour == 9 and ts.minute < 30)):
            continue
        tp = (c["h"] + c["l"] + c["c"]) / 3
        total_val += tp * c["v"]
        total_vol += c["v"]
    return total_val / total_vol if total_vol else None


def ema(vals, p: int = 9):
    if len(vals) < p:
        return None
    k = 2 / (p + 1)
    e = sum(vals[:p]) / p
    for v in vals[p:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(vals, p: int = 9):
    if len(vals) < p:
        return [None] * len(vals)
    k = 2 / (p + 1)
    out = [None] * (p - 1)
    e = sum(vals[:p]) / p
    out.append(e)
    for v in vals[p:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def opening_range(c1m, minutes: int = 5):
    bars = [
        c for c in c1m
        if c.get("ts") and c["ts"].hour == 9 and 30 <= c["ts"].minute < (30 + minutes)
    ]
    if not bars:
        return None, None
    return max(c["h"] for c in bars), min(c["l"] for c in bars)


def intraday_high(cs):
    r = rh(cs)
    return max(c["h"] for c in r) if r else None


def _pm_candles(ticker: str):
    """
    Fetch 1-minute premarket candles (4:00 AM to 9:30 AM ET).
    Uses ET.localize() to build correct timezone-aware timestamps,
    avoiding the .replace() bug that produces midnight UTC timestamps.
    """
    try:
        today = now_et().date()

        # Build naive datetimes first, then localize to ET properly
        start_naive = datetime(today.year, today.month, today.day, 4,  0, 0)
        end_naive   = datetime(today.year, today.month, today.day, 9, 30, 0)

        # ET.localize() correctly handles DST — .replace() does not
        start_et = ET.localize(start_naive)
        end_et   = ET.localize(end_naive)

        s = int(start_et.timestamp() * 1000)
        e = int(end_et.timestamp() * 1000)

        if e <= s:
            return []

        d = _get(
            f"/pricehistory?symbol={ticker}",
            {
                "periodType":    "day",
                "period":        1,
                "frequencyType": "minute",
                "frequency":     1,
                "startDate":     s,
                "endDate":       e,
                "needExtendedHoursData": "true",
            },
        )
        return d.get("candles", [])
    except Exception as e:
        print(f"[PM CANDLES ERR] {ticker}: {e}")
        return []


def pm_high(ticker: str):
    cs = _pm_candles(ticker)
    return max(x["high"] for x in cs) if cs else None


def pm_low(ticker: str):
    cs = _pm_candles(ticker)
    return min(x["low"] for x in cs) if cs else None


def prior_day(ticker: str):
    try:
        d = _get(
            f"/pricehistory?symbol={ticker}",
            {
                "periodType": "day",
                "period": 2,
                "frequencyType": "daily",
                "frequency": 1,
                "needExtendedHoursData": "false",
            },
        )
        today = now_et().date()
        for c in reversed(d.get("candles", [])):
            c_date = datetime.fromtimestamp(c["datetime"] / 1000, tz=ET).date()
            if c_date < today:
                return {
                    "h": c["high"],
                    "l": c["low"],
                    "c": c["close"],
                    "vwap": round((c["high"] + c["low"] + c["close"]) / 3, 2),
                }
    except Exception:
        pass
    return {}


# ──────────────────────────────────────────────────────────────
# SWEEP / CANDLE-QUALITY HELPERS
# ──────────────────────────────────────────────────────────────

def close_pos_in_range(candle) -> float:
    rng = candle["h"] - candle["l"]
    if rng <= 0:
        return 0.5
    return (candle["c"] - candle["l"]) / rng


def is_green(candle) -> bool:
    return candle["c"] > candle["o"]


def top_half_close(candle) -> bool:
    return close_pos_in_range(candle) >= 0.50


def micro_base_stats(cs, lookback: int = 5, exclude_last: int = 1):
    """
    Builds a micro-base from the most recent bars while excluding the current bar by default.
    Used for armed 2-minute sweep logic.
    """
    r = rh(cs)
    need = lookback + exclude_last
    if len(r) < need:
        return None

    if exclude_last > 0:
        base = r[-need:-exclude_last]
    else:
        base = r[-lookback:]

    if len(base) < 3:
        return None

    base_high = max(c["h"] for c in base)
    base_low = min(c["l"] for c in base)
    avg_close = sum(c["c"] for c in base) / len(base)
    width = base_high - base_low
    tight_pct = (width / avg_close) if avg_close else 999

    return {
        "bars": base,
        "high": base_high,
        "low": base_low,
        "width": width,
        "tight_pct": tight_pct,
    }


# ──────────────────────────────────────────────────────────────
# EXISTING CORE SETUPS (PRESERVED / CLEANED)
# ──────────────────────────────────────────────────────────────

def orb_5m_long(c5, c1, p, vw, pmh_v):
    ts = now_et()
    if not hhmm_lte(ts, *ORB_5M_CUTOFF):
        return False, {}

    oh, ol = opening_range(c1, 5)
    if oh is None or ol is None:
        return False, {}

    r = rh(c5)
    if len(r) < 2:
        return False, {}

    last = r[-1]
    prev = r[-2]
    avgv = av(c5)

    broke = p > oh
    above_vwap = p > (vw or 0)
    vol = last["v"] > (avgv or 0) * 1.3
    was_below = prev["c"] <= oh

    if not (broke and above_vwap and was_below):
        return False, {}

    score = 60 + (15 if vol else 0) + (10 if pmh_v and p > pmh_v else 0)

    return True, {
        "setup": "ORB_5M_LONG",
        "dir": "🟢 LONG",
        "trigger": f"Break above 5m OR high ${round(oh, 2)}",
        "inval": f"Loss of OR low ${round(ol, 2)}",
        "level": f"5m OR: ${round(ol, 2)}–${round(oh, 2)}",
        "vol": "Expanding ✅" if vol else "Weak ⚠️",
        "score": clamp_score(score),
        "action": "Actionable" if vol else "Watch",
        "notes": "Early 5m ORB only",
    }


def orb_15m_long(c5, c1, p, vw, pmh_v):
    ts = now_et()
    if not hhmm_gte(ts, *ORB_15M_START):
        return False, {}
    if not hhmm_lte(ts, *ORB_15M_CUTOFF):
        return False, {}

    oh, ol = opening_range(c1, 15)
    if oh is None or ol is None:
        return False, {}

    r = rh(c5)
    if len(r) < 2:
        return False, {}

    last = r[-1]
    prev = r[-2]
    avgv = av(c5)

    broke = p > oh
    above_vwap = p > (vw or 0)
    vol = last["v"] > (avgv or 0) * 1.2
    was_below = prev["c"] <= oh

    if not (broke and above_vwap and was_below):
        return False, {}

    score = 65 + (10 if vol else 0) + (10 if pmh_v and p > pmh_v else 0)

    return True, {
        "setup": "ORB_15M_LONG",
        "dir": "🟢 LONG",
        "trigger": f"Break above 15m OR high ${round(oh, 2)}",
        "inval": f"Loss of OR low ${round(ol, 2)}",
        "level": f"15m OR: ${round(ol, 2)}–${round(oh, 2)}",
        "vol": "Expanding ✅" if vol else "Average",
        "score": clamp_score(score),
        "action": "Actionable",
        "notes": "15m ORB in the post-9:45 window",
    }


def pmh_retest(c5, p, vw, pmh_v):
    if not pmh_v:
        return False, {}
    r = rh(c5)
    if len(r) < 3:
        return False, {}

    broke = any(c["h"] > pmh_v for c in r[:-2])
    if not broke:
        return False, {}

    near = abs(p - pmh_v) / pmh_v <= 0.004
    above = p >= pmh_v * 0.998
    above_vwap = p > (vw or 0)
    avgv = av(c5)
    light_pullback = all(c["v"] < (avgv or float("inf")) * 0.9 for c in r[-2:])

    if not (above and above_vwap):
        return False, {}

    score = 65 + (10 if near else 0) + (10 if light_pullback else 0) + (5 if above_vwap else 0)

    return True, {
        "setup": "PMH_BREAK_RETEST_LONG",
        "dir": "🟢 LONG",
        "trigger": f"Hold above PM High ${round(pmh_v, 2)} + push",
        "inval": f"Loss of ${round(pmh_v * 0.997, 2)}",
        "level": f"PM High: ${round(pmh_v, 2)}",
        "vol": "Pullback light ✅" if light_pullback else "Watch volume",
        "score": clamp_score(score),
        "action": "Actionable" if near and above_vwap else "Watch",
        "notes": "PM high broken earlier — now retesting",
    }


def pml_retest(c5, p, vw, pml_v):
    if not pml_v:
        return False, {}
    r = rh(c5)
    if len(r) < 3:
        return False, {}

    broke = any(c["l"] < pml_v for c in r[:-2])
    if not broke:
        return False, {}

    near = abs(p - pml_v) / pml_v <= 0.004
    below = p <= pml_v * 1.002
    below_vwap = p < (vw or float("inf"))
    avgv = av(c5)
    light_bounce = all(c["v"] < (avgv or float("inf")) * 0.9 for c in r[-2:])

    if not (below and below_vwap):
        return False, {}

    score = 65 + (10 if near else 0) + (10 if light_bounce else 0)

    return True, {
        "setup": "PML_BREAK_RETEST_SHORT",
        "dir": "🔴 SHORT",
        "trigger": f"Reject under PM Low ${round(pml_v, 2)}",
        "inval": f"Reclaim ${round(pml_v * 1.003, 2)}",
        "level": f"PM Low: ${round(pml_v, 2)}",
        "vol": "Bounce light ✅" if light_bounce else "Watch",
        "score": clamp_score(score),
        "action": "Actionable" if near and below_vwap else "Watch",
        "notes": "PM low broke earlier — underside retest failing",
    }


def vwap_reclaim(c5, p, vw):
    if not vw:
        return False, {}
    r = rh(c5)
    if len(r) < 4:
        return False, {}

    last = r[-1]
    prev = r[-2]
    avgv = av(c5)

    was_below = any(c["c"] < vw for c in r[-5:-1])
    if not was_below:
        return False, {}

    strong = last["c"] > vw and last["c"] > last["o"]
    first = strong and prev["c"] < vw
    if not strong:
        return False, {}

    vol = last["v"] > (avgv or 0) * 1.2
    prior_slice = r[-8:-2]
    prior_fails = sum(
        1 for i in range(1, len(prior_slice))
        if prior_slice[i]["c"] > vw and prior_slice[i - 1]["c"] < vw
    )

    score = 60 + (15 if first else 0) + (15 if vol else 0) - (20 if prior_fails > 1 else 0)
    if score < MIN_SCORE:
        return False, {}

    return True, {
        "setup": "VWAP_RECLAIM_LONG",
        "dir": "🟢 LONG",
        "trigger": f"Hold above VWAP ${round(vw, 2)} + push",
        "inval": f"Fail back below VWAP ${round(vw, 2)}",
        "level": f"VWAP: ${round(vw, 2)}",
        "vol": "Expanding ✅" if vol else "Light",
        "score": clamp_score(score),
        "action": "Actionable" if first and vol else "Watch",
        "notes": "First clean reclaim preferred",
    }


def vwap_reject(c5, p, vw):
    if not vw:
        return False, {}
    r = rh(c5)
    if len(r) < 4:
        return False, {}

    last = r[-1]
    prev = r[-2]
    near = abs(prev["h"] - vw) / vw <= 0.005
    below = last["c"] < vw
    bear = last["c"] < last["o"]
    was_below = any(c["c"] < vw for c in r[-6:-3])
    if not (near and below and bear and was_below):
        return False, {}

    avgv = av(c5)
    vol = last["v"] > (avgv or 0) * 1.1
    score = 65 + (10 if vol else 0) + (10 if bear else 0)

    return True, {
        "setup": "VWAP_REJECT_SHORT",
        "dir": "🔴 SHORT",
        "trigger": f"Break local pivot below VWAP ${round(vw, 2)}",
        "inval": f"Acceptance above VWAP ${round(vw * 1.003, 2)}",
        "level": f"VWAP resistance: ${round(vw, 2)}",
        "vol": "Expanding ✅" if vol else "Light",
        "score": clamp_score(score),
        "action": "Actionable",
        "notes": "Rejected at VWAP — rolling over",
    }


def ema9_pb_long(c5, p, vw):
    r = rh(c5)
    if len(r) < 12:
        return False, {}

    cls = [c["c"] for c in r]
    es = ema_series(cls, 9)
    en = es[-1]
    if en is None:
        return False, {}

    ema_vals = [e for e in es[-5:] if e is not None]
    rising = len(ema_vals) > 1 and ema_vals[-1] > ema_vals[0]

    last = r[-1]
    prev = r[-2]
    above_vwap = p > (vw or 0)
    touched = last["l"] <= en * 1.003 or prev["l"] <= en * 1.003
    bouncing = last["c"] > prev["h"] or last["c"] > en
    avgv = av(c5)
    light_pullback = last["v"] < (avgv or float("inf")) * 0.85

    if not (rising and above_vwap and touched and bouncing):
        return False, {}

    score = 65 + (10 if light_pullback else 0) + (10 if above_vwap else 0) + (5 if rising else 0)

    return True, {
        "setup": "EMA9_5M_PULLBACK_LONG",
        "dir": "🟢 LONG",
        "trigger": "Bounce after 9 EMA touch",
        "inval": f"Loss of 9 EMA ${round(en, 2)}",
        "level": f"9 EMA: ${round(en, 2)} | VWAP: ${round(vw, 2) if vw else 'N/A'}",
        "vol": "Pullback light ✅" if light_pullback else "Watch volume",
        "score": clamp_score(score),
        "action": "Actionable",
        "notes": "Rising EMA + controlled pullback",
    }


def ema9_pb_short(c5, p, vw):
    r = rh(c5)
    if len(r) < 12:
        return False, {}

    cls = [c["c"] for c in r]
    es = ema_series(cls, 9)
    en = es[-1]
    if en is None:
        return False, {}

    ema_vals = [e for e in es[-5:] if e is not None]
    falling = len(ema_vals) > 1 and ema_vals[-1] < ema_vals[0]

    last = r[-1]
    prev = r[-2]
    below_vwap = p < (vw or float("inf"))
    touched = last["h"] >= en * 0.997 or prev["h"] >= en * 0.997
    rejecting = last["c"] < last["o"] and last["c"] < en
    avgv = av(c5)
    light_bounce = last["v"] < (avgv or float("inf")) * 0.85

    if not (falling and below_vwap and touched and rejecting):
        return False, {}

    score = 65 + (10 if light_bounce else 0) + (10 if below_vwap else 0) + (5 if falling else 0)

    return True, {
        "setup": "EMA9_5M_PULLBACK_SHORT",
        "dir": "🔴 SHORT",
        "trigger": f"Break below ${round(prev['l'], 2)} after EMA rejection",
        "inval": f"Reclaim through 9 EMA ${round(en, 2)}",
        "level": f"9 EMA resistance: ${round(en, 2)}",
        "vol": "Bounce light ✅" if light_bounce else "Watch",
        "score": clamp_score(score),
        "action": "Actionable",
        "notes": "Falling EMA, weak bounce, rejecting",
    }


def flag_long(c5, p, vw):
    r = rh(c5)
    if len(r) < 8:
        return False, {}

    avgv = av(c5)
    impulse = None
    for c in r[-10:-3]:
        if (c["c"] - c["o"]) > 0 and c["v"] > (avgv or 0) * 1.5:
            impulse = c
            break
    if not impulse:
        return False, {}

    cons = r[-5:]
    flag_high = max(c["h"] for c in cons)
    flag_low = min(c["l"] for c in cons)
    flag_range = flag_high - flag_low
    impulse_size = impulse["c"] - impulse["o"]
    tight = impulse_size > 0 and flag_range < impulse_size * 0.5
    above_vwap = p > (vw or 0)

    last = r[-1]
    broke = last["c"] > flag_high and last["c"] > r[-2]["h"]
    vol = last["v"] > (avgv or 0) * 1.2
    dried_up = all(c["v"] < (avgv or float("inf")) * 0.8 for c in cons[:-1])

    if not (tight and broke and above_vwap):
        return False, {}

    score = 65 + (10 if dried_up else 0) + (15 if vol else 0) + (5 if tight else 0)

    return True, {
        "setup": "FLAG_BREAKOUT_LONG",
        "dir": "🟢 LONG",
        "trigger": f"Break above flag high ${round(flag_high, 2)}",
        "inval": f"Loss of flag low ${round(flag_low, 2)}",
        "level": f"Flag: ${round(flag_low, 2)}–${round(flag_high, 2)}",
        "vol": "Dry-up + expansion ✅" if (dried_up and vol) else "Watch volume",
        "score": clamp_score(score),
        "action": "Actionable" if (vol and tight) else "Watch",
        "notes": f"Flag range ${round(flag_range, 2)} vs impulse ${round(impulse_size, 2)}",
    }


def pdh_retest(c5, p, vw, pdh):
    if not pdh:
        return False, {}
    r = rh(c5)
    if len(r) < 3:
        return False, {}

    if not any(c["h"] > pdh for c in r[:-2]):
        return False, {}

    near = abs(p - pdh) / pdh <= 0.005
    above = p >= pdh * 0.998
    above_vwap = p > (vw or 0)
    avgv = av(c5)
    light_pullback = all(c["v"] < (avgv or float("inf")) * 0.9 for c in r[-2:])

    if not (above and above_vwap):
        return False, {}

    score = 70 + (10 if near else 0) + (10 if light_pullback else 0) + (5 if above_vwap else 0)

    return True, {
        "setup": "PDH_BREAK_RETEST_LONG",
        "dir": "🟢 LONG",
        "trigger": f"Reclaim above PDH ${round(pdh, 2)} + push",
        "inval": f"Loss of ${round(pdh * 0.997, 2)}",
        "level": f"Prior Day High: ${round(pdh, 2)}",
        "vol": "Pullback light ✅" if light_pullback else "Watch",
        "score": clamp_score(score),
        "action": "Actionable" if near and above_vwap else "Watch",
        "notes": "Daily breakout — institutional level",
    }


def pdl_retest(c5, p, vw, pdl):
    if not pdl:
        return False, {}
    r = rh(c5)
    if len(r) < 3:
        return False, {}

    if not any(c["l"] < pdl for c in r[:-2]):
        return False, {}

    near = abs(p - pdl) / pdl <= 0.005
    below = p <= pdl * 1.002
    below_vwap = p < (vw or float("inf"))
    avgv = av(c5)
    light_bounce = all(c["v"] < (avgv or float("inf")) * 0.9 for c in r[-2:])

    if not (below and below_vwap):
        return False, {}

    score = 70 + (10 if near else 0) + (10 if light_bounce else 0)

    return True, {
        "setup": "PDL_BREAK_RETEST_SHORT",
        "dir": "🔴 SHORT",
        "trigger": f"Reject under PDL ${round(pdl, 2)} + break low",
        "inval": f"Reclaim ${round(pdl * 1.003, 2)}",
        "level": f"Prior Day Low: ${round(pdl, 2)}",
        "vol": "Bounce light ✅" if light_bounce else "Watch",
        "score": clamp_score(score),
        "action": "Actionable" if near and below_vwap else "Watch",
        "notes": "Prior day low broke — institutional breakdown",
    }


def later_day_hod_breakout(c5, p, vw):
    ts = now_et()
    if not in_window(ts, HOD_START[0], HOD_START[1], HOD_END[0], HOD_END[1]):
        return False, {}

    r = rh(c5)
    if len(r) < 10:
        return False, {}

    last = r[-1]
    prev = r[-2]
    prior_hod = max(c["h"] for c in r[:-1]) if len(r) > 1 else None
    if prior_hod is None:
        return False, {}

    above_vwap = p > (vw or 0)
    broke = p > prior_hod and prev["c"] <= prior_hod
    avgv = av(c5)
    vol = last["v"] > (avgv or 0) * 1.2
    base_below_hod = len(r) >= 5 and all(c["h"] <= prior_hod * 1.002 for c in r[-5:-1])

    if not (broke and above_vwap):
        return False, {}

    score = 68 + (10 if vol else 0) + (10 if base_below_hod else 0) + (5 if above_vwap else 0)

    return True, {
        "setup": "LATER_DAY_HOD_BREAKOUT",
        "dir": "🟢 LONG",
        "trigger": f"Break above HOD ${round(prior_hod, 2)}",
        "inval": f"Fail back under ${round(prior_hod, 2)}",
        "level": f"Prior HOD: ${round(prior_hod, 2)}",
        "vol": "Expanding ✅" if vol else "Average",
        "score": clamp_score(score),
        "action": "Actionable",
        "notes": "Later-day continuation / daily expansion candidate",
    }


# ──────────────────────────────────────────────────────────────
# NEW SWEEP LOGIC (ARMED NAMES ONLY)
# ──────────────────────────────────────────────────────────────

def sweep_watch_long_v2(c2, p, vw):
    """
    Stage 1: Heads-up only.
    Trend intact, micro-base formed, price pressing the lows.
    """
    r = rh(c2)
    if len(r) < 8:
        return False, {}

    cls = [c["c"] for c in r]
    es = ema_series(cls, 9)
    en = es[-1]
    if en is None:
        return False, {}

    base = micro_base_stats(c2, lookback=5, exclude_last=1)
    if not base:
        return False, {}

    last = r[-1]
    above_vwap = p > (vw or 0)
    ema_slice = [x for x in es[-4:] if x is not None]
    ema_up = len(ema_slice) >= 2 and ema_slice[-1] >= ema_slice[0]
    tight = base["tight_pct"] <= 0.0045
    pressing_lows = last["l"] <= base["low"] * 1.002
    not_lost = last["c"] >= base["low"] * 0.999

    if not (above_vwap and ema_up and tight and pressing_lows and not_lost):
        return False, {}

    score = 55 + (10 if tight else 0) + (10 if above_vwap else 0) + (5 if ema_up else 0)

    return True, {
        "setup": "SWEEP_WATCH",
        "dir": "👀 WATCH",
        "trigger": f"Pressing micro-base low ${round(base['low'], 2)}",
        "inval": f"Clean loss of ${round(base['low'], 2)} without reclaim",
        "level": f"Base: ${round(base['low'], 2)}–${round(base['high'], 2)}",
        "vol": "Context only",
        "score": clamp_score(score),
        "action": "Watch now",
        "notes": "Trend intact, base formed, price pressing lows",
    }


def sweep_active_long_v2(c2, p, vw):
    """
    Stage 2: The undercut is happening now.
    No reclaim required yet.
    """
    r = rh(c2)
    if len(r) < 8:
        return False, {}

    cls = [c["c"] for c in r]
    es = ema_series(cls, 9)
    en = es[-1]
    if en is None:
        return False, {}

    base = micro_base_stats(c2, lookback=5, exclude_last=1)
    if not base:
        return False, {}

    last = r[-1]
    ema_slice = [x for x in es[-4:] if x is not None]
    above_vwap = p > (vw or 0)
    ema_up = len(ema_slice) >= 2 and ema_slice[-1] >= ema_slice[0]
    undercut_now = last["l"] < base["low"]

    if not (above_vwap and ema_up and undercut_now):
        return False, {}

    score = 60 + (10 if above_vwap else 0) + (5 if ema_up else 0)

    return True, {
        "setup": "SWEEP_ACTIVE",
        "dir": "⚠️ ACTIVE",
        "trigger": f"Undercut in progress below ${round(base['low'], 2)}",
        "inval": f"No reclaim / continued acceptance below ${round(base['low'], 2)}",
        "level": f"Base low: ${round(base['low'], 2)} | Current low: ${round(last['l'], 2)}",
        "vol": "Live decision zone",
        "score": clamp_score(score),
        "action": "Decision zone",
        "notes": "Undercut is happening now — watch for reclaim",
    }


def sweep_reclaim_long_v2(c2, p, vw):
    """
    Stage 3: Confirmed reclaim.
    Must close back above base low and in top half of range.
    Extra points for green close and same-candle reclaim.
    """
    r = rh(c2)
    if len(r) < 9:
        return False, {}

    cls = [c["c"] for c in r]
    es = ema_series(cls, 9)
    en = es[-1]
    if en is None:
        return False, {}

    ema_slice = [x for x in es[-4:] if x is not None]
    above_vwap = p > (vw or 0)
    ema_up = len(ema_slice) >= 2 and ema_slice[-1] >= ema_slice[0]
    if not (above_vwap and ema_up):
        return False, {}

    base_bars = r[-7:-2]
    if len(base_bars) < 4:
        return False, {}

    base_high = max(c["h"] for c in base_bars)
    base_low = min(c["l"] for c in base_bars)

    sweep_bar = r[-2]
    reclaim_bar = r[-1]

    # same-candle reclaim
    same_candle_undercut = sweep_bar["l"] < base_low
    same_candle_reclaim = sweep_bar["c"] > base_low and top_half_close(sweep_bar)

    # next-candle reclaim
    next_candle_undercut = sweep_bar["l"] < base_low
    next_candle_reclaim = reclaim_bar["c"] > base_low and top_half_close(reclaim_bar)

    valid = (same_candle_undercut and same_candle_reclaim) or (next_candle_undercut and next_candle_reclaim)
    if not valid:
        return False, {}

    used_bar = sweep_bar if (same_candle_undercut and same_candle_reclaim) else reclaim_bar
    same_bar = used_bar is sweep_bar

    score = 40
    score += 15  # close back above base low
    score += 15  # close in top half
    score += 10 if is_green(used_bar) else 0
    score += 10 if above_vwap else 0
    score += 10 if ema_up else 0
    score += 10 if same_bar else 0

    return True, {
        "setup": "SWEEP_RECLAIM_LONG",
        "dir": "🟢 LONG",
        "trigger": f"Reclaim above micro-base low ${round(base_low, 2)}",
        "inval": f"Loss of sweep low ${round(min(sweep_bar['l'], reclaim_bar['l']), 2)}",
        "level": f"Base: ${round(base_low, 2)}–${round(base_high, 2)} | Next pivot: ${round(base_high, 2)}",
        "vol": "Reclaim confirmed",
        "score": clamp_score(score),
        "action": "Actionable" if score >= 70 else "Watch closely",
        "notes": (
            f"{'Same-candle reclaim' if same_bar else 'Next-candle reclaim'} | "
            f"{'Green close' if is_green(used_bar) else 'Not green'} | "
            f"Close position: {round(close_pos_in_range(used_bar) * 100, 1)}% of range"
        ),
    }


# ──────────────────────────────────────────────────────────────
# FORMATTERS
# ──────────────────────────────────────────────────────────────

def fmt(ticker: str, d: dict) -> str:
    sc = d.get("score", 0)
    em = "🔥" if sc >= 85 else "✅" if sc >= 70 else "⚠️"
    return "\n".join(
        [
            f"{em} <b>{ticker} — {d.get('setup')}</b>  {d.get('dir', '')}",
            f"Confidence: <b>{sc}/100</b>  |  {d.get('action', 'Watch')}",
            "━" * 30,
            f"📍 <b>Trigger:</b> {d.get('trigger', '')}",
            f"🛑 <b>Stop:</b> {d.get('inval', '')}",
            f"🔑 <b>Level:</b> {d.get('level', '')}",
            f"📊 <b>Volume:</b> {d.get('vol', '')}",
            f"📝 {d.get('notes', '')}",
            "━" * 30,
            f"⏰ {now_et().strftime('%I:%M %p ET')}",
            f"👉 {d.get('action', 'Review before entry')}",
        ]
    )


# ──────────────────────────────────────────────────────────────
# SCANNER
# ──────────────────────────────────────────────────────────────

class Scanner:
    def __init__(self):
        saved_watch = load_json_file(WATCHLIST_FILE, DEFAULT_WATCHLIST)
        saved_armed = load_json_file(ARMED_FILE, [])

        self.wl = list(dict.fromkeys(saved_watch if isinstance(saved_watch, list) and saved_watch else DEFAULT_WATCHLIST))
        self.armed = set(saved_armed if isinstance(saved_armed, list) else [])

        self.pmh = {}
        self.pml = {}
        self.pr = {}

        self.pm_dt = None
        self.pr_dt = None

        self.last = defaultdict(lambda: None)
        self.earnings = set()

    def save_state(self):
        save_json_file(WATCHLIST_FILE, self.wl)
        save_json_file(ARMED_FILE, sorted(self.armed))

    def is_mkt(self) -> bool:
        n = now_et()
        if n.weekday() >= 5:
            return False
        return n.replace(hour=9, minute=25, second=0, microsecond=0) <= n <= n.replace(hour=16, minute=5, second=0, microsecond=0)

    def refresh(self):
        today = now_et().date()

        if self.pm_dt != today:
            print("[SCAN] Refreshing PM levels...")
            for t in self.wl:
                self.pmh[t] = pm_high(t)
                self.pml[t] = pm_low(t)
                time.sleep(0.35)
            self.pm_dt = today

        if self.pr_dt != today:
            print("[SCAN] Refreshing prior-day levels...")
            for t in self.wl:
                self.pr[t] = prior_day(t)
                time.sleep(0.35)
            self.pr_dt = today

    def can_alert(self, ticker: str, setup: str, cooldown: int = COOLDOWN) -> bool:
        key = f"{ticker}:{setup}"
        last = self.last[key]
        return last is None or (now_et() - last).total_seconds() / 60 >= cooldown

    def mark_alert(self, ticker: str, setup: str):
        self.last[f"{ticker}:{setup}"] = now_et()

    def scan_standard(self, ticker: str):
        alerts = []
        try:
            c5 = candles(ticker, 5)
            c1 = candles(ticker, 1)
            if not c5 or not c1:
                return alerts

            p = price(ticker)
            if not p:
                return alerts

            vw = vwap(c5)
            pmh_v = self.pmh.get(ticker)
            pml_v = self.pml.get(ticker)
            pd = self.pr.get(ticker, {})
            pdh = pd.get("h")
            pdl = pd.get("l")

            setups = [
                ("ORB_5M_LONG", lambda: orb_5m_long(c5, c1, p, vw, pmh_v), COOLDOWN),
                ("ORB_15M_LONG", lambda: orb_15m_long(c5, c1, p, vw, pmh_v), COOLDOWN),
                ("PMH_BREAK_RETEST_LONG", lambda: pmh_retest(c5, p, vw, pmh_v), COOLDOWN),
                ("PML_BREAK_RETEST_SHORT", lambda: pml_retest(c5, p, vw, pml_v), COOLDOWN),
                ("VWAP_RECLAIM_LONG", lambda: vwap_reclaim(c5, p, vw), COOLDOWN),
                ("VWAP_REJECT_SHORT", lambda: vwap_reject(c5, p, vw), COOLDOWN),
                ("EMA9_5M_PULLBACK_LONG", lambda: ema9_pb_long(c5, p, vw), COOLDOWN),
                ("EMA9_5M_PULLBACK_SHORT", lambda: ema9_pb_short(c5, p, vw), COOLDOWN),
                ("FLAG_BREAKOUT_LONG", lambda: flag_long(c5, p, vw), COOLDOWN),
                ("PDH_BREAK_RETEST_LONG", lambda: pdh_retest(c5, p, vw, pdh), COOLDOWN),
                ("PDL_BREAK_RETEST_SHORT", lambda: pdl_retest(c5, p, vw, pdl), COOLDOWN),
                ("LATER_DAY_HOD_BREAKOUT", lambda: later_day_hod_breakout(c5, p, vw), COOLDOWN),
            ]

            for name, fn, cooldown in setups:
                try:
                    ok, d = fn()
                    if ok and d.get("score", 0) >= MIN_SCORE and self.can_alert(ticker, name, cooldown):
                        alerts.append((name, d, cooldown))
                except Exception as e:
                    print(f"[SETUP ERR] {ticker}:{name}:{e}")

        except Exception as e:
            print(f"[SCAN ERR] {ticker}:{e}")

        return alerts

    def scan_sweep(self, ticker: str):
        alerts = []
        if ticker not in self.armed:
            return alerts

        try:
            c2 = candles(ticker, 2)
            if not c2:
                return alerts

            p = price(ticker)
            if not p:
                return alerts

            vw = vwap(c2)

            setups = [
                ("SWEEP_WATCH", lambda: sweep_watch_long_v2(c2, p, vw), SWEEP_WATCH_COOLDOWN),
                ("SWEEP_ACTIVE", lambda: sweep_active_long_v2(c2, p, vw), SWEEP_ACTIVE_COOLDOWN),
                ("SWEEP_RECLAIM_LONG", lambda: sweep_reclaim_long_v2(c2, p, vw), SWEEP_RECLAIM_COOLDOWN),
            ]

            for name, fn, cooldown in setups:
                try:
                    ok, d = fn()
                    if ok and self.can_alert(ticker, name, cooldown):
                        alerts.append((name, d, cooldown))
                except Exception as e:
                    print(f"[SWEEP ERR] {ticker}:{name}:{e}")

        except Exception as e:
            print(f"[SWEEP SCAN ERR] {ticker}:{e}")

        return alerts

    def cmd(self, command: str):
        global MIN_SCORE

        pts = command.strip().split()
        c = pts[0].lower() if pts else ""

        if c == "/watch" and len(pts) >= 2:
            added = []
            for raw in pts[1:]:
                t = raw.upper()
                if t not in self.wl:
                    self.wl.append(t)
                    added.append(t)
            self.save_state()
            send_telegram(f"✅ Added: {', '.join(added) if added else 'none'}\nWatching {len(self.wl)} stocks")

        elif c == "/remove" and len(pts) >= 2:
            removed = []
            for raw in pts[1:]:
                t = raw.upper()
                if t in self.wl:
                    self.wl = [x for x in self.wl if x != t]
                    self.armed.discard(t)
                    removed.append(t)
            self.save_state()
            send_telegram(f"🗑️ Removed: {', '.join(removed) if removed else 'none'}")

        elif c == "/arm" and len(pts) >= 2:
            armed_now = []
            for raw in pts[1:]:
                t = raw.upper()
                if t not in self.wl:
                    self.wl.append(t)
                self.armed.add(t)
                armed_now.append(t)
            self.save_state()
            send_telegram(f"🎯 Armed sweep logic for: {', '.join(armed_now)}")

        elif c == "/disarm" and len(pts) >= 2:
            removed = []
            for raw in pts[1:]:
                t = raw.upper()
                if t in self.armed:
                    self.armed.discard(t)
                    removed.append(t)
            self.save_state()
            send_telegram(f"🧹 Disarmed: {', '.join(removed) if removed else 'none'}")

        elif c == "/armed":
            send_telegram(f"🎯 Armed names ({len(self.armed)}):\n{', '.join(sorted(self.armed)) or 'none'}")

        elif c == "/list":
            send_telegram(f"📋 Watching ({len(self.wl)}):\n{', '.join(self.wl)}")

        elif c == "/status":
            send_telegram(
                f"📊 <b>Scanner v3.3</b>\n"
                f"Stocks: {len(self.wl)} | Armed: {len(self.armed)}\n"
                f"Min score: {MIN_SCORE}/100 | Cooldown: {COOLDOWN}m\n"
                f"5m ORB cutoff: 9:45 ET\n"
                f"15m ORB window: 9:45–10:05 ET\n"
                f"Later-day HOD window: 10:15–3:30 ET\n"
                f"Earnings tags: {', '.join(sorted(self.earnings)) or 'none'}"
            )

        elif c == "/setups":
            send_telegram(
                "📊 <b>Active Setups</b>\n"
                "1. ORB 5m Long (early only)\n"
                "2. ORB 15m Long\n"
                "3. PM High Retest Long\n"
                "4. PM Low Retest Short\n"
                "5. VWAP Reclaim Long\n"
                "6. VWAP Reject Short\n"
                "7. 9 EMA Pullback Long\n"
                "8. 9 EMA Pullback Short\n"
                "9. Flag Breakout Long\n"
                "10. PDH Retest Long\n"
                "11. PDL Retest Short\n"
                "12. Later-Day HOD Breakout\n"
                "13. Sweep Watch (armed only)\n"
                "14. Sweep Active (armed only)\n"
                "15. Sweep Reclaim Long (armed only)"
            )

        elif c == "/threshold" and len(pts) == 2:
            try:
                MIN_SCORE = int(pts[1])
                send_telegram(f"⚙️ Min score: {MIN_SCORE}/100")
            except Exception:
                send_telegram("❌ Usage: /threshold 65")

        elif c == "/earnings" and len(pts) >= 2:
            added = []
            for raw in pts[1:]:
                t = raw.upper()
                self.earnings.add(t)
                added.append(t)
            send_telegram(f"📋 Earnings flagged: {', '.join(added)}")

        elif c == "/unearnings" and len(pts) >= 2:
            removed = []
            for raw in pts[1:]:
                t = raw.upper()
                if t in self.earnings:
                    self.earnings.remove(t)
                    removed.append(t)
            send_telegram(f"🧹 Earnings unflagged: {', '.join(removed) if removed else 'none'}")

        elif c == "/auth" and len(pts) >= 2:
            _complete_auth(" ".join(pts[1:]))

        elif c == "/reauth":
            _save_tokens({})
            send_telegram("🔄 Tokens cleared. Starting fresh auth...")
            threading.Thread(target=_login, daemon=True).start()

        else:
            send_telegram(
                "Commands:\n"
                "/watch TICK1 TICK2\n"
                "/remove TICK1 TICK2\n"
                "/arm TICK1 TICK2\n"
                "/disarm TICK1 TICK2\n"
                "/armed\n"
                "/list\n"
                "/status\n"
                "/setups\n"
                "/threshold 65\n"
                "/earnings TICK1 TICK2\n"
                "/unearnings TICK1 TICK2\n"
                "/reauth"
            )

    def run(self):
        print("[SCANNER] v3.3 starting")
        send_telegram(
            f"🤖 <b>Scanner v3.3 Online</b>\n"
            f"{'━' * 28}\n"
            f"Watching <b>{len(self.wl)} stocks</b> | Armed <b>{len(self.armed)}</b>\n"
            f"Threshold ≥ {MIN_SCORE}/100\n"
            f"{'━' * 28}\n"
            f"Core: ORB 5m/15m · PMH/PML retests · VWAP reclaim/reject · EMA9 PB · Flag · PDH/PDL · Later-day HOD\n"
            f"Armed only: Sweep Watch · Sweep Active · Sweep Reclaim\n\n"
            f"Commands: /status /setups /watch /remove /arm /disarm /armed /threshold"
        )

        while True:
            if not self.is_mkt():
                print("[SCAN] Outside hours. Sleep 5m.")
                time.sleep(300)
                continue

            self.refresh()

            for t in list(self.wl):
                print(f"[SCAN] {t}...")

                try:
                    for name, d, cooldown in self.scan_standard(t):
                        send_telegram(fmt(t, d))
                        self.mark_alert(t, name)
                        time.sleep(1)
                except Exception as e:
                    print(f"[STD LOOP ERR] {t}:{e}")

                try:
                    for name, d, cooldown in self.scan_sweep(t):
                        send_telegram(fmt(t, d))
                        self.mark_alert(t, name)
                        time.sleep(1)
                except Exception as e:
                    print(f"[SWEEP LOOP ERR] {t}:{e}")

                time.sleep(0.35)

            print("[SCAN] Cycle done. Sleep 60s.")
            time.sleep(60)


# ──────────────────────────────────────────────────────────────
# TELEGRAM LISTENER
# ──────────────────────────────────────────────────────────────

def listen(sc: Scanner):
    offset = None
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"timeout": 30, "offset": offset},
                timeout=35,
            ).json()

            for u in r.get("result", []):
                offset = u["update_id"] + 1
                txt = u.get("message", {}).get("text", "")
                if txt.startswith("/"):
                    print(f"[CMD] {txt}")
                    sc.cmd(txt)

        except Exception as e:
            print(f"[CMD ERR] {e}")
            time.sleep(5)


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(
        f"[MAIN] v3.3 | Schwab:{'OK' if SCHWAB_CLIENT_ID else 'MISSING'} | "
        f"Telegram:{'OK' if TELEGRAM_TOKEN else 'MISSING'}"
    )
    sc = Scanner()
    threading.Thread(target=listen, args=(sc,), daemon=True).start()
    sc.run()
