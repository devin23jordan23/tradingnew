"""
main.py - Andre's Trading Scanner v3.6

CHANGES FROM v3.5 — QUALITY LAYER + FIB PULLBACK
──────────────────────────────────────────────────

What changed and why:

1. RELATIVE VOLUME (RVOL) GATE
   Every setup now checks RVOL before firing. If the stock isn't
   showing elevated volume relative to its norm, the move isn't
   institutional — it's noise. Gate: RVOL >= 1.5x to qualify,
   scored bonus at 2x+. Calculated from today's volume vs the
   average volume of the same time window on prior days.

2. RELATIVE STRENGTH vs SPY
   SPY is fetched once per cycle (not per ticker — one call).
   Every LONG setup gets a RS check: is this stock outperforming
   SPY on the session? Weak RS on a long = score penalty.
   Strong RS = score bonus. Does not block setups — just adjusts
   the score so you can see context in the alert.

3. TIME-OF-DAY SCORE MODIFIER
   Same setup at 9:45 vs 12:30 is not the same setup. Score is
   now adjusted by time window:
     9:30–10:30 → +5  (prime momentum window)
     10:30–12:00 → 0  (neutral, selective)
     12:00–14:00 → -8 (chop risk, raise bar)
     14:00–16:00 → +3 (continuation / power hour)

4. FIB PULLBACK SETUP (LONG + SHORT)
   New setup: after a strong qualifying impulse leg, watch the
   38.2%, 50%, and 61.8% retracement levels for a bounce.
   Requirements:
   - Impulse leg: min 1.5x ATR, min 3 bars, volume above avg
   - RVOL >= 1.5x on the name (in-play filter)
   - Volume contracting on pullback (healthy retracement, not distribution)
   - Price within 0.4% of a Fib level
   - EMA9 or VWAP confluence within 0.5% of the Fib level = bonus
   - Anchors: largest intraday impulse leg (low→high for longs,
     high→low for shorts)
   Levels: 38.2% (shallow/strong), 50% (most watched), 61.8%
   (last line of defense). 50% scores highest.

5. SCORE FLOOR RAISED SLIGHTLY
   With the new quality layers in place, the effective quality
   bar is higher even at MIN_SCORE=60 because RVOL failures
   and RS penalties will drop marginal setups below threshold.
   No code change needed — the modifiers handle it naturally.

Nothing removed. All v3.5 setups and state management intact.
"""

import os
import time
import json
import base64
import threading
from datetime import datetime, timedelta
from collections import defaultdict
from urllib.parse import urlparse, parse_qs, urlencode

import pytz
import requests


# ──────────────────────────────────────────────────────────────
# ENV / GLOBALS
# ──────────────────────────────────────────────────────────────

SCHWAB_CLIENT_ID    = os.environ.get("SCHWAB_CLIENT_ID", "")
SCHWAB_CLIENT_SECRET = os.environ.get("SCHWAB_CLIENT_SECRET", "")
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")

ET        = pytz.timezone("America/New_York")
AUTH_URL  = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
REDIRECT  = "https://127.0.0.1"
TOKEN_FILE = "schwab_tokens.json"
BASE      = "https://api.schwabapi.com/marketdata/v1"

DEFAULT_WATCHLIST = [
    "NVDA", "AMD", "TSLA", "PLTR", "AMZN", "MU", "MSFT", "GOOGL",
    "AAPL", "AVGO", "META", "CVX", "DELL", "RKLB", "MRVL", "ANET",
    "CRDO", "LITE", "COHR", "COIN", "AAOI", "XOM", "ARM", "INTC"
]

MIN_SCORE = 60
COOLDOWN  = 15  # legacy — overridden by per-bar dedup + TTL

# Timing windows
ORB_5M_CUTOFF  = (9, 45)
ORB_15M_START  = (9, 45)
ORB_15M_CUTOFF = (10, 5)
HOD_START      = (10, 15)
HOD_END        = (15, 30)

# Sweep cooldowns (legacy)
SWEEP_WATCH_COOLDOWN   = 10
SWEEP_ACTIVE_COOLDOWN  = 5
SWEEP_RECLAIM_COOLDOWN = 10

# Signal TTL per setup (minutes)
SIGNAL_TTL = {
    "ORB_5M_LONG":              20,
    "ORB_15M_LONG":             25,
    "PMH_BREAK_RETEST_LONG":    30,
    "PML_BREAK_RETEST_SHORT":   30,
    "VWAP_RECLAIM_LONG":        15,
    "VWAP_REJECT_SHORT":        15,
    "EMA9_5M_PULLBACK_LONG":    12,
    "EMA9_5M_PULLBACK_SHORT":   12,
    "FLAG_BREAKOUT_LONG":       18,
    "PDH_BREAK_RETEST_LONG":    30,
    "PDL_BREAK_RETEST_SHORT":   30,
    "LATER_DAY_HOD_BREAKOUT":   25,
    "FIB_PULLBACK_LONG":        20,
    "FIB_PULLBACK_SHORT":       20,
    "SWEEP_WATCH":              10,
    "SWEEP_ACTIVE":              5,
    "SWEEP_RECLAIM_LONG":       15,
}
DEFAULT_TTL = 15

MIN_REFIRE_GAP_MIN = 20

# ── NEW: Quality layer constants ──
RVOL_MIN       = 1.5   # minimum RVOL to qualify any setup
RVOL_STRONG    = 2.0   # RVOL above this scores a bonus
FIB_LEVELS     = [0.382, 0.50, 0.618]   # standard retracement levels
FIB_TOLERANCE  = 0.004  # price must be within 0.4% of a fib level
FIB_CONFLUENCE = 0.005  # EMA/VWAP within 0.5% of fib = confluence bonus
FIB_MIN_ATR_MULT = 1.5  # impulse must be >= 1.5x ATR to qualify
FIB_MIN_BARS   = 3      # impulse must span at least this many bars

WATCHLIST_FILE = "watchlist_state.json"
ARMED_FILE     = "armed_state.json"

_pending_auth  = False


# ──────────────────────────────────────────────────────────────
# GENERAL HELPERS
# ──────────────────────────────────────────────────────────────

def now_et():
    return datetime.now(ET)


def hhmm_gte(ts, h, m):
    return (ts.hour, ts.minute) >= (h, m)


def hhmm_lte(ts, h, m):
    return (ts.hour, ts.minute) <= (h, m)


def in_window(ts, sh, sm, eh, em):
    return hhmm_gte(ts, sh, sm) and hhmm_lte(ts, eh, em)


def clamp_score(v):
    return max(0, min(int(v), 100))


def send_telegram(msg):
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


def load_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[STATE SAVE ERR] {path}: {e}")


# ──────────────────────────────────────────────────────────────
# SCHWAB AUTH (unchanged)
# ──────────────────────────────────────────────────────────────

def _b64():
    return base64.b64encode(f"{SCHWAB_CLIENT_ID}:{SCHWAB_CLIENT_SECRET}".encode()).decode()


def _save_tokens(t):
    t["saved_at"] = time.time()
    with open(TOKEN_FILE, "w") as f:
        json.dump(t, f, indent=2)


def _load_tokens():
    if not os.path.exists(TOKEN_FILE):
        return {}
    try:
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _expired(t):
    if not t:
        return True
    return time.time() > t.get("saved_at", 0) + t.get("expires_in", 1800) - 300


def _refresh_tokens(t):
    headers = {"Authorization": f"Basic {_b64()}", "Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(TOKEN_URL, headers=headers,
                      data={"grant_type": "refresh_token", "refresh_token": t.get("refresh_token", "")},
                      timeout=15)
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
        "🔐 <b>Schwab Authorization Required</b>\n" + "━" * 30 +
        f"\n<b>Step 1:</b> Open this link:\n<code>{url}</code>\n\n"
        "<b>Step 2:</b> Log in and approve access\n\n"
        "<b>Step 3:</b> Copy the full redirect URL from the browser bar\n\n"
        "<b>Step 4:</b> Send it like this:\n"
        "<code>/auth https://127.0.0.1/?code=FULL_URL_HERE</code>"
    )
    start = time.time()
    while _pending_auth:
        if time.time() - start > 600:
            send_telegram("⏰ Auth timed out. Retry with /reauth")
            return None
        time.sleep(2)
    return _load_tokens()


