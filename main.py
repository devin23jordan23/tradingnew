"""
main.py — Andre's Trading Scanner v3.12

CHANGES FROM v3.6
─────────────────────────────────────────────────────────────────

1. EMA DIRECTION CONTEXT — "FROM ABOVE vs FROM BELOW" GATE
   Root bug: EMA9 pullback longs were firing when price had been
   BELOW the EMA for 10-15+ minutes and then just touched it from
   underneath — a resistance test, not a support pullback. Opposite
   setups were conflated.

   Fix: Every EMA setup now classifies the prior price position:
   - PULLBACK LONG: price must have been ABOVE the EMA for the last
     N bars before the touch. Minimum 3 bars above, no more than
     2 bars below allowed before the touch.
   - PULLBACK SHORT: price must have been BELOW the EMA for the last
     N bars before the bounce. Same rule inverted.
   - "Bars above/below EMA" is now reported in alert notes.

2. ATR-NORMALIZED RELATIVE STRENGTH
   Root bug: raw % diff vs SPY missed GOOGL-type setups where a
   stock is up 0.8% when SPY is up 0.5% — looks neutral (+0.3%)
   but GOOGL's daily ATR is ~1.5% so 0.8% is actually 53% of its
   range — genuinely strong for that name.

   Fix: RS now uses ATR-normalized moves:
     ticker_rs = ticker_pct / ticker_atr_pct
     spy_rs    = spy_pct    / spy_atr_pct
     diff      = ticker_rs  - spy_rs
   A stock moving 70% of its ATR when SPY moves 40% of its ATR
   is clearly outperforming on a normalized basis regardless of
   whether the raw % numbers look close.
   SPY ATR cached once per cycle alongside SPY pct.

3. VOLUME BASELINE FIX — SESSION ANCHOR NOT ROLLING
   Root bug: av() computed rolling average over last 20 bars.
   A stock ranging for 90 min suppresses its own vol average,
   making flat current volume look "above average." Result: rangebound
   names touching an EMA triggered volume-confirmed alerts.

   Fix: vol_baseline() computes average from the FIRST N bars of the
   session (opening pace), not the rolling last 20. Current volume
   is compared to what the stock was doing at the open, not what
   it's been doing during the chop.

   av() still exists for non-volume-gate uses (flag range, etc.)
   vol_baseline() replaces it in all setup volume checks.

4. EMA RIDER SETUP — CATCHING RUNNERS ON 10-MIN 4 EMA
   New setup: EMA4_10M_RIDER_LONG / SHORT.
   Catches stocks in a strong directional trend by watching the
   4 EMA on the 10-minute chart. Requires:
   - 4 EMA on 10-min is clearly sloped (trending, not flat)
   - Price is riding ABOVE (long) or BELOW (short) the 4 EMA
   - Price pulls back to touch or come within 0.3% of the 4 EMA
   - Volume on pullback bars is contracting (healthy, not distributing)
   - At least 4 consecutive bars have held the 4 EMA side (trend confirmed)
   - RVOL > 1.5x (name must be in play, not just any slow grind)
   This catches your runner scenario: stock that's been trending
   for 30-60 min and pulls back gently to the 4 EMA before continuation.
   Alert says: "Riding 4 EMA on 10-min — pullback to touch."

5. EMA9 ON 5-MIN AND 10-MIN — MULTI-TIMEFRAME
   Added ema9_pb_long_10m / ema9_pb_short_10m: same logic as the
   5-min version but evaluated on 10-min closed bars.
   10-min EMA9 pullbacks are higher-quality setups (more confirmation,
   less noise) and score 5 pts higher base. They require more bars
   above/below before the touch (5 bars vs 3 bars on 5-min).

   The 2-min EMA setup is intentionally NOT added as a standalone
   alert — too many false signals as you flagged. The 2-min is used
   internally in the sweep logic only (already there).

6. /REMOVE BUG FIXED
   Root bug: self.wl comparison used direct string match which is
   case-sensitive. If the ticker was stored with different casing
   or had trailing whitespace from Telegram input, the match failed
   silently. Now normalizes all inputs and the list comprehension
   uses explicit .upper() comparison throughout.

7. trigger_bar_ts NONE GUARD
   Root bug: some setups returned trigger_bar_ts=None (e.g. when
   r[-1] didn't exist or candles were empty). The should_fire()
   check was then comparing None == None = True, blocking future
   valid signals on that ticker/setup pair.
   Fix: if trigger_bar_ts is None, dedup skips the bar comparison
   and falls through to time-gap check only. Also added explicit
   guard in every setup function.

8. RS LABEL IN ALERT ALWAYS VISIBLE
   Previously the RS label was appended to notes which got truncated.
   Now it has its own dedicated line in the alert format.
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

SCHWAB_CLIENT_ID     = os.environ.get("SCHWAB_CLIENT_ID", "")
SCHWAB_CLIENT_SECRET = os.environ.get("SCHWAB_CLIENT_SECRET", "")
TELEGRAM_TOKEN       = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL  = os.environ.get("DISCORD_WEBHOOK_URL", "")  # optional — leave blank to disable

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
COOLDOWN  = 15

# Timing windows
ORB_5M_CUTOFF  = (9, 45)
ORB_15M_START  = (9, 45)
ORB_15M_CUTOFF = (10, 5)
HOD_START      = (10, 15)
HOD_END        = (15, 30)

# Sweep cooldowns
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
    "EMA9_10M_PULLBACK_LONG":   18,
    "EMA9_10M_PULLBACK_SHORT":  18,
    "EMA4_10M_RIDER_LONG":      20,
    "EMA4_10M_RIDER_SHORT":     20,
    "FLAG_BREAKOUT_LONG":       18,
    "PDH_BREAK_RETEST_LONG":    30,
    "PDL_BREAK_RETEST_SHORT":   30,
    "LATER_DAY_HOD_BREAKOUT":   25,
    "OPENING_DRIVE_LONG":       30,   # fires early, stays valid through first hour
    "FIB_PULLBACK_LONG":        20,
    "FIB_PULLBACK_SHORT":       20,
    "SWEEP_WATCH":              10,
    "SWEEP_ACTIVE":              5,
    "SWEEP_RECLAIM_LONG":       15,
}
DEFAULT_TTL = 15

MIN_REFIRE_GAP_MIN = 30  # raised from 20 — a setup that fired 20min ago on same conditions is stale

# Quality layer constants
RVOL_MIN       = 1.5
RVOL_STRONG    = 2.0
FIB_LEVELS     = [0.382, 0.50, 0.618]
FIB_TOLERANCE  = 0.004
FIB_CONFLUENCE = 0.005
FIB_MIN_ATR_MULT = 1.5
FIB_MIN_BARS   = 3

# EMA direction context — bars price must have spent on correct side
# before a touch is considered a valid pullback (not a resistance test)
# TIGHTENED: EMA_MAX_CROSS_BARS reduced from 2 to 0 — zero tolerance.
# If price spent ANY bar on the wrong side of the EMA in the check window,
# it is NOT a clean trend pullback. It is a chop zone or a resistance test.
# This is the core fix for "long alerts when price is approaching from below."
EMA_PRIOR_BARS_5M  = 4   # need 4 consecutive bars above EMA before touch (5-min)
EMA_PRIOR_BARS_10M = 5   # need 5 bars above EMA before touch (10-min)
EMA_MAX_CROSS_BARS = 0   # ZERO cross bars allowed — clean side only

# Volume baseline window — use first N session bars as reference pace
VOL_BASELINE_BARS = 8    # first 8 bars = first 40 min of session on 5-min chart

# 10-min 4 EMA rider thresholds
EMA4_SLOPE_MIN    = 0.0003  # minimum per-bar slope as fraction of price (0.03%)
EMA4_TOUCH_PCT    = 0.003   # within 0.3% of 4 EMA counts as "touching"
EMA4_MIN_BARS     = 4       # at least 4 bars trending on correct side of 4 EMA

WATCHLIST_FILE = "watchlist_state.json"
ARMED_FILE     = "armed_state.json"

# API health tracking — detect when Schwab feed goes silent
_api_consecutive_failures = 0
_api_failure_alerted      = False
API_FAILURE_THRESHOLD     = 3   # 3 consecutive full-cycle failures before alert

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


def _strip_html(msg):
    """
    Discord doesn't render Telegram HTML tags.
    Strip <b> bold tags and convert to Discord markdown (**bold**).
    Also strip any other HTML so the message reads cleanly.
    """
    import re
    msg = re.sub(r"<b>(.*?)</b>", r"**\1**", msg)
    msg = re.sub(r"<[^>]+>", "", msg)
    return msg


def send_discord(msg):
    """
    Forward alert to Discord via webhook.
    Silently skips if DISCORD_WEBHOOK_URL is not set.
    Discord has a 2000 char limit — truncates gracefully if needed.
    """
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        clean = _strip_html(msg)
        if len(clean) > 1950:
            clean = clean[:1950] + "\n…"
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": clean, "username": "Andre Scanner"},
            timeout=10,
        ).raise_for_status()
    except Exception as e:
        print(f"[DISCORD ERR] {e}")


def send_alert(msg):
    """
    Send to both Telegram and Discord in one call.
    Use this everywhere instead of calling send_telegram directly.
    """
    send_telegram(msg)
    send_discord(msg)


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
    """
    Refresh the access token using the refresh token.
    Schwab refresh tokens expire after 7 days — if the refresh itself
    fails with a 4xx, the refresh token is dead and we need a full
    re-auth. Catch that specifically and trigger _login() instead of
    crashing silently, which was causing the daily auth drop.
    """
    headers = {"Authorization": f"Basic {_b64()}", "Content-Type": "application/x-www-form-urlencoded"}
    try:
        r = requests.post(TOKEN_URL, headers=headers,
                          data={"grant_type": "refresh_token", "refresh_token": t.get("refresh_token", "")},
                          timeout=15)
        # 4xx on refresh = refresh token is expired or invalid — need full re-auth
        if r.status_code in (400, 401, 403):
            print(f"[AUTH] Refresh token expired ({r.status_code}). Triggering re-auth.")
            send_telegram(
                "🔐 <b>Schwab session expired</b>\n"
                "Refresh token is no longer valid (7-day limit).\n"
                "Send /reauth to reconnect."
            )
            return {}   # empty dict — tok() will trigger _login() on next call
        r.raise_for_status()
        new_t = r.json()
        if "refresh_token" not in new_t:
            new_t["refresh_token"] = t.get("refresh_token")
        _save_tokens(new_t)
        print(f"[AUTH] Tokens refreshed successfully.")
        return new_t
    except requests.exceptions.HTTPError as e:
        print(f"[AUTH REFRESH ERR] {e}")
        return {}
    except Exception as e:
        print(f"[AUTH REFRESH ERR] {e}")
        return t   # return old tokens on network error — don't wipe them


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
        print("[AUTH] Tokens saved.")
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
        # If refresh returned empty (refresh token expired), trigger full login
        if not t or not t.get("access_token"):
            t = _login()
            if not t:
                return ""
    return t.get("access_token", "")


def _hdr():
    return {"Authorization": f"Bearer {tok()}", "Accept": "application/json"}


def _get(ep, params=None):
    global _api_consecutive_failures, _api_failure_alerted
    for i in range(2):
        try:
            r = requests.get(f"{BASE}{ep}", headers=_hdr(), params=params or {}, timeout=10)
            if r.status_code == 401 and i == 0:
                _refresh_tokens(_load_tokens())
                continue
            r.raise_for_status()
            data = r.json()
            # Successful call — reset failure counter
            if _api_consecutive_failures > 0:
                _api_consecutive_failures = 0
                _api_failure_alerted      = False
                print("[API] Feed recovered.")
            return data
        except Exception as e:
            print(f"[DATA ERR] {ep}: {e}")
            if i == 0:
                time.sleep(2)
    # Both retries exhausted — increment failure counter
    _api_consecutive_failures += 1
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
    """Rolling average volume — used for non-gate purposes (flag range etc.)"""
    r = rh(cs)
    vols = [c["v"] for c in r[-n:]]
    return sum(vols) / len(vols) if vols else None


def vol_baseline(cs, n=VOL_BASELINE_BARS):
    """
    Session-anchored volume baseline.
    Uses the FIRST n bars of the session as the reference pace,
    not the rolling recent average. This prevents rangebound names
    from suppressing their own average and then triggering on flat volume.

    Returns (baseline_avg, recent_avg, ratio):
      ratio > 1.0 = current pace is faster than opening pace
      ratio < 0.8 = current pace is contracting vs opening
    """
    r = rh(cs)
    if len(r) < n + 2:
        return None, None, None
    base   = r[:n]
    recent = r[-3:]    # last 3 bars = current pace
    base_avg   = sum(c["v"] for c in base)   / len(base)
    recent_avg = sum(c["v"] for c in recent) / len(recent)
    if base_avg == 0:
        return None, None, None
    return base_avg, recent_avg, round(recent_avg / base_avg, 2)


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
# QUALITY LAYER HELPERS
# ──────────────────────────────────────────────────────────────

def gap_pct(c5, prior_close):
    """
    Gap percentage from prior close to today's open.
    Uses the first regular-hours bar's open price.
    Returns None if data unavailable.
    """
    r = rh(c5)
    if not r or not prior_close or prior_close == 0:
        return None
    today_open = r[0]["o"]
    return round((today_open - prior_close) / prior_close * 100, 2)


def calc_rvol(c5, baseline_bars=10, prior_close=None):
    """
    Session-anchored RVOL.

    GAP DAY FIX: On gap-up days (open > 2% above prior close), the first
    bars of the session ARE the volume spike — using them as baseline means
    AAOI's extraordinary volume reads as ~1.0 all day because every bar
    compares against the spike baseline. On gap days, use prior day's
    average bar volume as the reference instead of today's opening bars.

    Normal days: compare recent 5 bars to first 10 bars of today.
    Gap days: compare recent 5 bars to prior day avg bar volume.
    """
    r = rh(c5)
    if len(r) < 4:
        return None

    recent = r[-5:] if len(r) >= 5 else r
    recent_avg = sum(c["v"] for c in recent) / len(recent)

    # Detect gap day
    is_gap_day = False
    if prior_close and prior_close > 0 and r:
        today_open = r[0]["o"]
        gap = abs(today_open - prior_close) / prior_close * 100
        is_gap_day = gap >= 2.0

    if is_gap_day and prior_close:
        # Use prior day volume as baseline — prior_close is from prior_day()
        # We don't have prior day bar volume directly, so use session total
        # divided by typical bars (78 bars in a 6.5hr session on 5-min)
        # as the per-bar baseline. This is approximate but far more accurate
        # than using today's spike bars.
        # Better: use all of today's bars if we have enough (>15 bars = >75 min)
        if len(r) >= 15:
            # Use bars 11-onward as baseline (past the initial surge window)
            settling_bars = r[10:]
            base_avg = sum(c["v"] for c in settling_bars) / len(settling_bars)
        else:
            # Too early — can't normalize yet, return None to avoid false reads
            return None
    else:
        # Normal day — use first baseline_bars as reference
        if len(r) < baseline_bars + 2:
            base = r[:max(3, len(r) // 2)]
        else:
            base = r[:baseline_bars]
        if not base:
            return None
        base_avg = sum(c["v"] for c in base) / len(base)

    if base_avg == 0:
        return None
    return round(recent_avg / base_avg, 2)


def atr(cs, n=10):
    r = rh(cs)
    if len(r) < 2:
        return None
    trs = []
    for i in range(1, len(r)):
        prev_c = r[i - 1]["c"]
        trs.append(max(r[i]["h"] - r[i]["l"],
                       abs(r[i]["h"] - prev_c),
                       abs(r[i]["l"] - prev_c)))
    recent = trs[-n:]
    return sum(recent) / len(recent) if recent else None


def time_of_day_modifier():
    ts = now_et()
    if in_window(ts, 9, 30, 10, 30):
        return +5
    elif in_window(ts, 10, 30, 12, 0):
        return 0
    elif in_window(ts, 12, 0, 14, 0):
        return -8
    else:
        return +3


def session_pct_change(c5):
    r = rh(c5)
    if not r:
        return None
    open_price = r[0]["o"]
    current    = r[-1]["c"]
    if not open_price:
        return None
    return round((current - open_price) / open_price * 100, 2)


def atr_pct(cs, n=10):
    """ATR as a percentage of current price — for normalization."""
    r = rh(cs)
    if not r:
        return None
    atr_val = atr(cs, n)
    p_val   = r[-1]["c"]
    if not atr_val or not p_val:
        return None
    return round(atr_val / p_val * 100, 3)


def recent_direction(cs, lookback=4):
    """
    Classify the most recent directional trend of a candle series.
    Uses the last `lookback` closed bars to determine:
      "up"    — net move is clearly positive (> +0.05% of price)
      "down"  — net move is clearly negative (< -0.05% of price)
      "flat"  — oscillating within noise band
    This answers "what is SPY/ticker doing RIGHT NOW" vs session pct
    which can be stale from an early-session move hours ago.
    """
    r = rh(cs)
    if len(r) < lookback + 1:
        return "flat"
    window = r[-(lookback + 1):]
    start  = window[0]["c"]
    end    = window[-1]["c"]
    if not start:
        return "flat"
    move_pct = (end - start) / start * 100
    if move_pct >= 0.08:
        return "up"
    elif move_pct <= -0.08:
        return "down"
    return "flat"


def build_spy_context(spy_c5):
    """
    Build a full SPY context object from candle data.
    Called once per scan cycle, results shared across all tickers.
    Returns a dict with session_pct, atr_pct_val, and recent_dir.
    """
    return {
        "session_pct": session_pct_change(spy_c5),
        "atr_pct":     atr_pct(spy_c5),
        "recent_dir":  recent_direction(spy_c5, lookback=4),
    }


def grade_rs(ticker_pct, ticker_atr_pct, ticker_recent_dir, spy_ctx, direction="long"):
    """
    Full direction-aware relative strength grading system.

    Three-layer scoring (applied in order, highest tier wins):

    LAYER 1 — Directional Divergence (most powerful signal)
    ─────────────────────────────────────────────────────────
    Ticker going UP while SPY going DOWN (or vice versa for shorts)
    = institutional conviction trade. Market is wrong or they know
    something. This is your GOOGL-while-SPY-sells signal.
    Score: +15 flat. Grade: A+ RS.

    LAYER 2 — Same direction, ATR-normalized outperformance
    ─────────────────────────────────────────────────────────
    Both going same way, but ticker using more of its ATR than SPY.
    GOOGL up 0.8% (53% of 1.5% ATR) vs SPY up 0.5% (100% of 0.5% ATR)
    → SPY actually stronger normalized. Pure raw % diff misses this.
    Score: +5 to +10 based on normalized spread.

    LAYER 3 — Directional alignment (weakest positive signal)
    ─────────────────────────────────────────────────────────
    Same direction, similar pace — market is doing most of the work.
    Score: 0 to +3. Setup still valid but RS adds no edge.

    PENALTIES
    ─────────────────────────────────────────────────────────
    Ticker lagging SPY: -5 to -10. Forces score below threshold
    on marginal setups. Strong setups still fire but get noted.

    Parameters:
      ticker_pct        — session % change (open to now)
      ticker_atr_pct    — ticker ATR as % of price
      ticker_recent_dir — "up" / "down" / "flat" (last 4 bars)
      spy_ctx           — dict from build_spy_context()
      direction         — "long" or "short" (inverts RS for shorts)

    Returns: (score_modifier: int, grade_label: str, rs_tier: str)
      rs_tier: "A+" / "A" / "B" / "C" / "D" — used for alert formatting
    """
    spy_pct    = spy_ctx.get("session_pct")
    spy_atr    = spy_ctx.get("atr_pct")
    spy_dir    = spy_ctx.get("recent_dir", "flat")

    if spy_pct is None or ticker_pct is None:
        return 0, "RS: N/A", "?"

    tkr_pct  = ticker_pct
    raw_diff = tkr_pct - spy_pct

    # For shorts, strong RS means the ticker is WEAKER than SPY
    if direction == "short":
        ticker_recent_dir = "down" if ticker_recent_dir == "up" else (
                            "up"   if ticker_recent_dir == "down" else "flat")
        spy_dir_for_short = "down" if spy_dir == "up" else (
                            "up"   if spy_dir == "down" else "flat")
        raw_diff = -raw_diff
        spy_dir  = spy_dir_for_short

    # ─── LAYER 1: Counter-trend / directional divergence ───
    # Ticker going UP, SPY going DOWN (within recent 4 bars)
    counter_trend = (ticker_recent_dir == "up" and spy_dir == "down")
    with_trend_spy_down = (ticker_recent_dir == "up" and spy_dir == "flat" and spy_pct < -0.15)

    if counter_trend or with_trend_spy_down:
        spy_move   = round(spy_pct, 2)
        tkr_move   = round(tkr_pct, 2)
        spy_str    = f"SPY {spy_move:+.2f}%"
        tkr_str    = f"ticker {tkr_move:+.2f}%"
        label = (f"⚡ Counter-trend RS | {tkr_str} vs {spy_str} | "
                 f"SPY {'selling' if spy_dir == 'down' else 'flat/weak'} — institutional")
        return +15, label, "A+"

    # ─── LAYER 2: Same direction, ATR-normalized comparison ───
    if ticker_atr_pct and spy_atr and ticker_atr_pct > 0 and spy_atr > 0:
        ticker_norm = tkr_pct / ticker_atr_pct
        spy_norm    = spy_pct / spy_atr
        norm_diff   = ticker_norm - spy_norm
        raw_str     = f"{raw_diff:+.1f}% raw"
        norm_str    = f"{norm_diff:+.2f}x norm"

        if norm_diff >= 0.5:
            return +10, f"RS: Strong | {raw_str} | {norm_str} ✅", "A"
        elif norm_diff >= 0.15:
            return +5,  f"RS: Good | {raw_str} | {norm_str}", "B"
        elif norm_diff >= -0.15:
            return 0,   f"RS: Neutral | {raw_str} | {norm_str}", "B"
        elif norm_diff >= -0.5:
            return -5,  f"RS: Weak | {raw_str} | {norm_str} ⚠️", "C"
        else:
            return -12, f"RS: Lagging | {raw_str} | {norm_str} ❌", "D"

    # ─── LAYER 3: Fallback — raw % diff only ───
    if raw_diff >= 1.0:
        return +8,  f"RS: Strong +{round(raw_diff,1)}% vs SPY ✅", "A"
    elif raw_diff >= 0.3:
        return +4,  f"RS: Good +{round(raw_diff,1)}% vs SPY", "B"
    elif raw_diff >= -0.2:
        return 0,   f"RS: Neutral {round(raw_diff,1)}% vs SPY", "B"
    elif raw_diff >= -0.8:
        return -5,  f"RS: Weak {round(raw_diff,1)}% vs SPY ⚠️", "C"
    else:
        return -10, f"RS: Lagging {round(raw_diff,1)}% vs SPY ❌", "D"


# ──────────────────────────────────────────────────────────────
# EMA DIRECTION CONTEXT HELPERS
# ──────────────────────────────────────────────────────────────

def ema_position_context(r, es, prior_bars_required, max_cross_bars=EMA_MAX_CROSS_BARS):
    """
    Classify whether price approached the EMA from ABOVE or BELOW.

    TIGHTENED in v3.12:
    - Looks back 8 bars total (was prior_bars + max_cross which was only 4-5)
    - Zero cross tolerance (EMA_MAX_CROSS_BARS = 0) — any bar on the wrong
      side in the 8-bar window = MIXED, not a clean pullback
    - This directly kills the "approaching from below" false long alerts
      because any bar below EMA in the window returns "mixed" which blocks
      the long setup entirely

    The VWAP hard gate in each setup function is the primary blocker.
    This is the secondary/confirmation filter.
    """
    LOOKBACK = 8   # always look back 8 bars regardless of prior_bars_required
    if len(r) < LOOKBACK + 1 or len(es) < LOOKBACK + 1:
        return "insufficient_data", 0, 0

    check_bars = r[-(LOOKBACK + 1):-1]
    check_emas = es[-(LOOKBACK + 1):-1]

    above_count = 0
    below_count = 0
    for bar, ema_val in zip(check_bars, check_emas):
        if ema_val is None:
            continue
        if bar["c"] > ema_val:
            above_count += 1
        else:
            below_count += 1

    total = above_count + below_count
    if total == 0:
        return "insufficient_data", 0, 0

    # Zero cross tolerance — any bar on wrong side = mixed
    if above_count >= prior_bars_required and below_count == 0:
        return "from_above", above_count, below_count
    elif below_count >= prior_bars_required and above_count == 0:
        return "from_below", above_count, below_count
    else:
        return "mixed", above_count, below_count


# ──────────────────────────────────────────────────────────────
# FIB HELPERS (unchanged from v3.6)
# ──────────────────────────────────────────────────────────────

def find_impulse_leg(cs, direction="long"):
    r = rh(cs)
    if len(r) < FIB_MIN_BARS + 2:
        return None
    avg_vol = av(cs)
    atr_val = atr(cs)
    if not atr_val or not avg_vol:
        return None
    best = None
    best_size = 0
    for start in range(len(r) - FIB_MIN_BARS):
        for end in range(start + FIB_MIN_BARS, min(start + 12, len(r))):
            leg = r[start:end + 1]
            if direction == "long":
                leg_low  = min(c["l"] for c in leg)
                leg_high = max(c["h"] for c in leg)
                try:
                    low_idx  = next(i for i, c in enumerate(leg) if c["l"] == leg_low)
                    high_idx = next(i for i, c in enumerate(leg) if c["h"] == leg_high)
                except StopIteration:
                    continue
                if high_idx <= low_idx:
                    continue
            else:
                leg_high = max(c["h"] for c in leg)
                leg_low  = min(c["l"] for c in leg)
                try:
                    high_idx = next(i for i, c in enumerate(leg) if c["h"] == leg_high)
                    low_idx  = next(i for i, c in enumerate(leg) if c["l"] == leg_low)
                except StopIteration:
                    continue
                if low_idx <= high_idx:
                    continue
            size = leg_high - leg_low
            if size < atr_val * FIB_MIN_ATR_MULT:
                continue
            leg_avg_vol = sum(c["v"] for c in leg) / len(leg)
            if leg_avg_vol < avg_vol * 0.8:
                continue
            if size > best_size:
                best_size = size
                best = {
                    "low": leg_low, "high": leg_high, "size": size,
                    "bars": len(leg), "start_ts": leg[0]["ts"], "end_ts": leg[-1]["ts"],
                    "avg_vol": leg_avg_vol,
                }
    return best


def fib_levels_from_leg(leg, direction="long"):
    low, high = leg["low"], leg["high"]
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
    base_high = max(c["h"] for c in base)
    base_low  = min(c["l"] for c in base)
    avg_close = sum(c["c"] for c in base) / len(base)
    width     = base_high - base_low
    tight_pct = (width / avg_close) if avg_close else 999
    return {"bars": base, "high": base_high, "low": base_low,
            "width": width, "tight_pct": tight_pct}


# ──────────────────────────────────────────────────────────────
# SETUP HELPERS
# ──────────────────────────────────────────────────────────────

def _trigger_bar(r):
    """Safe trigger bar — never returns None if r is non-empty."""
    return r[-1]["ts"] if r else None


def _apply_quality_modifiers(score, rvol, rs_mod, rs_tier="?"):
    """
    Apply RVOL, RS, and time-of-day modifiers.

    rs_tier from grade_rs() further gates the score:
    - "A+" counter-trend RS: score gets full +15 from rs_mod
    - "A"  strong RS:        score gets full modifier
    - "B"  neutral:          no extra gate, modifier applied normally
    - "C"  weak:             additional -5 penalty on top of rs_mod
    - "D"  lagging:          additional -8 penalty — marginal setups die here
    - "?"  unknown:          no penalty, just apply rs_mod
    This means a "D" score setup (stock lagging badly) needs to have
    very strong core conditions to survive the combined penalty.
    A "C" setup that otherwise scores 65 will land at ~52 after penalties
    and won't fire. An "A+" counter-trend setup scoring 65 will land at
    ~80+ and fires as a high-confidence alert.
    """
    # RVOL modifier
    if rvol is None:
        score -= 5
    elif rvol >= RVOL_STRONG:
        score += 8
    elif rvol >= RVOL_MIN:
        score += 3
    else:
        score -= 10

    # RS modifier — already includes tier-appropriate value from grade_rs()
    score += rs_mod

    # Additional tier penalty for weak RS (stacks on top of rs_mod)
    if rs_tier == "D":
        score -= 8
    elif rs_tier == "C":
        score -= 5

    # Time of day
    score += time_of_day_modifier()

    return clamp_score(score)


# ──────────────────────────────────────────────────────────────
# CORE SETUPS
# ──────────────────────────────────────────────────────────────

def orb_5m_long(c5, c1, p, vw, pmh_v, rvol, rs_mod, rs_tier="?"):
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
    _, recent_vol, vol_ratio = vol_baseline(c5)
    broke      = p > oh
    above_vwap = p > (vw or 0)
    vol        = vol_ratio is not None and vol_ratio >= 1.3
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


def orb_15m_long(c5, c1, p, vw, pmh_v, rvol, rs_mod, rs_tier="?"):
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
    _, recent_vol, vol_ratio = vol_baseline(c5)
    broke      = p > oh
    above_vwap = p > (vw or 0)
    vol        = vol_ratio is not None and vol_ratio >= 1.2
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


def pmh_retest(c5, p, vw, pmh_v, rvol, rs_mod, rs_tier="?"):
    if not pmh_v:
        return False, {}
    r = rh(c5)
    if len(r) < 3:
        return False, {}
    broke = any(c["h"] > pmh_v for c in r[:-2])
    if not broke:
        return False, {}
    near       = abs(p - pmh_v) / pmh_v <= 0.004
    above      = p >= pmh_v * 0.998
    above_vwap = p > (vw or 0)
    _, _, vol_ratio = vol_baseline(c5)
    light_pb   = vol_ratio is not None and vol_ratio <= 0.85
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


def pml_retest(c5, p, vw, pml_v, rvol, rs_mod, rs_tier="?"):
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
    _, _, vol_ratio = vol_baseline(c5)
    light_bounce = vol_ratio is not None and vol_ratio <= 0.85
    if not (below and below_vwap):
        return False, {}
    score = _apply_quality_modifiers(
        65 + (10 if near else 0) + (10 if light_bounce else 0),
        rvol, -rs_mod
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


def vwap_reclaim(c5, p, vw, rvol, rs_mod, rs_tier="?"):
    if not vw:
        return False, {}
    r = rh(c5)
    if len(r) < 4:
        return False, {}
    last, prev = r[-1], r[-2]
    was_below  = any(c["c"] < vw for c in r[-5:-1])
    if not was_below:
        return False, {}
    strong = last["c"] > vw and last["c"] > last["o"]
    first  = strong and prev["c"] < vw
    if not strong:
        return False, {}
    _, _, vol_ratio = vol_baseline(c5)
    vol = vol_ratio is not None and vol_ratio >= 1.2
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


def vwap_reject(c5, p, vw, rvol, rs_mod, rs_tier="?"):
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
    _, _, vol_ratio = vol_baseline(c5)
    vol = vol_ratio is not None and vol_ratio >= 1.1
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


def ema9_pb_long(c5, p, vw, rvol, rs_mod, rs_tier="?"):
    """
    9 EMA pullback long — 5-minute bars.
    HARD GATES (checked before anything else):
    - Price must be ABOVE VWAP. Below VWAP = no long EMA setup, period.
    - Price must have been ABOVE the EMA for prior bars (direction context).
    Both conditions exist in the scoring logic but were not catching all
    cases. Making them hard exits before any further evaluation.
    """
    r = rh(c5)
    if len(r) < 14:
        return False, {}

    # HARD GATE 1 — must be above VWAP. Non-negotiable for longs.
    if vw and p <= vw:
        return False, {}

    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    if en is None:
        return False, {}

    # HARD GATE 2 — must have approached from above
    ctx, bars_above, bars_below = ema_position_context(r, es, EMA_PRIOR_BARS_5M)
    if ctx != "from_above":
        return False, {}

    ema_vals   = [e for e in es[-5:] if e is not None]
    rising     = len(ema_vals) > 1 and ema_vals[-1] > ema_vals[0]
    last, prev = r[-1], r[-2]
    above_vwap = p > (vw or 0)
    touched    = last["l"] <= en * 1.003 or prev["l"] <= en * 1.003
    bouncing   = last["c"] > prev["h"] or last["c"] > en
    _, _, vol_ratio = vol_baseline(c5)
    light_pb   = vol_ratio is not None and vol_ratio <= 0.80

    if not (rising and touched and bouncing):
        return False, {}

    score = _apply_quality_modifiers(
        65 + (10 if light_pb else 0) + (10 if above_vwap else 0) + (5 if rising else 0),
        rvol, rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "EMA9_5M_PULLBACK_LONG", "dir": "🟢 LONG",
        "trigger": "Bounce after 9 EMA touch (from above)",
        "inval":   f"Loss of 9 EMA ${round(en, 2)}",
        "level":   f"9 EMA: ${round(en, 2)} | VWAP: ${round(vw, 2) if vw else 'N/A'}",
        "vol":     f"Pullback light ✅ RVOL {rvol}x" if light_pb else f"Watch RVOL {rvol}x",
        "score":   score, "action": "Actionable",
        "notes":   f"Rising EMA | {bars_above}b above / {bars_below}b below before touch",
        "trigger_bar_ts": _trigger_bar(r),
    }


def ema9_pb_short(c5, p, vw, rvol, rs_mod, rs_tier="?"):
    """
    9 EMA pullback short — 5-minute bars.
    HARD GATE: price must be BELOW VWAP. Above VWAP = no short EMA setup.
    """
    r = rh(c5)
    if len(r) < 14:
        return False, {}

    # HARD GATE — must be below VWAP for shorts
    if vw and p >= vw:
        return False, {}

    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    if en is None:
        return False, {}

    ctx, bars_above, bars_below = ema_position_context(r, es, EMA_PRIOR_BARS_5M)
    if ctx != "from_below":
        return False, {}

    ema_vals  = [e for e in es[-5:] if e is not None]
    falling   = len(ema_vals) > 1 and ema_vals[-1] < ema_vals[0]
    last, prev = r[-1], r[-2]
    below_vwap = p < (vw or float("inf"))
    touched    = last["h"] >= en * 0.997 or prev["h"] >= en * 0.997
    rejecting  = last["c"] < last["o"] and last["c"] < en
    _, _, vol_ratio = vol_baseline(c5)
    light_bounce = vol_ratio is not None and vol_ratio <= 0.80

    if not (falling and touched and rejecting):
        return False, {}

    score = _apply_quality_modifiers(
        65 + (10 if light_bounce else 0) + (10 if below_vwap else 0) + (5 if falling else 0),
        rvol, -rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "EMA9_5M_PULLBACK_SHORT", "dir": "🔴 SHORT",
        "trigger": f"Break below ${round(prev['l'], 2)} after EMA rejection (from below)",
        "inval":   f"Reclaim through 9 EMA ${round(en, 2)}",
        "level":   f"9 EMA resistance: ${round(en, 2)}",
        "vol":     f"Bounce light ✅ RVOL {rvol}x" if light_bounce else f"Watch RVOL {rvol}x",
        "score":   score, "action": "Actionable",
        "notes":   f"Falling EMA | {bars_below}b below / {bars_above}b above before bounce",
        "trigger_bar_ts": _trigger_bar(r),
    }


def ema9_pb_long_10m(c10, p, vw, rvol, rs_mod, rs_tier="?"):
    """10-min EMA9 pullback long. Hard VWAP gate: must be above VWAP."""
    r = rh(c10)
    if len(r) < 12:
        return False, {}
    if vw and p <= vw:   # HARD GATE — no long below VWAP
        return False, {}
    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    if en is None:
        return False, {}

    ctx, bars_above, bars_below = ema_position_context(r, es, EMA_PRIOR_BARS_10M)
    if ctx != "from_above":
        return False, {}

    ema_vals   = [e for e in es[-4:] if e is not None]
    rising     = len(ema_vals) > 1 and ema_vals[-1] > ema_vals[0]
    last, prev = r[-1], r[-2]
    above_vwap = p > (vw or 0)
    touched    = last["l"] <= en * 1.003 or prev["l"] <= en * 1.003
    bouncing   = last["c"] > prev["h"] or last["c"] > en
    _, _, vol_ratio = vol_baseline(c10)
    light_pb   = vol_ratio is not None and vol_ratio <= 0.80

    if not (rising and above_vwap and touched and bouncing):
        return False, {}

    # 10-min base score is higher — more bars = more confirmation
    score = _apply_quality_modifiers(
        70 + (10 if light_pb else 0) + (10 if above_vwap else 0) + (5 if rising else 0),
        rvol, rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "EMA9_10M_PULLBACK_LONG", "dir": "🟢 LONG",
        "trigger": "Bounce after 9 EMA touch on 10-min (from above)",
        "inval":   f"Loss of 10-min 9 EMA ${round(en, 2)}",
        "level":   f"10m 9 EMA: ${round(en, 2)} | VWAP: ${round(vw, 2) if vw else 'N/A'}",
        "vol":     f"Pullback light ✅ RVOL {rvol}x" if light_pb else f"Watch RVOL {rvol}x",
        "score":   score, "action": "Actionable",
        "notes":   f"10-min confirmation | {bars_above}b above / {bars_below}b below before touch",
        "trigger_bar_ts": _trigger_bar(r),
    }


def ema9_pb_short_10m(c10, p, vw, rvol, rs_mod, rs_tier="?"):
    """10-min EMA9 pullback short. Hard VWAP gate: must be below VWAP."""
    r = rh(c10)
    if len(r) < 12:
        return False, {}
    if vw and p >= vw:   # HARD GATE — no short above VWAP
        return False, {}
    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    if en is None:
        return False, {}

    ctx, bars_above, bars_below = ema_position_context(r, es, EMA_PRIOR_BARS_10M)
    if ctx != "from_below":
        return False, {}

    ema_vals  = [e for e in es[-4:] if e is not None]
    falling   = len(ema_vals) > 1 and ema_vals[-1] < ema_vals[0]
    last, prev = r[-1], r[-2]
    below_vwap = p < (vw or float("inf"))
    touched    = last["h"] >= en * 0.997 or prev["h"] >= en * 0.997
    rejecting  = last["c"] < last["o"] and last["c"] < en
    _, _, vol_ratio = vol_baseline(c10)
    light_bounce = vol_ratio is not None and vol_ratio <= 0.80

    if not (falling and below_vwap and touched and rejecting):
        return False, {}

    score = _apply_quality_modifiers(
        70 + (10 if light_bounce else 0) + (10 if below_vwap else 0) + (5 if falling else 0),
        rvol, -rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "EMA9_10M_PULLBACK_SHORT", "dir": "🔴 SHORT",
        "trigger": f"Break below ${round(prev['l'], 2)} after 10-min EMA rejection",
        "inval":   f"Reclaim through 10-min 9 EMA ${round(en, 2)}",
        "level":   f"10m 9 EMA resistance: ${round(en, 2)}",
        "vol":     f"Bounce light ✅ RVOL {rvol}x" if light_bounce else f"Watch RVOL {rvol}x",
        "score":   score, "action": "Actionable",
        "notes":   f"10-min confirmation | {bars_below}b below / {bars_above}b above before bounce",
        "trigger_bar_ts": _trigger_bar(r),
    }


def ema4_10m_rider_long(c10, p, vw, rvol, rs_mod, rs_tier="?"):
    """
    4 EMA rider on 10-minute chart — catches runners.

    A stock in a strong directional trend rides the 4 EMA on 10-min.
    When price pulls back gently to touch the 4 EMA with contracting
    volume, it's the add zone or continuation entry for a runner.

    Requirements:
    - 4 EMA on 10-min is clearly upsloped (quantified by slope threshold)
    - Price has been ABOVE the 4 EMA for at least EMA4_MIN_BARS bars
    - Price touches or comes within EMA4_TOUCH_PCT of the 4 EMA
    - Volume contracting on pullback bars (healthy retracement)
    - RVOL > 1.5x — name must be active
    - Price above VWAP — trend context

    This setup intentionally does NOT fire on rangebound names because
    the slope requirement eliminates flat/chop conditions.
    """
    r = rh(c10)
    if len(r) < EMA4_MIN_BARS + 4:
        return False, {}

    cls = [c["c"] for c in r]
    es4 = ema_series(cls, 4)
    en4 = es4[-1]
    if en4 is None:
        return False, {}

    # Slope check — is the 4 EMA actually trending up?
    ema4_vals = [e for e in es4[-(EMA4_MIN_BARS + 2):] if e is not None]
    if len(ema4_vals) < 3:
        return False, {}
    avg_price = cls[-1] if cls[-1] else 1
    # Slope = (end - start) / (n bars * price) — normalized per bar per dollar
    slope = (ema4_vals[-1] - ema4_vals[0]) / (len(ema4_vals) * avg_price)
    if slope < EMA4_SLOPE_MIN:
        return False, {}    # 4 EMA is flat or declining — not a runner condition

    # Count bars above 4 EMA in the recent window
    recent_r   = r[-(EMA4_MIN_BARS + 3):]
    recent_es4 = es4[-(EMA4_MIN_BARS + 3):]
    bars_above = sum(1 for bar, ev in zip(recent_r, recent_es4)
                     if ev is not None and bar["c"] > ev)
    if bars_above < EMA4_MIN_BARS:
        return False, {}    # hasn't been trending long enough

    # Price touching the 4 EMA now (or previous bar)
    last, prev = r[-1], r[-2]
    touching = (abs(last["l"] - en4) / en4 <= EMA4_TOUCH_PCT or
                abs(prev["l"] - en4) / en4 <= EMA4_TOUCH_PCT or
                last["l"] <= en4 * (1 + EMA4_TOUCH_PCT))
    if not touching:
        return False, {}

    # Must still be holding above VWAP
    above_vwap = p > (vw or 0)
    if not above_vwap:
        return False, {}

    # Volume contracting on pullback (last 2 bars)
    _, _, vol_ratio = vol_baseline(c10)
    vol_contracting = vol_ratio is not None and vol_ratio <= 0.80

    # RVOL gate — must be in play
    if not rvol or rvol < RVOL_MIN:
        return False, {}

    score = _apply_quality_modifiers(
        68 + (12 if vol_contracting else 0) + (8 if above_vwap else 0)
           + (5 if rvol >= RVOL_STRONG else 0),
        rvol, rs_mod
    )
    if score < MIN_SCORE:
        return False, {}

    slope_pct = round(slope * 100, 3)
    return True, {
        "setup": "EMA4_10M_RIDER_LONG", "dir": "🟢 LONG",
        "trigger": f"Pullback to 10-min 4 EMA ${round(en4, 2)} — runner add zone",
        "inval":   f"Loss of 4 EMA ${round(en4, 2)} + close below",
        "level":   f"10m 4 EMA: ${round(en4, 2)} | VWAP: ${round(vw, 2) if vw else 'N/A'}",
        "vol":     f"Contracting ✅ RVOL {rvol}x" if vol_contracting else f"Watch | RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable" if vol_contracting else "Watch",
        "notes":   (f"Riding 4 EMA on 10-min | Slope: +{slope_pct}%/bar | "
                    f"{bars_above} bars trending above | Add or continuation entry"),
        "trigger_bar_ts": _trigger_bar(r),
    }


def ema4_10m_rider_short(c10, p, vw, rvol, rs_mod, rs_tier="?"):
    """4 EMA rider on 10-minute chart — short side."""
    r = rh(c10)
    if len(r) < EMA4_MIN_BARS + 4:
        return False, {}

    cls = [c["c"] for c in r]
    es4 = ema_series(cls, 4)
    en4 = es4[-1]
    if en4 is None:
        return False, {}

    ema4_vals = [e for e in es4[-(EMA4_MIN_BARS + 2):] if e is not None]
    if len(ema4_vals) < 3:
        return False, {}
    avg_price = cls[-1] if cls[-1] else 1
    slope = (ema4_vals[-1] - ema4_vals[0]) / (len(ema4_vals) * avg_price)
    if slope > -EMA4_SLOPE_MIN:
        return False, {}    # not sloping down

    recent_r   = r[-(EMA4_MIN_BARS + 3):]
    recent_es4 = es4[-(EMA4_MIN_BARS + 3):]
    bars_below = sum(1 for bar, ev in zip(recent_r, recent_es4)
                     if ev is not None and bar["c"] < ev)
    if bars_below < EMA4_MIN_BARS:
        return False, {}

    last, prev = r[-1], r[-2]
    touching = (abs(last["h"] - en4) / en4 <= EMA4_TOUCH_PCT or
                abs(prev["h"] - en4) / en4 <= EMA4_TOUCH_PCT or
                last["h"] >= en4 * (1 - EMA4_TOUCH_PCT))
    if not touching:
        return False, {}

    below_vwap = p < (vw or float("inf"))
    if not below_vwap:
        return False, {}

    _, _, vol_ratio = vol_baseline(c10)
    vol_contracting = vol_ratio is not None and vol_ratio <= 0.80

    if not rvol or rvol < RVOL_MIN:
        return False, {}

    score = _apply_quality_modifiers(
        68 + (12 if vol_contracting else 0) + (8 if below_vwap else 0)
           + (5 if rvol >= RVOL_STRONG else 0),
        rvol, -rs_mod
    )
    if score < MIN_SCORE:
        return False, {}

    slope_pct = round(abs(slope) * 100, 3)
    return True, {
        "setup": "EMA4_10M_RIDER_SHORT", "dir": "🔴 SHORT",
        "trigger": f"Bounce to 10-min 4 EMA ${round(en4, 2)} — runner add zone short",
        "inval":   f"Reclaim above 4 EMA ${round(en4, 2)} + close above",
        "level":   f"10m 4 EMA: ${round(en4, 2)} | VWAP: ${round(vw, 2) if vw else 'N/A'}",
        "vol":     f"Contracting ✅ RVOL {rvol}x" if vol_contracting else f"Watch | RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable" if vol_contracting else "Watch",
        "notes":   (f"Riding 4 EMA on 10-min (short) | Slope: -{slope_pct}%/bar | "
                    f"{bars_below} bars trending below | Add or continuation entry"),
        "trigger_bar_ts": _trigger_bar(r),
    }


def flag_long(c5, p, vw, rvol, rs_mod, rs_tier="?"):
    r = rh(c5)
    if len(r) < 8:
        return False, {}
    _, _, vol_ratio = vol_baseline(c5)
    avg_vol = av(c5)
    impulse = None
    for c in r[-10:-3]:
        if (c["c"] - c["o"]) > 0 and c["v"] > (avg_vol or 0) * 1.5:
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
    vol_expand = vol_ratio is not None and vol_ratio >= 1.2
    dried_up   = vol_ratio is not None and vol_ratio <= 0.80
    if not (tight and broke and above_vwap):
        return False, {}
    score = _apply_quality_modifiers(
        65 + (10 if dried_up else 0) + (15 if vol_expand else 0) + (5 if tight else 0),
        rvol, rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "FLAG_BREAKOUT_LONG", "dir": "🟢 LONG",
        "trigger": f"Break above flag high ${round(flag_high, 2)}",
        "inval":   f"Loss of flag low ${round(flag_low, 2)}",
        "level":   f"Flag: ${round(flag_low, 2)}–${round(flag_high, 2)}",
        "vol":     f"Dry-up + expansion ✅ RVOL {rvol}x" if (dried_up and vol_expand) else f"Watch RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable" if (vol_expand and tight) else "Watch",
        "notes":   f"Flag range ${round(flag_range, 2)} vs impulse ${round(imp_size, 2)}",
        "trigger_bar_ts": _trigger_bar(r),
    }


def pdh_retest(c5, p, vw, pdh, rvol, rs_mod, rs_tier="?"):
    """
    PDH Break + Retest Long.

    Root bug fix: was firing when price was near PDH from BELOW —
    a stock approaching PDH from the downside is resistance, not a breakout.

    Correct conditions:
    1. Price must have CLEANLY broken above PDH earlier today
       (a bar must have CLOSED above PDH, not just spiked through)
    2. Price pulled back to retest PDH from ABOVE (now treating it as support)
    3. Price is currently holding at or above PDH
    4. Above VWAP — trend context confirmed
    """
    if not pdh:
        return False, {}
    r = rh(c5)
    if len(r) < 3:
        return False, {}

    # HARD FIX: require a CLOSED bar above PDH, not just a high spike
    # A spike that closed back below is not a valid break
    clean_break = any(c["c"] > pdh for c in r[:-2])
    if not clean_break:
        return False, {}

    # HARD FIX: price must be above PDH now (holding, not approaching from below)
    # If price is below PDH it is approaching from the downside — that is NOT this setup
    if p < pdh * 0.997:
        return False, {}

    near       = abs(p - pdh) / pdh <= 0.005   # within 0.5% of PDH = retest zone
    above      = p >= pdh * 0.998
    above_vwap = p > (vw or 0)
    _, _, vol_ratio = vol_baseline(c5)
    light_pb   = vol_ratio is not None and vol_ratio <= 0.85

    if not (above and above_vwap):
        return False, {}

    score = _apply_quality_modifiers(
        70 + (10 if near else 0) + (10 if light_pb else 0) + (5 if above_vwap else 0),
        rvol, rs_mod, rs_tier
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "PDH_BREAK_RETEST_LONG", "dir": "🟢 LONG",
        "trigger": f"Holding above PDH ${round(pdh, 2)} after clean break",
        "inval":   f"Close back below ${round(pdh * 0.997, 2)}",
        "level":   f"Prior Day High: ${round(pdh, 2)}",
        "vol":     f"Pullback light ✅ RVOL {rvol}x" if light_pb else f"Watch RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable" if near and above_vwap else "Watch",
        "notes":   "Daily breakout retest — closed above PDH + holding as support",
        "trigger_bar_ts": _trigger_bar(r),
    }


def pdl_retest(c5, p, vw, pdl, rvol, rs_mod, rs_tier="?"):
    """PDL Break + Retest Short. Must have CLOSED below PDL and still be below it."""
    if not pdl:
        return False, {}
    r = rh(c5)
    if len(r) < 3:
        return False, {}
    # Must have a closed bar below PDL (not just a wick)
    clean_break = any(c["c"] < pdl for c in r[:-2])
    if not clean_break:
        return False, {}
    # Must currently be below PDL — not approaching from above
    if p > pdl * 1.003:
        return False, {}
    near         = abs(p - pdl) / pdl <= 0.005
    below        = p <= pdl * 1.002
    below_vwap   = p < (vw or float("inf"))
    _, _, vol_ratio = vol_baseline(c5)
    light_bounce = vol_ratio is not None and vol_ratio <= 0.85
    if not (below and below_vwap):
        return False, {}
    score = _apply_quality_modifiers(
        70 + (10 if near else 0) + (10 if light_bounce else 0),
        rvol, -rs_mod, rs_tier
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "PDL_BREAK_RETEST_SHORT", "dir": "🔴 SHORT",
        "trigger": f"Reject under PDL ${round(pdl, 2)} after clean close below",
        "inval":   f"Reclaim above ${round(pdl * 1.003, 2)}",
        "level":   f"Prior Day Low: ${round(pdl, 2)}",
        "vol":     f"Bounce light ✅ RVOL {rvol}x" if light_bounce else f"Watch RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable" if near and below_vwap else "Watch",
        "notes":   "Prior day low broke — holding below as resistance",
        "trigger_bar_ts": _trigger_bar(r),
    }


def later_day_hod_breakout(c5, p, vw, rvol, rs_mod, rs_tier="?"):
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
    above_vwap = p > (vw or 0)
    broke      = p > prior_hod and prev["c"] <= prior_hod
    _, _, vol_ratio = vol_baseline(c5)
    vol        = vol_ratio is not None and vol_ratio >= 1.2
    base_below = len(r) >= 5 and all(c["h"] <= prior_hod * 1.002 for c in r[-5:-1])
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


def fib_pullback_long(c5, p, vw, rvol):
    if not rvol or rvol < RVOL_MIN:
        return False, {}
    r = rh(c5)
    if len(r) < FIB_MIN_BARS + 4:
        return False, {}
    leg = find_impulse_leg(c5, direction="long")
    if not leg:
        return False, {}
    levels = fib_levels_from_leg(leg, direction="long")
    nearest_level, nearest_fib, nearest_dist = None, None, float("inf")
    for fib_pct, fib_price in levels.items():
        dist = abs(p - fib_price) / fib_price
        if dist < nearest_dist:
            nearest_dist, nearest_fib, nearest_level = dist, fib_pct, fib_price
    if nearest_dist > FIB_TOLERANCE:
        return False, {}
    if p < levels[0.618] * 0.997:
        return False, {}
    above_vwap = p > (vw or 0)
    if not above_vwap:
        return False, {}
    _, _, vol_ratio = vol_baseline(c5)
    vol_contracting = vol_ratio is not None and vol_ratio <= 0.85
    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    ema_confluence  = (en is not None and abs(en - nearest_level) / nearest_level <= FIB_CONFLUENCE)
    vwap_confluence = (vw is not None and abs(vw - nearest_level) / nearest_level <= FIB_CONFLUENCE)
    ema_vals   = [e for e in es[-5:] if e is not None]
    ema_rising = len(ema_vals) > 1 and ema_vals[-1] > ema_vals[0]
    if not ema_rising:
        return False, {}
    fib_label = {0.382: "38.2%", 0.500: "50%", 0.618: "61.8%"}.get(nearest_fib, str(nearest_fib))
    base  = {0.500: 70, 0.382: 65, 0.618: 62}.get(nearest_fib, 60)
    score = base
    score += 10 if vol_contracting else 0
    score += 8  if rvol >= RVOL_STRONG else (4 if rvol >= RVOL_MIN else 0)
    score += 8  if ema_confluence else 0
    score += 6  if vwap_confluence else 0
    score += 5  if above_vwap else 0
    score += time_of_day_modifier()
    if score < MIN_SCORE:
        return False, {}
    conf_str = " + ".join(filter(None, ["EMA9" if ema_confluence else "", "VWAP" if vwap_confluence else ""])) or "None"
    return True, {
        "setup":   "FIB_PULLBACK_LONG", "dir": "🟢 LONG",
        "trigger": f"Hold at {fib_label} retrace ${round(nearest_level, 2)} + bounce",
        "inval":   f"Loss of 61.8% level ${round(levels[0.618], 2)}",
        "level":   f"38.2%:${levels[0.382]} | 50%:${levels[0.500]} | 61.8%:${levels[0.618]}",
        "vol":     f"Contracting ✅ RVOL {rvol}x" if vol_contracting else f"Watch | RVOL {rvol}x",
        "score":   clamp_score(score),
        "action":  "Actionable" if (vol_contracting and score >= 70) else "Watch",
        "notes":   (f"Impulse ${round(leg['low'],2)}→${round(leg['high'],2)} "
                    f"({round(leg['size'],2)}pts {leg['bars']}bars) | At {fib_label} | Conf: {conf_str}"),
        "trigger_bar_ts": _trigger_bar(r),
    }


def fib_pullback_short(c5, p, vw, rvol):
    if not rvol or rvol < RVOL_MIN:
        return False, {}
    r = rh(c5)
    if len(r) < FIB_MIN_BARS + 4:
        return False, {}
    leg = find_impulse_leg(c5, direction="short")
    if not leg:
        return False, {}
    levels = fib_levels_from_leg(leg, direction="short")
    nearest_level, nearest_fib, nearest_dist = None, None, float("inf")
    for fib_pct, fib_price in levels.items():
        dist = abs(p - fib_price) / fib_price
        if dist < nearest_dist:
            nearest_dist, nearest_fib, nearest_level = dist, fib_pct, fib_price
    if nearest_dist > FIB_TOLERANCE:
        return False, {}
    if p > levels[0.618] * 1.003:
        return False, {}
    below_vwap = p < (vw or float("inf"))
    if not below_vwap:
        return False, {}
    _, _, vol_ratio = vol_baseline(c5)
    vol_contracting = vol_ratio is not None and vol_ratio <= 0.85
    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    ema_confluence  = (en is not None and abs(en - nearest_level) / nearest_level <= FIB_CONFLUENCE)
    vwap_confluence = (vw is not None and abs(vw - nearest_level) / nearest_level <= FIB_CONFLUENCE)
    ema_vals   = [e for e in es[-5:] if e is not None]
    ema_falling = len(ema_vals) > 1 and ema_vals[-1] < ema_vals[0]
    if not ema_falling:
        return False, {}
    fib_label = {0.382: "38.2%", 0.500: "50%", 0.618: "61.8%"}.get(nearest_fib, str(nearest_fib))
    base  = {0.500: 70, 0.382: 65, 0.618: 62}.get(nearest_fib, 60)
    score = base
    score += 10 if vol_contracting else 0
    score += 8  if rvol >= RVOL_STRONG else (4 if rvol >= RVOL_MIN else 0)
    score += 8  if ema_confluence else 0
    score += 6  if vwap_confluence else 0
    score += 5  if below_vwap else 0
    score += time_of_day_modifier()
    if score < MIN_SCORE:
        return False, {}
    conf_str = " + ".join(filter(None, ["EMA9" if ema_confluence else "", "VWAP" if vwap_confluence else ""])) or "None"
    return True, {
        "setup":   "FIB_PULLBACK_SHORT", "dir": "🔴 SHORT",
        "trigger": f"Rejection at {fib_label} bounce ${round(nearest_level, 2)}",
        "inval":   f"Acceptance above 61.8% bounce ${round(levels[0.618], 2)}",
        "level":   f"38.2%:${levels[0.382]} | 50%:${levels[0.500]} | 61.8%:${levels[0.618]}",
        "vol":     f"Contracting ✅ RVOL {rvol}x" if vol_contracting else f"Watch | RVOL {rvol}x",
        "score":   clamp_score(score),
        "action":  "Actionable" if (vol_contracting and score >= 70) else "Watch",
        "notes":   (f"Impulse ${round(leg['high'],2)}→${round(leg['low'],2)} "
                    f"({round(leg['size'],2)}pts {leg['bars']}bars) | At {fib_label} | Conf: {conf_str}"),
        "trigger_bar_ts": _trigger_bar(r),
    }


# ──────────────────────────────────────────────────────────────
# SWEEP LOGIC (unchanged — armed names only)
# ──────────────────────────────────────────────────────────────

def opening_drive_long(c5, c1, p, vw, pmh_v, prior_close, rvol, rs_mod, rs_tier="?"):
    """
    Opening Drive / Gap and Go — catches AAOI/NBIS/AAPL gap breakout scenarios.

    This setup fires when a name gaps up significantly and is holding the drive
    in the first 75 minutes. It explicitly does NOT require a pullback to an EMA
    because the trade IS the opening drive — you're confirming the drive is real
    and holding, not waiting for a retracement.

    The existing ORB setup misses these because `was_below` (prev bar closed
    below ORB high) never triggers on strong gap-up names where all bars close
    above the ORB high from the start.

    Conditions:
    - Only fires in first 75 minutes (9:30–10:45 ET)
    - Stock opened at least 2% above prior close (confirmed gap)
    - Current price is above VWAP (drive still holding)
    - First 5-min bar closed in top 50% of its range (strong directional open)
    - Price is above premarket high (gap didn't fade back into PM range)
    - RVOL above 1.5x (name is genuinely in play — uses gap-adjusted RVOL)
    - Price has NOT retraced more than 40% from session high (drive intact)

    Scores higher for:
    - Larger gap %
    - Counter-trend RS (stock up while market flat/down)
    - RVOL > 2x
    - Price at/near session high (drive still extending)
    """
    ts = now_et()
    # Only fire in first 75 minutes
    if not in_window(ts, 9, 30, 10, 45):
        return False, {}

    # Need prior close to calculate gap
    if not prior_close or prior_close == 0:
        return False, {}

    r = rh(c5)
    if len(r) < 2:
        return False, {}

    # Confirm gap up >= 2%
    today_open = r[0]["o"]
    gap = (today_open - prior_close) / prior_close * 100
    if gap < 2.0:
        return False, {}

    # VWAP — must be holding above it (drive is real, not fading)
    above_vwap = p > (vw or 0)
    if not above_vwap:
        return False, {}

    # RVOL gate — must be in play
    if not rvol or rvol < RVOL_MIN:
        return False, {}

    # First bar quality — must have closed in top 50% of its range
    first_bar = r[0]
    first_bar_range = first_bar["h"] - first_bar["l"]
    first_bar_close_pos = ((first_bar["c"] - first_bar["l"]) / first_bar_range
                           if first_bar_range > 0 else 0)
    if first_bar_close_pos < 0.50:
        return False, {}

    # Price must be above PMH (gap didn't fade back into premarket range)
    if pmh_v and p < pmh_v:
        return False, {}

    # Drive intact — price hasn't retraced more than 40% from session high
    session_high = max(c["h"] for c in r)
    session_low  = r[0]["l"]   # approximate drive base as opening bar low
    drive_size   = session_high - session_low
    if drive_size > 0:
        retrace_pct = (session_high - p) / drive_size
        if retrace_pct > 0.40:
            return False, {}

    # Score
    base  = 68
    score = base
    score += 10 if gap >= 5.0 else (5 if gap >= 3.0 else 2)
    score += 8  if rvol >= RVOL_STRONG else (4 if rvol >= RVOL_MIN else 0)
    score += 5  if above_vwap else 0
    score += rs_mod
    score += time_of_day_modifier()

    # Tier gate — still applies
    if rs_tier in ("C", "D"):
        return False, {}

    score = clamp_score(score)
    if score < MIN_SCORE:
        return False, {}

    near_hod    = abs(p - session_high) / session_high <= 0.005
    action      = "Actionable" if (rvol >= RVOL_STRONG and near_hod) else "Watch"

    return True, {
        "setup":   "OPENING_DRIVE_LONG",
        "dir":     "🟢 LONG",
        "trigger": f"Opening drive holding above gap — ${round(today_open, 2)} open, ${round(p, 2)} now",
        "inval":   f"Loss of VWAP ${round(vw, 2) if vw else 'N/A'} or PM High ${round(pmh_v, 2) if pmh_v else 'N/A'}",
        "level":   f"Gap: +{round(gap, 1)}% | Session HOD: ${round(session_high, 2)} | VWAP: ${round(vw, 2) if vw else 'N/A'}",
        "vol":     f"RVOL {rvol}x ✅" if rvol >= RVOL_STRONG else f"RVOL {rvol}x",
        "score":   score,
        "action":  action,
        "notes":   (f"Gap +{round(gap, 1)}% from ${round(prior_close, 2)} | "
                    f"Drive {round((1-retrace_pct)*100, 0):.0f}% intact | "
                    f"First bar top {round(first_bar_close_pos*100,0):.0f}%"),
        "trigger_bar_ts": _trigger_bar(r),
    }


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
        "vol":     "Context only", "score": score, "action": "Watch now",
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
        "vol":     "Live decision zone", "score": score, "action": "Decision zone",
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
        "vol":     "Reclaim confirmed", "score": score,
        "action":  "Actionable" if score >= 70 else "Watch closely",
        "notes":   (f"{'Same-candle' if same_bar else 'Next-candle'} reclaim | "
                    f"{'Green' if is_green(used_bar) else 'Not green'} | "
                    f"Close: {round(close_pos_in_range(used_bar)*100,1)}% of range"),
        "trigger_bar_ts": _trigger_bar(r),
    }


# ──────────────────────────────────────────────────────────────
# FORMATTERS — RS tier drives the alert grade label
# ──────────────────────────────────────────────────────────────

def fmt(ticker, d, rs_label="", rs_tier="?"):
    """
    Alert formatter.

    The alert GRADE shown at the top is a composite of setup score
    AND RS tier — a stock touching an EMA with D-tier RS gets a
    completely different visual treatment than a counter-trend A+ RS.

    Grade logic:
      Score 85+ AND RS tier A+/A  → 🔥 A+ SETUP
      Score 70-84 AND RS tier A+/A → ✅ A SETUP
      Score 70+ AND RS tier B       → ✅ B+ SETUP
      Score 60-69 OR RS tier C/D    → ⚠️ B/C SETUP — watch only
      Counter-trend RS (A+)         → always adds ⚡ to header
    """
    sc    = d.get("score", 0)
    setup = d.get("setup", "")

    # Determine composite grade
    counter_trend = rs_tier == "A+"
    if sc >= 85 and rs_tier in ("A+", "A"):
        grade_em    = "🔥"
        grade_label = "A+ SETUP"
    elif sc >= 70 and rs_tier in ("A+", "A"):
        grade_em    = "✅"
        grade_label = "A SETUP"
    elif sc >= 70 and rs_tier == "B":
        grade_em    = "✅"
        grade_label = "B+ SETUP"
    elif sc >= 60 and rs_tier in ("B", "?"):
        grade_em    = "⚠️"
        grade_label = "B SETUP — watch"
    elif rs_tier in ("C", "D"):
        grade_em    = "⚠️"
        grade_label = "C SETUP — low priority"
    else:
        grade_em    = "⚠️"
        grade_label = "Watch"

    ct_tag = " ⚡" if counter_trend else ""

    header = f"{grade_em}{ct_tag} <b>{ticker} — {setup}</b>  {d.get('dir', '')}"
    grade  = f"<b>{grade_label}</b>  |  Score: {sc}/100  |  {d.get('action', 'Watch')}"

    rs_line = f"📈 <b>RS [{rs_tier}]:</b> {rs_label}" if rs_label else f"📈 <b>RS:</b> N/A"

    lines = [
        header,
        grade,
        "━" * 30,
        f"📍 <b>Trigger:</b> {d.get('trigger', '')}",
        f"🛑 <b>Stop:</b> {d.get('inval', '')}",
        f"🔑 <b>Level:</b> {d.get('level', '')}",
        f"📊 <b>Volume:</b> {d.get('vol', '')}",
        rs_line,
        f"📝 {d.get('notes', '')}",
        "━" * 30,
        f"⏰ {now_et().strftime('%I:%M %p ET')}",
        f"👉 {d.get('action', 'Review before entry')}",
    ]
    return "\n".join(lines)


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

        # SPY data cached per cycle
        self._spy_ctx  = {"session_pct": None, "atr_pct": None, "recent_dir": "flat"}
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
                self._spy_ctx  = {"session_pct": None, "atr_pct": None, "recent_dir": "flat"}
                print(f"[DAILY RESET] Cleared {old} stale signals. Session: {today}")
                if old > 0:
                    send_alert(f"🔄 <b>Daily Reset</b>\nCleared {old} signals from prior session.")

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
        Fetch SPY data once per cycle — shared across all tickers.
        Computes session pct, ATR pct, and recent direction (last 4 bars).
        The recent direction is what catches the GOOGL-while-SPY-sells scenario.
        """
        try:
            spy_c5 = candles("SPY", 5)
            spy_c5 = closed_only(spy_c5, 5)
            self._spy_ctx  = build_spy_context(spy_c5)
            self._spy_last = now_et()
            print(f"[SPY] pct={self._spy_ctx['session_pct']}% "
                  f"atr={self._spy_ctx['atr_pct']}% "
                  f"dir={self._spy_ctx['recent_dir']}")
        except Exception as e:
            print(f"[SPY ERR] {e}")
            self._spy_ctx = {"session_pct": None, "atr_pct": None, "recent_dir": "flat"}

    def should_fire(self, ticker, setup, bar_ts, current_price=None):
        """
        Three-layer dedup — any one layer blocking = no fire.

        LAYER 1 — Per-bar dedup (unchanged):
          Same bar timestamp as last fire = absolute block.
          One alert per bar, always.

        LAYER 2 — Time gap (raised to 30 min):
          Even on a new bar, must wait 30 minutes since last fire
          of this setup on this ticker.

        LAYER 3 — Price-level dedup (NEW — the stale alert fix):
          If price hasn't moved meaningfully since the last fire,
          the setup hasn't reset — it's the same stale condition
          re-evaluating. Gate: price must have moved at least 0.15%
          from the level at which the signal last fired.
          This kills the "ranging stock firing EMA alerts every 5 min"
          problem because price keeps coming back to the same level.
          A real new setup requires price to have moved away and come back.
        """
        key  = (ticker, setup)
        info = self.fired_signals.get(key)
        if info is None:
            return True

        stored_bar_ts   = info.get("bar_ts")
        stored_price    = info.get("fired_price")
        fired_at        = info.get("fired_at")

        # LAYER 1 — same bar = block
        if bar_ts is not None and stored_bar_ts is not None and bar_ts == stored_bar_ts:
            return False

        # LAYER 2 — time gap
        if fired_at:
            gap_min = (now_et() - fired_at).total_seconds() / 60
            if gap_min < MIN_REFIRE_GAP_MIN:
                return False

        # LAYER 3 — price must have moved meaningfully since last fire
        if current_price and stored_price and stored_price > 0:
            price_move_pct = abs(current_price - stored_price) / stored_price * 100
            if price_move_pct < 0.15:
                # Price has barely moved — this is the same stale condition
                return False

        return True

    def mark_fired(self, ticker, setup, bar_ts, score, current_price=None):
        self.fired_signals[(ticker, setup)] = {
            "fired_at":    now_et(),
            "bar_ts":      bar_ts,
            "score":       score,
            "fired_price": current_price,  # NEW — store price at fire time
        }

    def check_api_health(self, cycle_had_data=True):
        """
        Alert if Schwab API has been returning empty responses.
        The _api_consecutive_failures counter is incremented in _get()
        on failure and reset on success. We just check the threshold here.
        """
        global _api_consecutive_failures, _api_failure_alerted
        if (_api_consecutive_failures >= API_FAILURE_THRESHOLD
                and not _api_failure_alerted):
            _api_failure_alerted = True
            send_telegram(
                "🚨 <b>SCANNER FEED DOWN</b>\n"
                f"Schwab API returning errors for ~{_api_consecutive_failures} min.\n"
                "Alerts are NOT firing. Check Railway logs.\n"
                "Send /reauth if auth may have expired."
            )
        elif _api_consecutive_failures == 0 and _api_failure_alerted:
            _api_failure_alerted = False
            send_telegram("✅ <b>Scanner feed recovered.</b> Alerts resuming.")

    def scan_standard(self, ticker):
        candidates = []
        try:
            c5_raw  = candles(ticker, 5)
            c1_raw  = candles(ticker, 1)
            c10_raw = candles(ticker, 10)
            c5      = closed_only(c5_raw,  5)
            c1      = closed_only(c1_raw,  1)
            c10     = closed_only(c10_raw, 10)
            if not c5 or not c1:
                return candidates
            # Mark that data loaded — used by API health monitor
            # Return non-empty list sentinel handled in run loop
            _ = True   # candles loaded successfully

            p = price(ticker)
            if not p:
                return candidates

            vw    = vwap(c5)
            pmh_v = self.pmh.get(ticker)
            pml_v = self.pml.get(ticker)
            pd          = self.pr.get(ticker, {})
            pdh         = pd.get("h")
            pdl         = pd.get("l")
            prior_close = pd.get("c")   # prior day close — used for gap detection

            # Quality inputs — computed once per ticker
            # Pass prior_close so calc_rvol can detect gap days and adjust baseline
            rvol             = calc_rvol(c5, prior_close=prior_close)
            tkr_pct          = session_pct_change(c5)
            tkr_atr_pct      = atr_pct(c5)
            tkr_recent_dir   = recent_direction(c5, lookback=4)

            # Full direction-aware RS grading — catches counter-trend strength
            rs_mod, rs_label, rs_tier = grade_rs(
                tkr_pct, tkr_atr_pct, tkr_recent_dir,
                self._spy_ctx, direction="long"
            )
            rs_mod_short, rs_label_short, rs_tier_short = grade_rs(
                tkr_pct, tkr_atr_pct, tkr_recent_dir,
                self._spy_ctx, direction="short"
            )

            setups = [
                # ── LONG setups — use long RS ──
                ("OPENING_DRIVE_LONG",
                 lambda: opening_drive_long(c5, c1, p, vw, pmh_v, prior_close, rvol, rs_mod, rs_tier)),
                ("ORB_5M_LONG",
                 lambda: orb_5m_long(c5, c1, p, vw, pmh_v, rvol, rs_mod, rs_tier)),
                ("ORB_15M_LONG",
                 lambda: orb_15m_long(c5, c1, p, vw, pmh_v, rvol, rs_mod, rs_tier)),
                ("PMH_BREAK_RETEST_LONG",
                 lambda: pmh_retest(c5, p, vw, pmh_v, rvol, rs_mod, rs_tier)),
                ("VWAP_RECLAIM_LONG",
                 lambda: vwap_reclaim(c5, p, vw, rvol, rs_mod, rs_tier)),
                ("EMA9_5M_PULLBACK_LONG",
                 lambda: ema9_pb_long(c5, p, vw, rvol, rs_mod, rs_tier)),
                ("EMA9_10M_PULLBACK_LONG",
                 lambda: ema9_pb_long_10m(c10, p, vw, rvol, rs_mod, rs_tier) if c10 else (False, {})),
                ("EMA4_10M_RIDER_LONG",
                 lambda: ema4_10m_rider_long(c10, p, vw, rvol, rs_mod, rs_tier) if c10 else (False, {})),
                ("FLAG_BREAKOUT_LONG",
                 lambda: flag_long(c5, p, vw, rvol, rs_mod, rs_tier)),
                ("PDH_BREAK_RETEST_LONG",
                 lambda: pdh_retest(c5, p, vw, pdh, rvol, rs_mod, rs_tier)),
                ("LATER_DAY_HOD_BREAKOUT",
                 lambda: later_day_hod_breakout(c5, p, vw, rvol, rs_mod, rs_tier)),
                ("FIB_PULLBACK_LONG",
                 lambda: fib_pullback_long(c5, p, vw, rvol)),

                # ── SHORT setups — use short RS (inverted) ──
                ("PML_BREAK_RETEST_SHORT",
                 lambda: pml_retest(c5, p, vw, pml_v, rvol, rs_mod_short, rs_tier_short)),
                ("VWAP_REJECT_SHORT",
                 lambda: vwap_reject(c5, p, vw, rvol, rs_mod_short, rs_tier_short)),
                ("EMA9_5M_PULLBACK_SHORT",
                 lambda: ema9_pb_short(c5, p, vw, rvol, rs_mod_short, rs_tier_short)),
                ("EMA9_10M_PULLBACK_SHORT",
                 lambda: ema9_pb_short_10m(c10, p, vw, rvol, rs_mod_short, rs_tier_short) if c10 else (False, {})),
                ("EMA4_10M_RIDER_SHORT",
                 lambda: ema4_10m_rider_short(c10, p, vw, rvol, rs_mod_short, rs_tier_short) if c10 else (False, {})),
                ("PDL_BREAK_RETEST_SHORT",
                 lambda: pdl_retest(c5, p, vw, pdl, rvol, rs_mod_short, rs_tier_short)),
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
                    if not self.should_fire(ticker, name, bar_ts, current_price=p):
                        continue
                    # Tag correct RS label based on direction
                    is_short   = "SHORT" in name
                    alert_tier = rs_tier_short if is_short else rs_tier
                    # HARD GATE: suppress C and D tier alerts entirely
                    if alert_tier in ("C", "D"):
                        continue
                    d["_rs_label"]    = rs_label_short if is_short else rs_label
                    d["_rs_tier"]     = alert_tier
                    d["_fired_price"] = p   # stored for price-level dedup in mark_fired
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
                    if not self.should_fire(ticker, name, bar_ts, current_price=p):
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
        # FIX: normalize input — strip whitespace, handle encoding issues
        pts = command.strip().split()
        c   = pts[0].lower() if pts else ""

        if c == "/watch" and len(pts) >= 2:
            added = []
            for raw in pts[1:]:
                t = raw.upper().strip()   # FIX: explicit strip
                if t and t not in self.wl:
                    self.wl.append(t)
                    added.append(t)
            self.save_state()
            send_alert(f"✅ Added: {', '.join(added) if added else 'none'}\nWatching {len(self.wl)} stocks")

        elif c == "/remove" and len(pts) >= 2:
            removed = []
            for raw in pts[1:]:
                t = raw.upper().strip()   # FIX: explicit strip
                # FIX: rebuild list with normalized comparison
                before = len(self.wl)
                self.wl = [x for x in self.wl if x.upper().strip() != t]
                if len(self.wl) < before:
                    self.armed.discard(t)
                    removed.append(t)
            self.save_state()
            send_alert(f"🗑️ Removed: {', '.join(removed) if removed else 'none (not found)'}")

        elif c == "/arm" and len(pts) >= 2:
            armed_now = []
            for raw in pts[1:]:
                t = raw.upper().strip()
                if t not in self.wl:
                    self.wl.append(t)
                self.armed.add(t)
                armed_now.append(t)
            self.save_state()
            send_alert(f"🎯 Armed sweep logic for: {', '.join(armed_now)}")

        elif c == "/disarm" and len(pts) >= 2:
            removed = []
            for raw in pts[1:]:
                t = raw.upper().strip()
                if t in self.armed:
                    self.armed.discard(t)
                    removed.append(t)
            self.save_state()
            send_alert(f"🧹 Disarmed: {', '.join(removed) if removed else 'none'}")

        elif c == "/armed":
            send_alert(f"🎯 Armed ({len(self.armed)}):\n{', '.join(sorted(self.armed)) or 'none'}")

        elif c == "/list":
            send_alert(f"📋 Watching ({len(self.wl)}):\n{', '.join(self.wl)}")

        elif c == "/status":
            spy = self._spy_ctx
            spy_str = (f"{spy.get('session_pct','N/A')}% | "
                       f"dir={spy.get('recent_dir','?')} | "
                       f"ATR={spy.get('atr_pct','N/A')}%")
            send_alert(
                f"📊 <b>Scanner v3.12</b>\n"
                f"Stocks: {len(self.wl)} | Armed: {len(self.armed)}\n"
                f"Min score: {MIN_SCORE}/100\n"
                f"Active fired signals: {len(self.fired_signals)}\n"
                f"SPY: {spy_str}\n"
                f"Session: {self.session_date}\n"
                f"RVOL min gate: {RVOL_MIN}x\n"
                f"Earnings tags: {', '.join(sorted(self.earnings)) or 'none'}"
            )

        elif c == "/setups":
            send_alert(
                "📊 <b>Active Setups (v3.8)</b>\n"
                "1. ORB 5m Long | 2. ORB 15m Long\n"
                "3. PM High Retest | 4. PM Low Retest\n"
                "5. VWAP Reclaim | 6. VWAP Reject\n"
                "7. EMA9 5m PB Long | 8. EMA9 5m PB Short\n"
                "9. EMA9 10m PB Long | 10. EMA9 10m PB Short\n"
                "11. EMA4 10m Rider Long | 12. EMA4 10m Rider Short\n"
                "13. Flag Breakout Long\n"
                "14. PDH Retest | 15. PDL Retest\n"
                "16. Later-Day HOD Breakout\n"
                "17. Fib Pullback Long | 18. Fib Pullback Short\n"
                "<b>Armed only:</b>\n"
                "19. Sweep Watch | 20. Sweep Active | 21. Sweep Reclaim\n\n"
                "<b>RS Grades (shown on every alert):</b>\n"
                "⚡ A+ = Counter-trend (+15) — stock vs market\n"
                "✅ A  = Strong normalized RS (+10)\n"
                "✅ B+ = Good RS (+5)\n"
                "⚠️ B  = Neutral — market doing the work\n"
                "⚠️ C  = Weak RS (-5 to -10)\n"
                "⚠️ D  = Lagging (-12 to -18) — suppressed"
            )

        elif c == "/threshold" and len(pts) == 2:
            try:
                MIN_SCORE = int(pts[1])
                send_alert(f"⚙️ Min score: {MIN_SCORE}/100")
            except Exception:
                send_alert("❌ Usage: /threshold 65")

        elif c == "/reset":
            old = len(self.fired_signals)
            self.fired_signals.clear()
            send_alert(f"🔄 Cleared {old} fired signals. Scanner reset.")

        elif c == "/fired":
            if not self.fired_signals:
                send_alert("📭 No active fired signals.")
            else:
                n     = now_et()
                lines = ["📋 <b>Active fired signals:</b>"]
                for (t, s), info in sorted(self.fired_signals.items()):
                    age = int((n - info["fired_at"]).total_seconds() / 60)
                    ttl = SIGNAL_TTL.get(s, DEFAULT_TTL)
                    lines.append(f"• {t} {s} — {age}m old (TTL {ttl}m)")
                send_alert("\n".join(lines))

        elif c == "/earnings" and len(pts) >= 2:
            added = [raw.upper().strip() for raw in pts[1:]]
            for t in added:
                self.earnings.add(t)
            send_alert(f"📋 Earnings flagged: {', '.join(added)}")

        elif c == "/unearnings" and len(pts) >= 2:
            removed = []
            for raw in pts[1:]:
                t = raw.upper().strip()
                if t in self.earnings:
                    self.earnings.remove(t)
                    removed.append(t)
            send_alert(f"🧹 Earnings unflagged: {', '.join(removed) if removed else 'none'}")

        elif c == "/auth" and len(pts) >= 2:
            _complete_auth(" ".join(pts[1:]))

        elif c == "/reauth":
            _save_tokens({})
            send_telegram("🔄 Tokens cleared. Starting fresh auth...")
            threading.Thread(target=_login, daemon=True).start()

        else:
            send_alert(
                "Commands:\n"
                "/watch /remove /arm /disarm /armed /list\n"
                "/status /setups /threshold 65\n"
                "/reset /fired\n"
                "/earnings /unearnings /reauth"
            )

    def run(self):
        print("[SCANNER] v3.12 starting")
        send_alert(
            f"🤖 <b>Scanner v3.12 Online</b>\n{'━' * 28}\n"
            f"Watching <b>{len(self.wl)} stocks</b> | Armed <b>{len(self.armed)}</b>\n"
            f"Threshold ≥ {MIN_SCORE}/100\n{'━' * 28}\n"
            f"<b>New in v3.8 — Direction-Aware RS:</b>\n"
            f"• ⚡ Counter-trend RS (+15) — stock vs market\n"
            f"• ATR-normalized scoring (GOOGL-type moves captured)\n"
            f"• SPY recent direction tracked every cycle\n"
            f"• Alert grade A+/A/B/C/D shown on every alert\n"
            f"• Short RS computed separately (inverted)\n\n"
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
            self.refresh_spy()

            cycle_had_data = True   # health tracked via _api_consecutive_failures in _get

            for t in list(self.wl):
                print(f"[SCAN] {t}...")
                try:
                    results = self.scan_standard(t)
                    # scan_standard returns [] on no data OR on no setups firing
                    # We track data separately via the global _api_consecutive_failures
                    # which resets in _get() on any successful API call
                    if results is not None:
                        cycle_had_data = True
                    for name, d in (results or []):
                        bar_ts      = d.get("trigger_bar_ts")
                        fired_price = d.get("_fired_price")
                        rs_label    = d.pop("_rs_label", "")
                        rs_tier     = d.pop("_rs_tier",  "?")
                        d.pop("_fired_price", None)
                        send_alert(fmt(t, d, rs_label, rs_tier))
                        self.mark_fired(t, name, bar_ts, d.get("score", 0), current_price=fired_price)
                        time.sleep(1)
                except Exception as e:
                    print(f"[STD LOOP ERR] {t}:{e}")

                try:
                    for name, d in self.scan_sweep(t):
                        bar_ts      = d.get("trigger_bar_ts")
                        fired_price = d.get("_fired_price")
                        d.pop("_fired_price", None)
                        send_alert(fmt(t, d))
                        self.mark_fired(t, name, bar_ts, d.get("score", 0), current_price=fired_price)
                        time.sleep(1)
                except Exception as e:
                    print(f"[SWEEP LOOP ERR] {t}:{e}")

                time.sleep(0.35)

            self.check_api_health(cycle_had_data)
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
        f"[MAIN] v3.8 | Schwab:{'OK' if SCHWAB_CLIENT_ID else 'MISSING'} | "
        f"Telegram:{'OK' if TELEGRAM_TOKEN else 'MISSING'}"
    )
    sc = Scanner()
    threading.Thread(target=listen, args=(sc,), daemon=True).start()
    sc.run()