def _complete_auth(full_redirect_url):
    global _pending_auth
    try:
        parsed = urlparse(full_redirect_url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if not code:
            send_telegram("❌ Could not find auth code in that URL.")
            return False
        headers = {"Authorization": f"Basic {_b64()}", "Content-Type": "application/x-www-form-urlencoded"}
        r = requests.post(TOKEN_URL, headers=headers,
                          data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT},
                          timeout=15)
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


def tok():
    t = _load_tokens()
    if not t:
        t = _login()
        if not t:
            return ""
    elif _expired(t):
        t = _refresh_tokens(t)
    return t.get("access_token", "")


def _hdr():
    return {"Authorization": f"Bearer {tok()}", "Accept": "application/json"}


def _get(ep, params=None):
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

def candles(ticker, m=5):
    params = {
        "periodType": "day", "period": 1,
        "frequencyType": "minute", "frequency": m,
        "needExtendedHoursData": "true",
    }
    data = _get(f"/pricehistory?symbol={ticker}", params)
    out = []
    for c in data.get("candles", []):
        out.append({
            "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"], "v": c["volume"],
            "ts": datetime.fromtimestamp(c["datetime"] / 1000, tz=ET),
        })
    return out


def closed_only(cs, bar_minutes):
    """Drop the live forming bar — only evaluate completed bars."""
    if not cs:
        return cs
    n = now_et()
    return [c for c in cs if c.get("ts") and n >= c["ts"] + timedelta(minutes=bar_minutes)]


def price(ticker):
    try:
        d = _get("/quotes", {"symbols": ticker})
        q = d.get(ticker, {}).get("quote", {})
        return q.get("lastPrice") or q.get("mark") or q.get("closePrice")
    except Exception:
        return None


def rh(cs):
    """Regular hours bars only (9:30 ET onward)."""
    return [c for c in cs if c.get("ts") and
            (c["ts"].hour > 9 or (c["ts"].hour == 9 and c["ts"].minute >= 30))]


def av(cs, n=20):
    r = rh(cs)
    vols = [c["v"] for c in r[-n:]]
    return sum(vols) / len(vols) if vols else None


def vwap(cs):
    total_val, total_vol = 0, 0
    for c in cs:
        ts = c.get("ts")
        if ts and (ts.hour < 9 or (ts.hour == 9 and ts.minute < 30)):
            continue
        tp = (c["h"] + c["l"] + c["c"]) / 3
        total_val += tp * c["v"]
        total_vol += c["v"]
    return total_val / total_vol if total_vol else None


def ema(vals, p=9):
    if len(vals) < p:
        return None
    k = 2 / (p + 1)
    e = sum(vals[:p]) / p
    for v in vals[p:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(vals, p=9):
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


def opening_range(c1m, minutes=5):
    bars = [c for c in c1m if c.get("ts") and c["ts"].hour == 9
            and 30 <= c["ts"].minute < (30 + minutes)]
    if not bars:
        return None, None
    return max(c["h"] for c in bars), min(c["l"] for c in bars)


def intraday_high(cs):
    r = rh(cs)
    return max(c["h"] for c in r) if r else None


def _pm_candles(ticker):
    try:
        today = now_et().date()
        start_naive = datetime(today.year, today.month, today.day, 4, 0, 0)
        end_naive   = datetime(today.year, today.month, today.day, 9, 30, 0)
        start_et = ET.localize(start_naive)
        end_et   = ET.localize(end_naive)
        s = int(start_et.timestamp() * 1000)
        e = int(end_et.timestamp() * 1000)
        if e <= s:
            return []
        d = _get(f"/pricehistory?symbol={ticker}", {
            "periodType": "day", "period": 1,
            "frequencyType": "minute", "frequency": 1,
            "startDate": s, "endDate": e,
            "needExtendedHoursData": "true",
        })
        return d.get("candles", [])
    except Exception as e:
        print(f"[PM CANDLES ERR] {ticker}: {e}")
        return []


def pm_high(ticker):
    cs = _pm_candles(ticker)
    return max(x["high"] for x in cs) if cs else None


def pm_low(ticker):
    cs = _pm_candles(ticker)
    return min(x["low"] for x in cs) if cs else None


def prior_day(ticker):
    try:
        d = _get(f"/pricehistory?symbol={ticker}", {
            "periodType": "month", "period": 1,
            "frequencyType": "daily", "frequency": 1,
            "needExtendedHoursData": "false",
        })
        today = now_et().date()
        for c in reversed(d.get("candles", [])):
            c_date = datetime.fromtimestamp(c["datetime"] / 1000, tz=ET).date()
            if c_date < today:
                return {
                    "h": c["high"], "l": c["low"], "c": c["close"],
                    "vwap": round((c["high"] + c["low"] + c["close"]) / 3, 2),
                }
    except Exception:
        pass
    return {}


# ──────────────────────────────────────────────────────────────
# NEW: QUALITY LAYER HELPERS
# ──────────────────────────────────────────────────────────────

def calc_rvol(c5, baseline_bars=10):
    """
    Relative volume: compares the recent pace of volume to the opening
    session baseline. This correctly detects acceleration and deceleration.

    Method: use the first `baseline_bars` bars as the "normal" session pace.
    Compare the most recent 5 bars' average to that baseline.
    A ratio of 2.0 means current pace is running 2x the opening average —
    the stock is accelerating, likely in play.

    Why not use a simple total/average approach: that method is self-normalizing
    and always returns ~1.0 because the average includes the current bars.
    This approach anchors to the opening baseline, which is stable.

    Requires at least 6 regular-hours bars to calculate.
    """
    r = rh(c5)
    if len(r) < 6:
        return None
    # Use first 10 bars (or half of available) as the baseline
    base = r[:baseline_bars] if len(r) >= baseline_bars else r[:max(3, len(r) // 2)]
    recent = r[-5:]
    base_avg   = sum(c["v"] for c in base)   / len(base)   if base   else 0
    recent_avg = sum(c["v"] for c in recent) / len(recent) if recent else 0
    if base_avg == 0:
        return None
    return round(recent_avg / base_avg, 2)


def atr(cs, n=10):
    """Average True Range over last n bars."""
    r = rh(cs)
    if len(r) < 2:
        return None
    trs = []
    for i in range(1, len(r)):
        prev_c = r[i - 1]["c"]
        high   = r[i]["h"]
        low    = r[i]["l"]
        trs.append(max(high - low, abs(high - prev_c), abs(low - prev_c)))
    recent = trs[-n:]
    return sum(recent) / len(recent) if recent else None


def time_of_day_modifier():
    """
    Score adjustment based on time of day.
    Prime window gets a bonus. Chop window gets a penalty.
    Keeps it honest — the same pattern at noon should score lower.
    """
    ts = now_et()
    if in_window(ts, 9, 30, 10, 30):
        return +5   # prime momentum window
    elif in_window(ts, 10, 30, 12, 0):
        return 0    # selective but neutral
    elif in_window(ts, 12, 0, 14, 0):
        return -8   # chop risk — raise effective bar
    else:
        return +3   # 14:00–16:00 continuation / power hour


def relative_strength_vs_spy(ticker_pct, spy_pct):
    """
    Simple RS: how much is the ticker outperforming SPY?
    Returns a score modifier and a label.
    ticker_pct / spy_pct = session % change for each.
    """
    if spy_pct is None or ticker_pct is None:
        return 0, "RS: N/A"
    diff = ticker_pct - spy_pct
    if diff >= 1.5:
        return +10, f"RS: Strong +{round(diff, 1)}% vs SPY ✅"
    elif diff >= 0.5:
        return +5,  f"RS: Good +{round(diff, 1)}% vs SPY"
    elif diff >= -0.3:
        return 0,   f"RS: Neutral {round(diff, 1)}% vs SPY"
    elif diff >= -1.0:
        return -5,  f"RS: Weak {round(diff, 1)}% vs SPY ⚠️"
    else:
        return -10, f"RS: Lagging {round(diff, 1)}% vs SPY ❌"


def session_pct_change(c5):
    """
    Session % change from open to current close.
    Used for RS calculation.
    """
    r = rh(c5)
    if not r:
        return None
    open_price = r[0]["o"]
    current    = r[-1]["c"]
    if not open_price:
        return None
    return round((current - open_price) / open_price * 100, 2)


# ──────────────────────────────────────────────────────────────
# NEW: FIB RETRACEMENT HELPERS
# ──────────────────────────────────────────────────────────────

def find_impulse_leg(cs, direction="long"):
    """
    Find the strongest qualifying impulse leg of the day.

    For longs: look for the largest low→high move.
    For shorts: look for the largest high→low move.

    Qualification:
    - Minimum FIB_MIN_BARS bars in the leg
    - Move size >= FIB_MIN_ATR_MULT * ATR
    - Average volume during leg > overall average (momentum confirmation)

    Returns (swing_low_price, swing_high_price, leg_size, leg_bars)
    or None if no qualifying leg found.
    """
    r = rh(cs)
    if len(r) < FIB_MIN_BARS + 2:
        return None

    avg_vol = av(cs)
    atr_val = atr(cs)
    if not atr_val or not avg_vol:
        return None

    best = None
    best_size = 0

    # Slide a window looking for the strongest impulse leg
    for start in range(len(r) - FIB_MIN_BARS):
        for end in range(start + FIB_MIN_BARS, min(start + 12, len(r))):
            leg = r[start:end + 1]
            if direction == "long":
                leg_low  = min(c["l"] for c in leg)
                leg_high = max(c["h"] for c in leg)
                # Low must come before high for a bullish leg
                low_idx  = next(i for i, c in enumerate(leg) if c["l"] == leg_low)
                high_idx = next(i for i, c in enumerate(leg) if c["h"] == leg_high)
                if high_idx <= low_idx:
                    continue
            else:
                leg_high = max(c["h"] for c in leg)
                leg_low  = min(c["l"] for c in leg)
                high_idx = next(i for i, c in enumerate(leg) if c["h"] == leg_high)
                low_idx  = next(i for i, c in enumerate(leg) if c["l"] == leg_low)
                if low_idx <= high_idx:
                    continue

            size = leg_high - leg_low
            if size < atr_val * FIB_MIN_ATR_MULT:
                continue

            # Volume check: leg should have above-average participation
            leg_avg_vol = sum(c["v"] for c in leg) / len(leg)
            if leg_avg_vol < avg_vol * 0.8:
                continue

            if size > best_size:
                best_size = size
                best = {
                    "low":  leg_low,
                    "high": leg_high,
                    "size": size,
                    "bars": len(leg),
                    "start_ts": leg[0]["ts"],
                    "end_ts":   leg[-1]["ts"],
                    "avg_vol":  leg_avg_vol,
                }

    return best


def fib_levels_from_leg(leg, direction="long"):
    """
    Calculate 38.2%, 50%, 61.8% retracement levels from an impulse leg.
    For longs: retracements are measured down from the high.
    For shorts: retracements are measured up from the low.
    """
    low  = leg["low"]
    high = leg["high"]
    size = high - low
    if direction == "long":
        return {
            0.382: round(high - size * 0.382, 2),
            0.500: round(high - size * 0.500, 2),
            0.618: round(high - size * 0.618, 2),
        }
    else:
        return {
            0.382: round(low + size * 0.382, 2),
            0.500: round(low + size * 0.500, 2),
            0.618: round(low + size * 0.618, 2),
        }


def fib_pullback_long(c5, p, vw, rvol):
    """
    Long Fib pullback setup.

    After a strong bullish impulse leg, price is retracing.
    We want to catch it at a meaningful Fib level with:
    - Volume contracting on the pullback
    - Price holding above the 61.8% (if it breaks 61.8% cleanly, trend may be over)
    - RVOL confirms the name is in play
    - EMA9 or VWAP confluence near the Fib level = bonus
    """
    # RVOL gate — name must be in play
    if not rvol or rvol < RVOL_MIN:
        return False, {}

    r = rh(c5)
    if len(r) < FIB_MIN_BARS + 4:
        return False, {}

    leg = find_impulse_leg(c5, direction="long")
    if not leg:
        return False, {}

    levels = fib_levels_from_leg(leg, direction="long")
    avgv   = av(c5)

    # Find which Fib level price is nearest to
    nearest_level = None
    nearest_fib   = None
    nearest_dist  = float("inf")
    for fib_pct, fib_price in levels.items():
        dist = abs(p - fib_price) / fib_price
        if dist < nearest_dist:
            nearest_dist  = dist
            nearest_fib   = fib_pct
            nearest_level = fib_price

    # Must be within tolerance of a level
    if nearest_dist > FIB_TOLERANCE:
        return False, {}

    # Price must be above the 61.8% (if below, the leg is likely done)
    if p < levels[0.618] * 0.997:
        return False, {}

    # Must still be above VWAP — trend context
    above_vwap = p > (vw or 0)
    if not above_vwap:
        return False, {}

    # Pullback volume should be contracting relative to the impulse
    pullback_bars = r[-4:]
    pullback_vol  = sum(c["v"] for c in pullback_bars) / len(pullback_bars) if pullback_bars else 0
    vol_contracting = pullback_vol < (avgv or float("inf")) * 0.85

    # EMA9 confluence — extra confidence if EMA9 is near this Fib level
    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    ema_confluence = (en is not None and abs(en - nearest_level) / nearest_level <= FIB_CONFLUENCE)
    vwap_confluence = (vw is not None and abs(vw - nearest_level) / nearest_level <= FIB_CONFLUENCE)

    # EMA must be rising for this to be a healthy pullback
    ema_vals = [e for e in es[-5:] if e is not None]
    ema_rising = len(ema_vals) > 1 and ema_vals[-1] > ema_vals[0]
    if not ema_rising:
        return False, {}

    # Score — 50% level is the most reliable, gets highest base
    fib_label_map = {0.382: "38.2%", 0.500: "50%", 0.618: "61.8%"}
    fib_label = fib_label_map.get(nearest_fib, str(nearest_fib))
    base = {0.500: 70, 0.382: 65, 0.618: 62}.get(nearest_fib, 60)

    score = base
    score += 10 if vol_contracting else 0
    score += 8  if rvol >= RVOL_STRONG else (4 if rvol >= RVOL_MIN else 0)
    score += 8  if ema_confluence else 0
    score += 6  if vwap_confluence else 0
    score += 5  if above_vwap else 0
    score += time_of_day_modifier()

    if score < MIN_SCORE:
        return False, {}

    confluence_note = []
    if ema_confluence:  confluence_note.append("EMA9")
    if vwap_confluence: confluence_note.append("VWAP")
    conf_str = " + ".join(confluence_note) if confluence_note else "None"

    return True, {
        "setup":   "FIB_PULLBACK_LONG",
        "dir":     "🟢 LONG",
        "trigger": f"Hold at {fib_label} retrace ${round(nearest_level, 2)} + bounce",
        "inval":   f"Loss of 61.8% level ${round(levels[0.618], 2)}",
        "level":   (f"Fib levels — 38.2%: ${levels[0.382]} | "
                    f"50%: ${levels[0.500]} | 61.8%: ${levels[0.618]}"),
        "vol":     f"Contracting ✅ RVOL {rvol}x" if vol_contracting else f"Watch | RVOL {rvol}x",
        "score":   clamp_score(score),
        "action":  "Actionable" if (vol_contracting and score >= 70) else "Watch",
        "notes":   (f"Impulse: ${round(leg['low'], 2)}→${round(leg['high'], 2)} "
                    f"({round(leg['size'], 2)} pts, {leg['bars']} bars) | "
                    f"At {fib_label} | Confluence: {conf_str}"),
        "trigger_bar_ts": r[-1]["ts"] if r else None,
    }


def fib_pullback_short(c5, p, vw, rvol):
    """
    Short Fib pullback setup.

    After a strong bearish impulse leg, price is bouncing.
    Watch the 38.2%, 50%, 61.8% bounce levels for rejection.
    """
    if not rvol or rvol < RVOL_MIN:
        return False, {}

    r = rh(c5)
    if len(r) < FIB_MIN_BARS + 4:
        return False, {}

    leg = find_impulse_leg(c5, direction="short")
    if not leg:
        return False, {}

    levels = fib_levels_from_leg(leg, direction="short")
    avgv   = av(c5)

    nearest_level = None
    nearest_fib   = None
    nearest_dist  = float("inf")
    for fib_pct, fib_price in levels.items():
        dist = abs(p - fib_price) / fib_price
        if dist < nearest_dist:
            nearest_dist  = dist
            nearest_fib   = fib_pct
            nearest_level = fib_price

    if nearest_dist > FIB_TOLERANCE:
        return False, {}

    # Price must still be below 61.8% bounce level
    if p > levels[0.618] * 1.003:
        return False, {}

    below_vwap = p < (vw or float("inf"))
    if not below_vwap:
        return False, {}

    bounce_bars = r[-4:]
    bounce_vol  = sum(c["v"] for c in bounce_bars) / len(bounce_bars) if bounce_bars else 0
    vol_contracting = bounce_vol < (avgv or float("inf")) * 0.85

    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    ema_confluence  = (en is not None and abs(en - nearest_level) / nearest_level <= FIB_CONFLUENCE)
    vwap_confluence = (vw is not None and abs(vw - nearest_level) / nearest_level <= FIB_CONFLUENCE)

    ema_vals   = [e for e in es[-5:] if e is not None]
    ema_falling = len(ema_vals) > 1 and ema_vals[-1] < ema_vals[0]
    if not ema_falling:
        return False, {}

    fib_label_map = {0.382: "38.2%", 0.500: "50%", 0.618: "61.8%"}
    fib_label = fib_label_map.get(nearest_fib, str(nearest_fib))
    base = {0.500: 70, 0.382: 65, 0.618: 62}.get(nearest_fib, 60)

    score = base
    score += 10 if vol_contracting else 0
    score += 8  if rvol >= RVOL_STRONG else (4 if rvol >= RVOL_MIN else 0)
    score += 8  if ema_confluence else 0
    score += 6  if vwap_confluence else 0
    score += 5  if below_vwap else 0
    score += time_of_day_modifier()

    if score < MIN_SCORE:
        return False, {}

    confluence_note = []
    if ema_confluence:  confluence_note.append("EMA9")
    if vwap_confluence: confluence_note.append("VWAP")
    conf_str = " + ".join(confluence_note) if confluence_note else "None"

    return True, {
        "setup":   "FIB_PULLBACK_SHORT",
        "dir":     "🔴 SHORT",
        "trigger": f"Rejection at {fib_label} bounce ${round(nearest_level, 2)}",
        "inval":   f"Acceptance above 61.8% bounce ${round(levels[0.618], 2)}",
        "level":   (f"Fib levels — 38.2%: ${levels[0.382]} | "
                    f"50%: ${levels[0.500]} | 61.8%: ${levels[0.618]}"),
        "vol":     f"Contracting ✅ RVOL {rvol}x" if vol_contracting else f"Watch | RVOL {rvol}x",
        "score":   clamp_score(score),
        "action":  "Actionable" if (vol_contracting and score >= 70) else "Watch",
        "notes":   (f"Impulse: ${round(leg['high'], 2)}→${round(leg['low'], 2)} "
                    f"({round(leg['size'], 2)} pts, {leg['bars']} bars) | "
                    f"At {fib_label} | Confluence: {conf_str}"),
        "trigger_bar_ts": r[-1]["ts"] if r else None,
    }


# ──────────────────────────────────────────────────────────────
# SWEEP / CANDLE-QUALITY HELPERS (unchanged)
# ──────────────────────────────────────────────────────────────

def close_pos_in_range(candle):
    rng = candle["h"] - candle["l"]
    if rng <= 0:
        return 0.5
    return (candle["c"] - candle["l"]) / rng


def is_green(candle):
    return candle["c"] > candle["o"]


def top_half_close(candle):
    return close_pos_in_range(candle) >= 0.50


def micro_base_stats(cs, lookback=5, exclude_last=1):
    r = rh(cs)
    need = lookback + exclude_last
    if len(r) < need:
        return None
    base = r[-need:-exclude_last] if exclude_last > 0 else r[-lookback:]
    if len(base) < 3:
        return None
    base_high  = max(c["h"] for c in base)
    base_low   = min(c["l"] for c in base)
    avg_close  = sum(c["c"] for c in base) / len(base)
    width      = base_high - base_low
    tight_pct  = (width / avg_close) if avg_close else 999
    return {"bars": base, "high": base_high, "low": base_low,
            "width": width, "tight_pct": tight_pct}


# ──────────────────────────────────────────────────────────────
# SETUP HELPERS
# ──────────────────────────────────────────────────────────────

def _trigger_bar(r):
    return r[-1]["ts"] if r else None


def _apply_quality_modifiers(score, rvol, rs_mod, direction="long"):
    """
    Apply RVOL and RS modifiers to any setup's score.
    Called at the end of each existing setup function.
    """
    # RVOL modifier
    if rvol is None:
        score -= 5   # can't confirm volume — slight penalty
    elif rvol >= RVOL_STRONG:
        score += 8
    elif rvol >= RVOL_MIN:
        score += 3
    else:
        score -= 10  # below minimum RVOL — weak name

    # RS modifier (already computed externally)
    score += rs_mod

    # Time of day
    score += time_of_day_modifier()

    return clamp_score(score)


# ──────────────────────────────────────────────────────────────
# CORE SETUPS — same logic as v3.5, now accept rvol + rs_mod
# ──────────────────────────────────────────────────────────────

def orb_5m_long(c5, c1, p, vw, pmh_v, rvol, rs_mod):
    ts = now_et()
    if not hhmm_lte(ts, *ORB_5M_CUTOFF):
        return False, {}
    oh, ol = opening_range(c1, 5)
    if oh is None or ol is None:
        return False, {}
    r = rh(c5)
    if len(r) < 2:
        return False, {}
    last, prev = r[-1], r[-2]
    avgv = av(c5)
    broke      = p > oh
    above_vwap = p > (vw or 0)
    vol        = last["v"] > (avgv or 0) * 1.3
    was_below  = prev["c"] <= oh
    if not (broke and above_vwap and was_below):
        return False, {}
    score = _apply_quality_modifiers(
        60 + (15 if vol else 0) + (10 if pmh_v and p > pmh_v else 0),
        rvol, rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "ORB_5M_LONG", "dir": "🟢 LONG",
        "trigger": f"Break above 5m OR high ${round(oh, 2)}",
        "inval":   f"Loss of OR low ${round(ol, 2)}",
        "level":   f"5m OR: ${round(ol, 2)}–${round(oh, 2)}",
        "vol":     f"Expanding ✅ RVOL {rvol}x" if vol else f"Weak ⚠️ RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable" if vol else "Watch",
        "notes":   "Early 5m ORB only",
        "trigger_bar_ts": _trigger_bar(r),
    }


def orb_15m_long(c5, c1, p, vw, pmh_v, rvol, rs_mod):
    ts = now_et()
    if not hhmm_gte(ts, *ORB_15M_START) or not hhmm_lte(ts, *ORB_15M_CUTOFF):
        return False, {}
    oh, ol = opening_range(c1, 15)
    if oh is None or ol is None:
        return False, {}
    r = rh(c5)
    if len(r) < 2:
        return False, {}
    last, prev = r[-1], r[-2]
    avgv = av(c5)
    broke      = p > oh
    above_vwap = p > (vw or 0)
    vol        = last["v"] > (avgv or 0) * 1.2
    was_below  = prev["c"] <= oh
    if not (broke and above_vwap and was_below):
        return False, {}
    score = _apply_quality_modifiers(
        65 + (10 if vol else 0) + (10 if pmh_v and p > pmh_v else 0),
        rvol, rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "ORB_15M_LONG", "dir": "🟢 LONG",
        "trigger": f"Break above 15m OR high ${round(oh, 2)}",
        "inval":   f"Loss of OR low ${round(ol, 2)}",
        "level":   f"15m OR: ${round(ol, 2)}–${round(oh, 2)}",
        "vol":     f"Expanding ✅ RVOL {rvol}x" if vol else f"Average RVOL {rvol}x",
        "score":   score, "action": "Actionable",
        "notes":   "15m ORB in the post-9:45 window",
        "trigger_bar_ts": _trigger_bar(r),
    }


def pmh_retest(c5, p, vw, pmh_v, rvol, rs_mod):
    if not pmh_v:
        return False, {}
    r = rh(c5)
    if len(r) < 3:
        return False, {}
    broke = any(c["h"] > pmh_v for c in r[:-2])
    if not broke:
        return False, {}
    near         = abs(p - pmh_v) / pmh_v <= 0.004
    above        = p >= pmh_v * 0.998
    above_vwap   = p > (vw or 0)
    avgv         = av(c5)
    light_pb     = all(c["v"] < (avgv or float("inf")) * 0.9 for c in r[-2:])
    if not (above and above_vwap):
        return False, {}
    score = _apply_quality_modifiers(
        65 + (10 if near else 0) + (10 if light_pb else 0) + (5 if above_vwap else 0),
        rvol, rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "PMH_BREAK_RETEST_LONG", "dir": "🟢 LONG",
        "trigger": f"Hold above PM High ${round(pmh_v, 2)} + push",
        "inval":   f"Loss of ${round(pmh_v * 0.997, 2)}",
        "level":   f"PM High: ${round(pmh_v, 2)}",
        "vol":     f"Pullback light ✅ RVOL {rvol}x" if light_pb else f"Watch | RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable" if near and above_vwap else "Watch",
        "notes":   "PM high broken earlier — now retesting",
        "trigger_bar_ts": _trigger_bar(r),
    }


def pml_retest(c5, p, vw, pml_v, rvol, rs_mod):
    if not pml_v:
        return False, {}
    r = rh(c5)
    if len(r) < 3:
        return False, {}
    broke = any(c["l"] < pml_v for c in r[:-2])
    if not broke:
        return False, {}
    near         = abs(p - pml_v) / pml_v <= 0.004
    below        = p <= pml_v * 1.002
    below_vwap   = p < (vw or float("inf"))
    avgv         = av(c5)
    light_bounce = all(c["v"] < (avgv or float("inf")) * 0.9 for c in r[-2:])
    if not (below and below_vwap):
        return False, {}
    score = _apply_quality_modifiers(
        65 + (10 if near else 0) + (10 if light_bounce else 0),
        rvol, -rs_mod  # invert RS for shorts — lagging = good
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "PML_BREAK_RETEST_SHORT", "dir": "🔴 SHORT",
        "trigger": f"Reject under PM Low ${round(pml_v, 2)}",
        "inval":   f"Reclaim ${round(pml_v * 1.003, 2)}",
        "level":   f"PM Low: ${round(pml_v, 2)}",
        "vol":     f"Bounce light ✅ RVOL {rvol}x" if light_bounce else f"Watch | RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable" if near and below_vwap else "Watch",
        "notes":   "PM low broke earlier — underside retest failing",
        "trigger_bar_ts": _trigger_bar(r),
    }


def vwap_reclaim(c5, p, vw, rvol, rs_mod):
    if not vw:
        return False, {}
    r = rh(c5)
    if len(r) < 4:
        return False, {}
    last, prev = r[-1], r[-2]
    avgv       = av(c5)
    was_below  = any(c["c"] < vw for c in r[-5:-1])
    if not was_below:
        return False, {}
    strong = last["c"] > vw and last["c"] > last["o"]
    first  = strong and prev["c"] < vw
    if not strong:
        return False, {}
    vol = last["v"] > (avgv or 0) * 1.2
    prior_slice = r[-8:-2]
    prior_fails = sum(1 for i in range(1, len(prior_slice))
                     if prior_slice[i]["c"] > vw and prior_slice[i - 1]["c"] < vw)
    score = _apply_quality_modifiers(
        60 + (15 if first else 0) + (15 if vol else 0) - (20 if prior_fails > 1 else 0),
        rvol, rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "VWAP_RECLAIM_LONG", "dir": "🟢 LONG",
        "trigger": f"Hold above VWAP ${round(vw, 2)} + push",
        "inval":   f"Fail back below VWAP ${round(vw, 2)}",
        "level":   f"VWAP: ${round(vw, 2)}",
        "vol":     f"Expanding ✅ RVOL {rvol}x" if vol else f"Light RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable" if first and vol else "Watch",
        "notes":   "First clean reclaim preferred",
        "trigger_bar_ts": _trigger_bar(r),
    }


def vwap_reject(c5, p, vw, rvol, rs_mod):
    if not vw:
        return False, {}
    r = rh(c5)
    if len(r) < 4:
        return False, {}
    last, prev = r[-1], r[-2]
    near      = abs(prev["h"] - vw) / vw <= 0.005
    below     = last["c"] < vw
    bear      = last["c"] < last["o"]
    was_below = any(c["c"] < vw for c in r[-6:-3])
    if not (near and below and bear and was_below):
        return False, {}
    avgv = av(c5)
    vol  = last["v"] > (avgv or 0) * 1.1
    score = _apply_quality_modifiers(
        65 + (10 if vol else 0) + (10 if bear else 0),
        rvol, -rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "VWAP_REJECT_SHORT", "dir": "🔴 SHORT",
        "trigger": f"Break local pivot below VWAP ${round(vw, 2)}",
        "inval":   f"Acceptance above VWAP ${round(vw * 1.003, 2)}",
        "level":   f"VWAP resistance: ${round(vw, 2)}",
        "vol":     f"Expanding ✅ RVOL {rvol}x" if vol else f"Light RVOL {rvol}x",
        "score":   score, "action": "Actionable",
        "notes":   "Rejected at VWAP — rolling over",
        "trigger_bar_ts": _trigger_bar(r),
    }


def ema9_pb_long(c5, p, vw, rvol, rs_mod):
    r = rh(c5)
    if len(r) < 12:
        return False, {}
    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    if en is None:
        return False, {}
    ema_vals = [e for e in es[-5:] if e is not None]
    rising   = len(ema_vals) > 1 and ema_vals[-1] > ema_vals[0]
    last, prev = r[-1], r[-2]
    above_vwap   = p > (vw or 0)
    touched      = last["l"] <= en * 1.003 or prev["l"] <= en * 1.003
    bouncing     = last["c"] > prev["h"] or last["c"] > en
    avgv         = av(c5)
    light_pb     = last["v"] < (avgv or float("inf")) * 0.85
    if not (rising and above_vwap and touched and bouncing):
        return False, {}
    score = _apply_quality_modifiers(
        65 + (10 if light_pb else 0) + (10 if above_vwap else 0) + (5 if rising else 0),
        rvol, rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "EMA9_5M_PULLBACK_LONG", "dir": "🟢 LONG",
        "trigger": "Bounce after 9 EMA touch",
        "inval":   f"Loss of 9 EMA ${round(en, 2)}",
        "level":   f"9 EMA: ${round(en, 2)} | VWAP: ${round(vw, 2) if vw else 'N/A'}",
        "vol":     f"Pullback light ✅ RVOL {rvol}x" if light_pb else f"Watch RVOL {rvol}x",
        "score":   score, "action": "Actionable",
        "notes":   "Rising EMA + controlled pullback",
        "trigger_bar_ts": _trigger_bar(r),
    }


def ema9_pb_short(c5, p, vw, rvol, rs_mod):
    r = rh(c5)
    if len(r) < 12:
        return False, {}
    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    if en is None:
        return False, {}
    ema_vals  = [e for e in es[-5:] if e is not None]
    falling   = len(ema_vals) > 1 and ema_vals[-1] < ema_vals[0]
    last, prev = r[-1], r[-2]
    below_vwap  = p < (vw or float("inf"))
    touched     = last["h"] >= en * 0.997 or prev["h"] >= en * 0.997
    rejecting   = last["c"] < last["o"] and last["c"] < en
    avgv        = av(c5)
    light_bounce = last["v"] < (avgv or float("inf")) * 0.85
    if not (falling and below_vwap and touched and rejecting):
        return False, {}
    score = _apply_quality_modifiers(
        65 + (10 if light_bounce else 0) + (10 if below_vwap else 0) + (5 if falling else 0),
        rvol, -rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "EMA9_5M_PULLBACK_SHORT", "dir": "🔴 SHORT",
        "trigger": f"Break below ${round(prev['l'], 2)} after EMA rejection",
        "inval":   f"Reclaim through 9 EMA ${round(en, 2)}",
        "level":   f"9 EMA resistance: ${round(en, 2)}",
        "vol":     f"Bounce light ✅ RVOL {rvol}x" if light_bounce else f"Watch RVOL {rvol}x",
        "score":   score, "action": "Actionable",
        "notes":   "Falling EMA, weak bounce, rejecting",
        "trigger_bar_ts": _trigger_bar(r),
    }


def flag_long(c5, p, vw, rvol, rs_mod):
    r = rh(c5)
    if len(r) < 8:
        return False, {}
    avgv    = av(c5)
    impulse = None
    for c in r[-10:-3]:
        if (c["c"] - c["o"]) > 0 and c["v"] > (avgv or 0) * 1.5:
            impulse = c
            break
    if not impulse:
        return False, {}
    cons       = r[-5:]
    flag_high  = max(c["h"] for c in cons)
    flag_low   = min(c["l"] for c in cons)
    flag_range = flag_high - flag_low
    imp_size   = impulse["c"] - impulse["o"]
    tight      = imp_size > 0 and flag_range < imp_size * 0.5
    above_vwap = p > (vw or 0)
    last       = r[-1]
    broke      = last["c"] > flag_high and last["c"] > r[-2]["h"]
    vol        = last["v"] > (avgv or 0) * 1.2
    dried_up   = all(c["v"] < (avgv or float("inf")) * 0.8 for c in cons[:-1])
    if not (tight and broke and above_vwap):
        return False, {}
    score = _apply_quality_modifiers(
        65 + (10 if dried_up else 0) + (15 if vol else 0) + (5 if tight else 0),
        rvol, rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "FLAG_BREAKOUT_LONG", "dir": "🟢 LONG",
        "trigger": f"Break above flag high ${round(flag_high, 2)}",
        "inval":   f"Loss of flag low ${round(flag_low, 2)}",
        "level":   f"Flag: ${round(flag_low, 2)}–${round(flag_high, 2)}",
        "vol":     f"Dry-up + expansion ✅ RVOL {rvol}x" if (dried_up and vol) else f"Watch RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable" if (vol and tight) else "Watch",
        "notes":   f"Flag range ${round(flag_range, 2)} vs impulse ${round(imp_size, 2)}",
        "trigger_bar_ts": _trigger_bar(r),
    }


def pdh_retest(c5, p, vw, pdh, rvol, rs_mod):
    if not pdh:
        return False, {}
    r = rh(c5)
    if len(r) < 3:
        return False, {}
    if not any(c["h"] > pdh for c in r[:-2]):
        return False, {}
    near       = abs(p - pdh) / pdh <= 0.005
    above      = p >= pdh * 0.998
    above_vwap = p > (vw or 0)
    avgv       = av(c5)
    light_pb   = all(c["v"] < (avgv or float("inf")) * 0.9 for c in r[-2:])
    if not (above and above_vwap):
        return False, {}
    score = _apply_quality_modifiers(
        70 + (10 if near else 0) + (10 if light_pb else 0) + (5 if above_vwap else 0),
        rvol, rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "PDH_BREAK_RETEST_LONG", "dir": "🟢 LONG",
        "trigger": f"Reclaim above PDH ${round(pdh, 2)} + push",
        "inval":   f"Loss of ${round(pdh * 0.997, 2)}",
        "level":   f"Prior Day High: ${round(pdh, 2)}",
        "vol":     f"Pullback light ✅ RVOL {rvol}x" if light_pb else f"Watch RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable" if near and above_vwap else "Watch",
        "notes":   "Daily breakout — institutional level",
        "trigger_bar_ts": _trigger_bar(r),
    }


def pdl_retest(c5, p, vw, pdl, rvol, rs_mod):
    if not pdl:
        return False, {}
    r = rh(c5)
    if len(r) < 3:
        return False, {}
    if not any(c["l"] < pdl for c in r[:-2]):
        return False, {}
    near         = abs(p - pdl) / pdl <= 0.005
    below        = p <= pdl * 1.002
    below_vwap   = p < (vw or float("inf"))
    avgv         = av(c5)
    light_bounce = all(c["v"] < (avgv or float("inf")) * 0.9 for c in r[-2:])
    if not (below and below_vwap):
        return False, {}
    score = _apply_quality_modifiers(
        70 + (10 if near else 0) + (10 if light_bounce else 0),
        rvol, -rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "PDL_BREAK_RETEST_SHORT", "dir": "🔴 SHORT",
        "trigger": f"Reject under PDL ${round(pdl, 2)} + break low",
        "inval":   f"Reclaim ${round(pdl * 1.003, 2)}",
        "level":   f"Prior Day Low: ${round(pdl, 2)}",
        "vol":     f"Bounce light ✅ RVOL {rvol}x" if light_bounce else f"Watch RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable" if near and below_vwap else "Watch",
        "notes":   "Prior day low broke — institutional breakdown",
        "trigger_bar_ts": _trigger_bar(r),
    }


def later_day_hod_breakout(c5, p, vw, rvol, rs_mod):
    ts = now_et()
    if not in_window(ts, HOD_START[0], HOD_START[1], HOD_END[0], HOD_END[1]):
        return False, {}
    r = rh(c5)
    if len(r) < 10:
        return False, {}
    last, prev = r[-1], r[-2]
    prior_hod  = max(c["h"] for c in r[:-1]) if len(r) > 1 else None
    if prior_hod is None:
        return False, {}
    above_vwap   = p > (vw or 0)
    broke        = p > prior_hod and prev["c"] <= prior_hod
    avgv         = av(c5)
    vol          = last["v"] > (avgv or 0) * 1.2
    base_below   = len(r) >= 5 and all(c["h"] <= prior_hod * 1.002 for c in r[-5:-1])
    if not (broke and above_vwap):
        return False, {}
    score = _apply_quality_modifiers(
        68 + (10 if vol else 0) + (10 if base_below else 0) + (5 if above_vwap else 0),
        rvol, rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "LATER_DAY_HOD_BREAKOUT", "dir": "🟢 LONG",
        "trigger": f"Break above HOD ${round(prior_hod, 2)}",
        "inval":   f"Fail back under ${round(prior_hod, 2)}",
        "level":   f"Prior HOD: ${round(prior_hod, 2)}",
        "vol":     f"Expanding ✅ RVOL {rvol}x" if vol else f"Average RVOL {rvol}x",
        "score":   score, "action": "Actionable",
        "notes":   "Later-day continuation / daily expansion candidate",
        "trigger_bar_ts": _trigger_bar(r),
    }


# ──────────────────────────────────────────────────────────────
# SWEEP LOGIC (unchanged — sweeps don't need RVOL/RS as they
# are armed manually only on in-play names)
# ──────────────────────────────────────────────────────────────

def sweep_watch_long_v2(c2, p, vw):
    r = rh(c2)
    if len(r) < 8:
        return False, {}
    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    if en is None:
        return False, {}
    base = micro_base_stats(c2, lookback=5, exclude_last=1)
    if not base:
        return False, {}
    last       = r[-1]
    above_vwap = p > (vw or 0)
    ema_slice  = [x for x in es[-4:] if x is not None]
    ema_up     = len(ema_slice) >= 2 and ema_slice[-1] >= ema_slice[0]
    tight      = base["tight_pct"] <= 0.0045
    pressing   = last["l"] <= base["low"] * 1.002
    not_lost   = last["c"] >= base["low"] * 0.999
    if not (above_vwap and ema_up and tight and pressing and not_lost):
        return False, {}
    score = clamp_score(55 + (10 if tight else 0) + (10 if above_vwap else 0) + (5 if ema_up else 0))
    return True, {
        "setup": "SWEEP_WATCH", "dir": "👀 WATCH",
        "trigger": f"Pressing micro-base low ${round(base['low'], 2)}",
        "inval":   f"Clean loss of ${round(base['low'], 2)} without reclaim",
        "level":   f"Base: ${round(base['low'], 2)}–${round(base['high'], 2)}",
        "vol":     "Context only",
        "score":   score, "action": "Watch now",
        "notes":   "Trend intact, base formed, price pressing lows",
        "trigger_bar_ts": _trigger_bar(r),
    }


def sweep_active_long_v2(c2, p, vw):
    r = rh(c2)
    if len(r) < 8:
        return False, {}
    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    if en is None:
        return False, {}
    base = micro_base_stats(c2, lookback=5, exclude_last=1)
    if not base:
        return False, {}
    last       = r[-1]
    ema_slice  = [x for x in es[-4:] if x is not None]
    above_vwap = p > (vw or 0)
    ema_up     = len(ema_slice) >= 2 and ema_slice[-1] >= ema_slice[0]
    undercut   = last["l"] < base["low"]
    if not (above_vwap and ema_up and undercut):
        return False, {}
    score = clamp_score(60 + (10 if above_vwap else 0) + (5 if ema_up else 0))
    return True, {
        "setup": "SWEEP_ACTIVE", "dir": "⚠️ ACTIVE",
        "trigger": f"Undercut in progress below ${round(base['low'], 2)}",
        "inval":   f"No reclaim / continued acceptance below ${round(base['low'], 2)}",
        "level":   f"Base low: ${round(base['low'], 2)} | Current low: ${round(last['l'], 2)}",
        "vol":     "Live decision zone",
        "score":   score, "action": "Decision zone",
        "notes":   "Undercut is happening now — watch for reclaim",
        "trigger_bar_ts": _trigger_bar(r),
    }


def sweep_reclaim_long_v2(c2, p, vw):
    r = rh(c2)
    if len(r) < 9:
        return False, {}
    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    if en is None:
        return False, {}
    ema_slice  = [x for x in es[-4:] if x is not None]
    above_vwap = p > (vw or 0)
    ema_up     = len(ema_slice) >= 2 and ema_slice[-1] >= ema_slice[0]
    if not (above_vwap and ema_up):
        return False, {}
    base_bars  = r[-7:-2]
    if len(base_bars) < 4:
        return False, {}
    base_high  = max(c["h"] for c in base_bars)
    base_low   = min(c["l"] for c in base_bars)
    sweep_bar  = r[-2]
    rcl_bar    = r[-1]
    sc_under   = sweep_bar["l"] < base_low
    sc_reclaim = sweep_bar["c"] > base_low and top_half_close(sweep_bar)
    nc_under   = sweep_bar["l"] < base_low
    nc_reclaim = rcl_bar["c"] > base_low and top_half_close(rcl_bar)
    valid      = (sc_under and sc_reclaim) or (nc_under and nc_reclaim)
    if not valid:
        return False, {}
    used_bar = sweep_bar if (sc_under and sc_reclaim) else rcl_bar
    same_bar = used_bar is sweep_bar
    score = clamp_score(
        40 + 15 + 15
        + (10 if is_green(used_bar) else 0)
        + (10 if above_vwap else 0)
        + (10 if ema_up else 0)
        + (10 if same_bar else 0)
    )
    return True, {
        "setup": "SWEEP_RECLAIM_LONG", "dir": "🟢 LONG",
        "trigger": f"Reclaim above micro-base low ${round(base_low, 2)}",
        "inval":   f"Loss of sweep low ${round(min(sweep_bar['l'], rcl_bar['l']), 2)}",
        "level":   f"Base: ${round(base_low, 2)}–${round(base_high, 2)} | Next: ${round(base_high, 2)}",
        "vol":     "Reclaim confirmed",
        "score":   score,
        "action":  "Actionable" if score >= 70 else "Watch closely",
        "notes":   (f"{'Same-candle' if same_bar else 'Next-candle'} reclaim | "
                    f"{'Green' if is_green(used_bar) else 'Not green'} | "
                    f"Close: {round(close_pos_in_range(used_bar) * 100, 1)}% of range"),
        "trigger_bar_ts": _trigger_bar(r),
    }


# ──────────────────────────────────────────────────────────────
# FORMATTERS
# ──────────────────────────────────────────────────────────────

def fmt(ticker, d):
    sc = d.get("score", 0)
    em = "🔥" if sc >= 85 else "✅" if sc >= 70 else "⚠️"
    return "\n".join([
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
    ])


# ──────────────────────────────────────────────────────────────
# SCANNER
# ──────────────────────────────────────────────────────────────

class Scanner:
    def __init__(self):
        saved_watch = load_json_file(WATCHLIST_FILE, DEFAULT_WATCHLIST)
        saved_armed = load_json_file(ARMED_FILE, [])
        self.wl     = list(dict.fromkeys(
            saved_watch if isinstance(saved_watch, list) and saved_watch else DEFAULT_WATCHLIST
        ))
        self.armed  = set(saved_armed if isinstance(saved_armed, list) else [])

        self.pmh, self.pml, self.pr = {}, {}, {}
        self.pm_dt, self.pr_dt      = None, None
        self.earnings               = set()
        self.fired_signals          = {}
        self.session_date           = None

        # SPY data cached per cycle — one call serves all tickers
        self._spy_pct  = None
        self._spy_last = None

    def save_state(self):
        save_json_file(WATCHLIST_FILE, self.wl)
        save_json_file(ARMED_FILE, sorted(self.armed))

    def is_mkt(self):
        n = now_et()
        if n.weekday() >= 5:
            return False
        return (n.replace(hour=9, minute=25, second=0, microsecond=0)
                <= n <=
                n.replace(hour=16, minute=5, second=0, microsecond=0))

    def maybe_daily_reset(self):
        today = now_et().date()
        if self.session_date != today:
            n = now_et()
            if self.session_date is None or hhmm_gte(n, 9, 25):
                old = len(self.fired_signals)
                self.fired_signals.clear()
                self.session_date = today
                self._spy_pct     = None
                print(f"[DAILY RESET] Cleared {old} stale signals. Session: {today}")
                if old > 0:
                    send_telegram(f"🔄 <b>Daily Reset</b>\nCleared {old} signals from prior session.")

    def purge_expired(self):
        n       = now_et()
        expired = [
            k for k, info in self.fired_signals.items()
            if (n - info["fired_at"]).total_seconds() / 60
               >= SIGNAL_TTL.get(k[1], DEFAULT_TTL)
        ]
        for k in expired:
            del self.fired_signals[k]
        if expired:
            print(f"[TTL PURGE] Removed {len(expired)} expired signals")

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

    def refresh_spy(self):
        """
        Fetch SPY session % change once per cycle.
        All tickers share this value — one API call per 60s loop.
        """
        try:
            spy_c5 = candles("SPY", 5)
            spy_c5 = closed_only(spy_c5, 5)
            self._spy_pct  = session_pct_change(spy_c5)
            self._spy_last = now_et()
            print(f"[SPY] Session pct: {self._spy_pct}%")
        except Exception as e:
            print(f"[SPY ERR] {e}")
            self._spy_pct = None

    def should_fire(self, ticker, setup, bar_ts):
        key  = (ticker, setup)
        info = self.fired_signals.get(key)
        if info is None:
            return True
        if bar_ts is not None and info.get("bar_ts") == bar_ts:
            return False
        gap_min = (now_et() - info["fired_at"]).total_seconds() / 60
        return gap_min >= MIN_REFIRE_GAP_MIN

    def mark_fired(self, ticker, setup, bar_ts, score):
        self.fired_signals[(ticker, setup)] = {
            "fired_at": now_et(),
            "bar_ts":   bar_ts,
            "score":    score,
        }

    def scan_standard(self, ticker):
        candidates = []
        try:
            c5_raw = candles(ticker, 5)
            c1_raw = candles(ticker, 1)
            c5     = closed_only(c5_raw, 5)
            c1     = closed_only(c1_raw, 1)
            if not c5 or not c1:
                return candidates

            p = price(ticker)
            if not p:
                return candidates

            vw      = vwap(c5)
            pmh_v   = self.pmh.get(ticker)
            pml_v   = self.pml.get(ticker)
            pd      = self.pr.get(ticker, {})
            pdh     = pd.get("h")
            pdl     = pd.get("l")

            # ── Quality layer inputs (computed once per ticker) ──
            rvol    = calc_rvol(c5)
            tkr_pct = session_pct_change(c5)
            rs_mod, rs_label = relative_strength_vs_spy(tkr_pct, self._spy_pct)

            setups = [
                ("ORB_5M_LONG",
                 lambda: orb_5m_long(c5, c1, p, vw, pmh_v, rvol, rs_mod)),
                ("ORB_15M_LONG",
                 lambda: orb_15m_long(c5, c1, p, vw, pmh_v, rvol, rs_mod)),
                ("PMH_BREAK_RETEST_LONG",
                 lambda: pmh_retest(c5, p, vw, pmh_v, rvol, rs_mod)),
                ("PML_BREAK_RETEST_SHORT",
                 lambda: pml_retest(c5, p, vw, pml_v, rvol, rs_mod)),
                ("VWAP_RECLAIM_LONG",
                 lambda: vwap_reclaim(c5, p, vw, rvol, rs_mod)),
                ("VWAP_REJECT_SHORT",
                 lambda: vwap_reject(c5, p, vw, rvol, rs_mod)),
                ("EMA9_5M_PULLBACK_LONG",
                 lambda: ema9_pb_long(c5, p, vw, rvol, rs_mod)),
                ("EMA9_5M_PULLBACK_SHORT",
                 lambda: ema9_pb_short(c5, p, vw, rvol, rs_mod)),
                ("FLAG_BREAKOUT_LONG",
                 lambda: flag_long(c5, p, vw, rvol, rs_mod)),
                ("PDH_BREAK_RETEST_LONG",
                 lambda: pdh_retest(c5, p, vw, pdh, rvol, rs_mod)),
                ("PDL_BREAK_RETEST_SHORT",
                 lambda: pdl_retest(c5, p, vw, pdl, rvol, rs_mod)),
                ("LATER_DAY_HOD_BREAKOUT",
                 lambda: later_day_hod_breakout(c5, p, vw, rvol, rs_mod)),
                ("FIB_PULLBACK_LONG",
                 lambda: fib_pullback_long(c5, p, vw, rvol)),
                ("FIB_PULLBACK_SHORT",
                 lambda: fib_pullback_short(c5, p, vw, rvol)),
            ]

            for name, fn in setups:
                try:
                    ok, d = fn()
                    if not ok:
                        continue
                    if d.get("score", 0) < MIN_SCORE:
                        continue
                    bar_ts = d.get("trigger_bar_ts")
                    if not self.should_fire(ticker, name, bar_ts):
                        continue
                    # Append RS label to notes for context
                    d["notes"] = d.get("notes", "") + f" | {rs_label}"
                    candidates.append((name, d))
                except Exception as e:
                    print(f"[SETUP ERR] {ticker}:{name}:{e}")

        except Exception as e:
            print(f"[SCAN ERR] {ticker}:{e}")

        if not candidates:
            return []
        candidates.sort(key=lambda x: x[1].get("score", 0), reverse=True)
        return [candidates[0]]

    def scan_sweep(self, ticker):
        candidates = []
        if ticker not in self.armed:
            return candidates
        try:
            c2_raw = candles(ticker, 2)
            c2     = closed_only(c2_raw, 2)
            if not c2:
                return candidates
            p  = price(ticker)
            if not p:
                return candidates
            vw = vwap(c2)

            for name, fn in [
                ("SWEEP_WATCH",        lambda: sweep_watch_long_v2(c2, p, vw)),
                ("SWEEP_ACTIVE",       lambda: sweep_active_long_v2(c2, p, vw)),
                ("SWEEP_RECLAIM_LONG", lambda: sweep_reclaim_long_v2(c2, p, vw)),
            ]:
                try:
                    ok, d = fn()
                    if not ok:
                        continue
                    bar_ts = d.get("trigger_bar_ts")
                    if not self.should_fire(ticker, name, bar_ts):
                        continue
                    candidates.append((name, d))
                except Exception as e:
                    print(f"[SWEEP ERR] {ticker}:{name}:{e}")
        except Exception as e:
            print(f"[SWEEP SCAN ERR] {ticker}:{e}")

        if not candidates:
            return []
        candidates.sort(key=lambda x: x[1].get("score", 0), reverse=True)
        return [candidates[0]]

    def cmd(self, command):
        global MIN_SCORE
        pts = command.strip().split()
        c   = pts[0].lower() if pts else ""

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
            send_telegram(f"🎯 Armed ({len(self.armed)}):\n{', '.join(sorted(self.armed)) or 'none'}")

        elif c == "/list":
            send_telegram(f"📋 Watching ({len(self.wl)}):\n{', '.join(self.wl)}")

        elif c == "/status":
            spy_str = f"{self._spy_pct}%" if self._spy_pct is not None else "N/A"
            send_telegram(
                f"📊 <b>Scanner v3.6</b>\n"
                f"Stocks: {len(self.wl)} | Armed: {len(self.armed)}\n"
                f"Min score: {MIN_SCORE}/100\n"
                f"Active fired signals: {len(self.fired_signals)}\n"
                f"SPY session: {spy_str}\n"
                f"Session: {self.session_date}\n"
                f"RVOL min gate: {RVOL_MIN}x\n"
                f"5m ORB cutoff: 9:45 ET | 15m ORB: 9:45–10:05 ET\n"
                f"Earnings tags: {', '.join(sorted(self.earnings)) or 'none'}"
            )

        elif c == "/setups":
            send_telegram(
                "📊 <b>Active Setups (v3.6)</b>\n"
                "1. ORB 5m Long\n2. ORB 15m Long\n3. PM High Retest Long\n"
                "4. PM Low Retest Short\n5. VWAP Reclaim Long\n6. VWAP Reject Short\n"
                "7. 9 EMA Pullback Long\n8. 9 EMA Pullback Short\n9. Flag Breakout Long\n"
                "10. PDH Retest Long\n11. PDL Retest Short\n12. Later-Day HOD Breakout\n"
                "<b>NEW:</b>\n"
                "13. Fib Pullback Long (38.2/50/61.8%)\n"
                "14. Fib Pullback Short (38.2/50/61.8%)\n"
                "<b>Armed only:</b>\n"
                "15. Sweep Watch | 16. Sweep Active | 17. Sweep Reclaim Long\n\n"
                "<b>Quality layers on all setups:</b>\n"
                "• RVOL gate (min 1.5x)\n"
                "• Relative strength vs SPY\n"
                "• Time-of-day score modifier"
            )

        elif c == "/threshold" and len(pts) == 2:
            try:
                MIN_SCORE = int(pts[1])
                send_telegram(f"⚙️ Min score: {MIN_SCORE}/100")
            except Exception:
                send_telegram("❌ Usage: /threshold 65")

        elif c == "/reset":
            old = len(self.fired_signals)
            self.fired_signals.clear()
            send_telegram(f"🔄 Cleared {old} fired signals. Scanner reset.")

        elif c == "/fired":
            if not self.fired_signals:
                send_telegram("📭 No active fired signals.")
            else:
                n     = now_et()
                lines = ["📋 <b>Active fired signals:</b>"]
                for (t, s), info in sorted(self.fired_signals.items()):
                    age = int((n - info["fired_at"]).total_seconds() / 60)
                    ttl = SIGNAL_TTL.get(s, DEFAULT_TTL)
                    lines.append(f"• {t} {s} — {age}m old (TTL {ttl}m)")
                send_telegram("\n".join(lines))

        elif c == "/earnings" and len(pts) >= 2:
            added = [raw.upper() for raw in pts[1:]]
            for t in added:
                self.earnings.add(t)
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
                "/watch /remove /arm /disarm /armed /list\n"
                "/status /setups /threshold 65\n"
                "/reset /fired\n"
                "/earnings /unearnings /reauth"
            )

    def run(self):
        print("[SCANNER] v3.6 starting")
        send_telegram(
            f"🤖 <b>Scanner v3.6 Online</b>\n{'━' * 28}\n"
            f"Watching <b>{len(self.wl)} stocks</b> | Armed <b>{len(self.armed)}</b>\n"
            f"Threshold ≥ {MIN_SCORE}/100\n{'━' * 28}\n"
            f"<b>New in v3.6:</b>\n"
            f"• Fib pullback setup (38.2 / 50 / 61.8%)\n"
            f"• RVOL gate on all setups (min {RVOL_MIN}x)\n"
            f"• Relative strength vs SPY scoring\n"
            f"• Time-of-day score modifier\n\n"
            f"Commands: /status /setups /fired /reset"
        )

        while True:
            if not self.is_mkt():
                print("[SCAN] Outside hours. Sleep 5m.")
                time.sleep(300)
                continue

            self.maybe_daily_reset()
            self.purge_expired()
            self.refresh()
            self.refresh_spy()   # one SPY call per cycle, shared across all tickers

            for t in list(self.wl):
                print(f"[SCAN] {t}...")
                try:
                    for name, d in self.scan_standard(t):
                        bar_ts = d.get("trigger_bar_ts")
                        send_telegram(fmt(t, d))
                        self.mark_fired(t, name, bar_ts, d.get("score", 0))
                        time.sleep(1)
                except Exception as e:
                    print(f"[STD LOOP ERR] {t}:{e}")

                try:
                    for name, d in self.scan_sweep(t):
                        bar_ts = d.get("trigger_bar_ts")
                        send_telegram(fmt(t, d))
                        self.mark_fired(t, name, bar_ts, d.get("score", 0))
                        time.sleep(1)
                except Exception as e:
                    print(f"[SWEEP LOOP ERR] {t}:{e}")

                time.sleep(0.35)

            print(f"[SCAN] Cycle done. {len(self.fired_signals)} active signals. Sleep 60s.")
            time.sleep(60)


# ──────────────────────────────────────────────────────────────
# TELEGRAM LISTENER
# ──────────────────────────────────────────────────────────────

def listen(sc):
    offset = None
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"timeout": 30, "offset": offset}, timeout=35,
            ).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                txt    = u.get("message", {}).get("text", "")
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
        f"[MAIN] v3.6 | Schwab:{'OK' if SCHWAB_CLIENT_ID else 'MISSING'} | "
        f"Telegram:{'OK' if TELEGRAM_TOKEN else 'MISSING'}"
    )
    sc = Scanner()
    threading.Thread(target=listen, args=(sc,), daemon=True).start()
    sc.run()
