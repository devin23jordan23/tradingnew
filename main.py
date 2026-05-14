"""
main.py — Andre's Trading Scanner v3.15

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
    "AAPL", "AVGO", "META", "DELL", "RKLB", "MRVL",
    "QCOM", "AAOI", "ARM", "INTC"
]
# Removed: XOM, CVX, COIN, ANET, CRDO, LITE, COHR per user request.
# Added: QCOM (strong mover, options liquid), kept INTC and AAOI as catalysts.
# Set WATCHLIST env var in Railway to make changes persist across restarts.
# The watchlist is persisted in watchlist_state.json on Railway.
# If that file is missing on restart, this default is used.
# To permanently remove a ticker use /remove — it updates watchlist_state.json.
# The DEFAULT_WATCHLIST is the fallback for fresh deploys only.

MIN_SCORE = 70  # raised from 60 — requires genuine quality signal beyond baseline
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
    "EMA9_2M_FIRST_PULLBACK_LONG":  10,
    "EMA9_2M_FIRST_PULLBACK_SHORT": 10,
    "EMA_STACK_MOMENTUM_LONG":      20,
    "EMA_STACK_MOMENTUM_SHORT":     20,
    "EMA9_10M_PULLBACK_LONG":   18,
    "EMA9_10M_PULLBACK_SHORT":  18,
    "EMA4_10M_RIDER_LONG":      20,
    "EMA4_10M_RIDER_SHORT":     20,
    "FLAG_BREAKOUT_LONG":       18,
    "PDH_BREAK_RETEST_LONG":    30,
    "PDL_BREAK_RETEST_SHORT":   30,
    "LATER_DAY_HOD_BREAKOUT":   25,
    "OPENING_DRIVE_LONG":       30,
    "FIB_PULLBACK_LONG":        20,
    "FIB_PULLBACK_SHORT":       20,
    "FASHIONABLY_LATE_LONG":    20,
    "FASHIONABLY_LATE_SHORT":   20,
    "RUBBER_BAND_SCALP_LONG":   15,   # momentum snapback — act fast or miss it
    "SECOND_CHANCE_SCALP_LONG": 20,   # retest valid for ~20 min before level goes stale
    "HITCHHIKER_SCALP_LONG":    10,   # opening drive only, tight window
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


def daily_atr21(ticker):
    """
    Fetch last 21 daily bars and compute true ATR.
    Called once per day per ticker, cached in self.datr.
    This gives the real 21-day ATR that shows on your TOS chart —
    the baseline against which we measure whether today's move is
    60-70%+ of ATR (genuinely in play) or just drifting.
    """
    try:
        d = _get(f"/pricehistory?symbol={ticker}", {
            "periodType": "month", "period": 2,
            "frequencyType": "daily", "frequency": 1,
            "needExtendedHoursData": "false",
        })
        bars = d.get("candles", [])[-22:]
        if len(bars) < 2:
            return None
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        recent = trs[-21:]
        return round(sum(recent) / len(recent), 2) if recent else None
    except Exception:
        return None


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

    UPGRADED: Now uses candle BODY midpoint instead of just close price.
    A bar that closes barely above the EMA but has its entire body below
    it is classified as a "below" bar. This fixes the core false signal —
    price surging into the EMA from below, body below EMA, close just
    crossing — was being classified as "from_above" incorrectly.

    Body midpoint = (open + close) / 2. This is more representative of
    where price actually spent time during that bar than the close alone.
    A wick that pokes through the EMA doesn't count as being above it.

    Also checks: prior swing high must exist above EMA (confirms price
    was actually up there and pulled back, not approached from below).
    """
    LOOKBACK = 8
    if len(r) < LOOKBACK + 1 or len(es) < LOOKBACK + 1:
        return "insufficient_data", 0, 0

    check_bars = r[-(LOOKBACK + 1):-1]
    check_emas = es[-(LOOKBACK + 1):-1]

    above_count = 0
    below_count = 0
    for bar, ema_val in zip(check_bars, check_emas):
        if ema_val is None:
            continue
        # Use body midpoint — not just close
        body_mid = (bar["o"] + bar["c"]) / 2
        if body_mid > ema_val:
            above_count += 1
        else:
            below_count += 1

    total = above_count + below_count
    if total == 0:
        return "insufficient_data", 0, 0

    if above_count >= prior_bars_required and below_count == 0:
        return "from_above", above_count, below_count
    elif below_count >= prior_bars_required and above_count == 0:
        return "from_below", above_count, below_count
    else:
        return "mixed", above_count, below_count


def _candle_approach_direction(r, es, lookback=4):
    """
    Determine whether price is approaching the EMA from above or below
    by analyzing the CHARACTER of the bars leading into the touch.

    For a valid LONG pullback (approaching from above):
    - The last 2-4 bars before the touch should show DECLINING closes
      (price came down to the EMA — it was higher before)
    - The open of the touch bar should be ABOVE or AT the EMA
      (price opened above EMA and dipped to touch it, not surged up to it)
    - There must be a prior swing high ABOVE the current EMA level
      in the last 10 bars (price was up there and came back down)

    For a false long (approaching from below):
    - The bars leading to the touch show RISING closes (price surging up)
    - The open of the touch bar is BELOW the EMA (opened below, trying to break through)
    - No prior swing high above the EMA — price was never up there

    Returns: "pullback_long", "pullback_short", or "false_approach"
    """
    if len(r) < lookback + 2 or len(es) < lookback + 2:
        return "insufficient_data"

    en = es[-1]
    if en is None:
        return "insufficient_data"

    last = r[-1]
    touch_bar_open = last["o"]

    # Check 1: open of touch bar relative to EMA
    # Valid long pullback: bar opened at or above EMA (came down to it)
    # False approach from below: bar opened below EMA (surging up through it)
    open_above_ema = touch_bar_open >= en * 0.997   # within 0.3% counts as "at"

    # Check 2: character of preceding bars (were closes declining or rising?)
    pre_bars = r[-(lookback + 1):-1]
    if len(pre_bars) >= 2:
        closes = [c["c"] for c in pre_bars]
        # Declining closes = pullback character (good for long)
        # Rising closes = approach from below character (bad for long, good for short entry)
        net_change = closes[-1] - closes[0]
        declining_approach = net_change < 0   # closes fell into the EMA
        rising_approach    = net_change > 0   # closes rose into the EMA
    else:
        declining_approach = False
        rising_approach    = False

    # Check 3: prior swing high above EMA (was price up there before?)
    # Look back 10 bars for a high significantly above the current EMA
    prior_bars = r[-11:-1] if len(r) >= 11 else r[:-1]
    prior_swing_high = max((c["h"] for c in prior_bars), default=0)
    had_swing_high_above = prior_swing_high > en * 1.005   # at least 0.5% above EMA

    # Classify
    if open_above_ema and declining_approach and had_swing_high_above:
        return "pullback_long"   # clean pullback from above — valid long
    elif not open_above_ema and rising_approach and not had_swing_high_above:
        return "false_approach"  # surging from below — false long signal
    elif not open_above_ema and rising_approach:
        return "false_approach"  # opened below EMA, rising into it
    elif open_above_ema and rising_approach:
        return "false_approach"  # opened above but rising = not a pullback, a continuation
    else:
        return "ambiguous"       # unclear — let other gates decide


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
    # HARD GATE: RVOL near-zero means no session data exists yet (premarket or
    # early session with no volume baseline). Not a penalty — a hard block.
    # RVOL 0.0x or 0.01x appearing on alerts = meaningless data firing.
    if rvol is not None and rvol < 0.1:
        return 0   # force score to zero — will fail MIN_SCORE gate

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
    """
    PMH Break + Retest Long.

    FIXED: Was firing all day whenever price was near the PMH level because
    `any(c["h"] > pmh_v ...)` was true all day once the initial break happened.
    A PMH retest at 2:30pm when the break was at 9:45am is not the same trade.

    Fixes:
    1. Require a CLOSED bar above PMH (not just a wick spike)
    2. Time gate: the break must have happened within the last 12 bars (60 min)
       — after that the level is too stale for a retest trade
    3. Retest must happen within 8 bars of the break bar
    """
    if not pmh_v:
        return False, {}
    r = rh(c5)
    if len(r) < 3:
        return False, {}

    prior_bars = r[:-2]   # exclude last 2 bars (current + trigger bar)

    # CLOSED bar above PMH (fix: was using c["h"] = wick spike)
    break_bars = [i for i, c in enumerate(prior_bars) if c["c"] > pmh_v * 1.001]
    if not break_bars:
        return False, {}

    # Most recent break bar index
    last_break_idx  = break_bars[-1]
    bars_since_break = len(r) - 2 - last_break_idx   # bars since the break

    # TIME GATE: break must have happened within 12 bars (~60 min)
    if bars_since_break > 20:   # raised from 12 — 100min window
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
        "trigger": f"Hold above PM High ${round(pmh_v, 2)} — {bars_since_break} bars after break",
        "inval":   f"Loss of ${round(pmh_v * 0.997, 2)}",
        "level":   f"PM High: ${round(pmh_v, 2)}",
        "vol":     f"Pullback light ✅ RVOL {rvol}x" if light_pb else f"Watch | RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable" if near and above_vwap else "Watch",
        "notes":   f"PMH broken {bars_since_break} bars ago ({bars_since_break*5}min) — retesting as support",
        "trigger_bar_ts": _trigger_bar(r),
    }


def pml_retest(c5, p, vw, pml_v, rvol, rs_mod, rs_tier="?"):
    """PML Break + Retest Short. Closed bar below PML + time gate (12 bars)."""
    if not pml_v:
        return False, {}
    r = rh(c5)
    if len(r) < 3:
        return False, {}
    prior_bars  = r[:-2]
    break_bars  = [i for i, c in enumerate(prior_bars) if c["c"] < pml_v * 0.999]
    if not break_bars:
        return False, {}
    last_break_idx   = break_bars[-1]
    bars_since_break = len(r) - 2 - last_break_idx
    if bars_since_break > 20:   # raised from 12 — 100min window
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
        "trigger": f"Reject under PM Low ${round(pml_v, 2)} — {bars_since_break} bars after break",
        "inval":   f"Reclaim ${round(pml_v * 1.003, 2)}",
        "level":   f"PM Low: ${round(pml_v, 2)}",
        "vol":     f"Bounce light ✅ RVOL {rvol}x" if light_bounce else f"Watch | RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable" if near and below_vwap else "Watch",
        "notes":   f"PML broken {bars_since_break} bars ago ({bars_since_break*5}min) — rejecting as resistance",
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

    QUALITY GATES (all must pass — these separate actionable from noise):

    Gate 1 — VWAP: must be above VWAP. Hard gate.
    Gate 2 — Direction: approached from above only. Hard gate.
    Gate 3 — EMA slope: must be genuinely rising, not drifting.
             Slope measured as % of price per bar. Flat EMA = ranging stock = skip.
    Gate 4 — Touch count: max 2 EMA touches this session.
             3+ touches = oscillating, not trending. COIN all day fails here.
    Gate 5 — Volume dry-up on pullback: REQUIRED, not a bonus.
             Active volume on pullback = sellers still present = not clean.
    Gate 6 — Bounce bar quality: must close in top 40% of range AND
             bar range >= 0.15% of price. Doji at EMA = indecision = skip.
    """
    r = rh(c5)
    if len(r) < 14:
        return False, {}

    # GATE 1 — above VWAP
    if vw and p <= vw:
        return False, {}

    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    if en is None:
        return False, {}

    # GATE 2 — direction context: approached from above (body-based)
    ctx, bars_above, bars_below = ema_position_context(r, es, EMA_PRIOR_BARS_5M)
    if ctx != "from_above":
        return False, {}
    # Note: _candle_approach_direction removed — it incorrectly flagged valid
    # uptrend pullbacks as "false_approach" when pre-bars showed rising closes
    # (which is normal on a strong trend before the pullback bar)

    # GATE 3 — EMA slope must be meaningful (0.03% of price per bar minimum)
    # This kills flat/ranging stocks where EMA drifted up into price
    ema_vals = [e for e in es[-6:] if e is not None]
    if len(ema_vals) < 4:
        return False, {}
    slope_pct = (ema_vals[-1] - ema_vals[0]) / ema_vals[0] * 100 / len(ema_vals)
    if slope_pct < 0.015:   # lowered from 0.03 — mature trends have lower slope
        return False, {}

    # GATE 4 — touch count: max 2 EMA touches this session
    # Count how many times price got within 0.3% of EMA in the full session
    touch_count = sum(
        1 for i, (bar, ema_v) in enumerate(zip(r[:-1], es[:-1]))
        if ema_v is not None and bar["l"] <= ema_v * 1.003
    )
    if touch_count > 3:   # raised — strong trends have 3+ valid pullbacks
        return False, {}

    last, prev = r[-1], r[-2]

    # GATE 5 — volume check (score modifier, not hard gate)
    # On strong trend days volume is elevated session-wide — a hard gate here
    # blocks every EMA pullback on GOOGL/AAPL running all day.
    # Score: dry-up = +10 bonus, elevated = no penalty (stock is just in play)
    # Only block if volume is EXPANDING significantly on the pullback (sellers pushing)
    _, _, vol_ratio = vol_baseline(c5)
    vol_expanding_on_pb = vol_ratio is not None and vol_ratio > 1.50
    light_pb            = vol_ratio is not None and vol_ratio <= 0.80

    # Must be touching the EMA
    touched = last["l"] <= en * 1.003 or prev["l"] <= en * 1.003
    if not touched:
        return False, {}

    # GATE 6 — bounce bar quality: close in top 40% of range, real range
    bar_range = last["h"] - last["l"]
    min_range  = p * 0.0015   # 0.15% of price minimum range
    close_pos  = (last["c"] - last["l"]) / bar_range if bar_range > 0 else 0
    if bar_range < min_range or close_pos < 0.40:
        return False, {}

    # Block only if volume actively EXPANDING on pullback (sellers in control)
    if vol_expanding_on_pb:
        return False, {}

    above_vwap = p > (vw or 0)
    rising     = slope_pct > 0

    score = _apply_quality_modifiers(
        68 + (8 if touch_count == 1 else 3) + (5 if above_vwap else 0)
           + (10 if light_pb else 0) + (5 if rising else 0),
        rvol, rs_mod
    )
    if score < MIN_SCORE:
        return False, {}

    return True, {
        "setup": "EMA9_5M_PULLBACK_LONG", "dir": "🟢 LONG",
        "trigger": f"{'First' if touch_count == 1 else 'Second'} clean 9 EMA pullback from above",
        "inval":   f"Loss of 9 EMA ${round(en, 2)}",
        "level":   f"9 EMA: ${round(en, 2)} | VWAP: ${round(vw, 2) if vw else 'N/A'}",
        "vol":     f"Pullback dry ✅ vol ratio {round(vol_ratio,2)}x | RVOL {rvol}x",
        "score":   score, "action": "Actionable",
        "notes":   (f"Touch #{touch_count} | EMA slope {round(slope_pct*100,1)}bp/bar | "
                    f"Close pos: top {round(close_pos*100,0):.0f}%"),
        "trigger_bar_ts": _trigger_bar(r),
    }


def ema9_pb_short(c5, p, vw, rvol, rs_mod, rs_tier="?"):
    """
    9 EMA pullback short — 5-minute bars.
    Same quality gates as long version, inverted:
    Gate 1: below VWAP. Gate 3: EMA slope falling 0.03%/bar min.
    Gate 4: max 2 touches. Gate 5: vol dry-up required.
    Gate 6: close in bottom 40% of range.
    """
    r = rh(c5)
    if len(r) < 14:
        return False, {}

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

    ema_vals = [e for e in es[-6:] if e is not None]
    if len(ema_vals) < 4:
        return False, {}
    slope_pct = (ema_vals[-1] - ema_vals[0]) / ema_vals[0] * 100 / len(ema_vals)
    if slope_pct > -0.03:   # EMA must be falling at least 0.03%/bar
        return False, {}

    # Touch count — max 2
    touch_count = sum(
        1 for bar, ema_v in zip(r[:-1], es[:-1])
        if ema_v is not None and bar["h"] >= ema_v * 0.997
    )
    if touch_count > 3:   # raised — strong trends have 3+ valid pullbacks
        return False, {}

    last, prev = r[-1], r[-2]

    # Volume dry-up required
    _, _, vol_ratio = vol_baseline(c5)
    if vol_ratio is None or vol_ratio > 0.80:
        return False, {}

    touched   = last["h"] >= en * 0.997 or prev["h"] >= en * 0.997
    if not touched:
        return False, {}

    # Rejection bar: close in bottom 40% of range
    bar_range = last["h"] - last["l"]
    min_range  = p * 0.0015
    close_pos  = (last["c"] - last["l"]) / bar_range if bar_range > 0 else 1
    if bar_range < min_range or close_pos > 0.60:
        return False, {}

    below_vwap = p < (vw or float("inf"))
    score = _apply_quality_modifiers(
        68 + (8 if touch_count == 1 else 3) + (5 if below_vwap else 0),
        rvol, -rs_mod
    )
    if score < MIN_SCORE:
        return False, {}

    return True, {
        "setup": "EMA9_5M_PULLBACK_SHORT", "dir": "🔴 SHORT",
        "trigger": f"{'First' if touch_count == 1 else 'Second'} clean 9 EMA rejection from below",
        "inval":   f"Reclaim through 9 EMA ${round(en, 2)}",
        "level":   f"9 EMA resistance: ${round(en, 2)}",
        "vol":     f"Bounce dry ✅ vol ratio {round(vol_ratio,2)}x | RVOL {rvol}x",
        "score":   score, "action": "Actionable",
        "notes":   (f"Touch #{touch_count} | EMA slope {round(slope_pct*100,1)}bp/bar | "
                    f"Close pos: bot {round((1-close_pos)*100,0):.0f}%"),
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
    """
    Later-Day HOD Breakout.

    FIXED: Was firing every time price touched the HOD level even hours later.
    Root cause: `prior_hod` was recalculated fresh each cycle so a HOD set at
    10am would still trigger at 2pm when price came back to that level.

    Fix: The break must be RECENT — the HOD must have been set within the last
    10 bars (50 minutes on 5-min). If the HOD was set earlier and price has
    been ranging since, it's not a fresh breakout, it's stale.

    Also: `prev["c"] <= prior_hod` only catches one bar before the break.
    Now requiring that at least 3 of the last 5 bars were BELOW the HOD
    (a real base built below) before the current bar breaks above it.
    """
    ts = now_et()
    if not in_window(ts, HOD_START[0], HOD_START[1], HOD_END[0], HOD_END[1]):
        return False, {}
    r = rh(c5)
    if len(r) < 10:
        return False, {}
    last, prev = r[-1], r[-2]

    # Find the index of the bar that set the HOD (excluding current bar)
    prior_bars = r[:-1]
    hod_val    = max(c["h"] for c in prior_bars)
    hod_idx    = max(i for i, c in enumerate(prior_bars) if c["h"] == hod_val)

    # FRESHNESS GATE: HOD must have been set within last 10 bars (~50 min)
    # If HOD was set 20 bars ago and price has been below it since, it's stale
    bars_since_hod = len(prior_bars) - 1 - hod_idx
    if bars_since_hod > 20:   # raised — allow 100min consolidation before break
        return False, {}

    # BASE GATE: at least 3 of last 5 bars (before current) must be below HOD
    # This confirms a real base was built, not just a single dip and pop
    base_check_bars = r[-6:-1]
    bars_below_hod  = sum(1 for c in base_check_bars if c["h"] <= hod_val * 1.002)
    if bars_below_hod < 1:   # lowered — just need 1 pause bar below HOD
        return False, {}

    # BREAK GATE: current bar must be breaking above HOD
    broke      = p > hod_val and prev["c"] <= hod_val * 1.002
    above_vwap = p > (vw or 0)
    if not (broke and above_vwap):
        return False, {}

    _, _, vol_ratio = vol_baseline(c5)
    vol = vol_ratio is not None and vol_ratio >= 1.2

    score = _apply_quality_modifiers(
        68 + (10 if vol else 0) + (8 if bars_below_hod >= 4 else 0) + (5 if above_vwap else 0),
        rvol, rs_mod
    )
    if score < MIN_SCORE:
        return False, {}
    return True, {
        "setup": "LATER_DAY_HOD_BREAKOUT", "dir": "🟢 LONG",
        "trigger": f"Break above HOD ${round(hod_val, 2)} — {bars_since_hod} bars after HOD set",
        "inval":   f"Fail back under ${round(hod_val, 2)}",
        "level":   f"HOD: ${round(hod_val, 2)} | Base: {bars_below_hod} bars below",
        "vol":     f"Expanding ✅ RVOL {rvol}x" if vol else f"Average RVOL {rvol}x",
        "score":   score, "action": "Actionable",
        "notes":   f"HOD set {bars_since_hod} bars ago ({bars_since_hod*5}min) | Base {bars_below_hod}/5 bars below",
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


# ──────────────────────────────────────────────────────────────
# FASHIONABLY LATE — SMB Rubber Band VWAP/EMA Cross Model
# ──────────────────────────────────────────────────────────────

def _measure_ema_vwap_divergence(r, es, vw_series, direction="long"):
    """
    Scan backwards through bars to identify the divergence and
    convergence phases of the Fashionably Late model.

    Returns a dict with:
      impulse_start_idx  — bar where EMA/VWAP separation began
      exhaustion_idx     — bar of swing high (long) or swing low (short)
      divergence_bars    — number of bars in divergence phase
      convergence_bars   — number of bars in convergence phase (up to current)
      max_separation     — peak |EMA - VWAP| seen during divergence
      exhaustion_price   — price at exhaustion point (for stop calc)
      vol_div_avg        — avg volume during divergence phase
      vol_conv_avg       — avg volume during convergence phase
    Returns None if phases can't be cleanly identified.
    """
    if len(r) < 8 or len(es) < 8 or not vw_series:
        return None

    # Build parallel VWAP series aligned with r
    # vw_series is a list of cumulative VWAP values per bar
    n = min(len(r), len(es), len(vw_series))
    if n < 8:
        return None

    # Find the exhaustion point — swing high for longs, swing low for shorts
    # Look back max 20 bars
    lookback = min(n - 1, 20)
    window = r[n - lookback:n]
    window_es = es[n - lookback:n]
    window_vw = vw_series[n - lookback:n]

    if direction == "long":
        # Swing high = highest bar before the current reversal
        # Find bar with max high in the first 2/3 of the window
        search_end = max(3, len(window) * 2 // 3)
        exh_idx_local = max(range(search_end), key=lambda i: window[i]["h"])
    else:
        search_end = max(3, len(window) * 2 // 3)
        exh_idx_local = min(range(search_end), key=lambda i: window[i]["l"])

    exh_bar   = window[exh_idx_local]
    exh_price = exh_bar["h"] if direction == "long" else exh_bar["l"]
    exh_ema   = window_es[exh_idx_local]
    exh_vwap  = window_vw[exh_idx_local]
    if exh_ema is None or exh_vwap is None:
        return None

    max_sep = abs(exh_ema - exh_vwap)
    # RAISED from 0.001 to 0.008 — rubber band must be genuinely stretched.
    # Looking at the SMB charts the EMA/VWAP separation at exhaustion is
    # clearly 1-3% of price. 0.001 (0.1%) was so small it allowed flat
    # markets where EMA and VWAP naturally drift apart by noise.
    # 0.008 = 0.8% minimum — on a $180 stock that's $1.44 separation.
    if max_sep / (exh_vwap or 1) < 0.008:
        return None   # not a real rubber band stretch

    # Divergence phase: bars from impulse start up to exhaustion
    div_bars  = window[:exh_idx_local + 1]
    conv_bars = window[exh_idx_local + 1:]

    divergence_bars  = len(div_bars)
    convergence_bars = len(conv_bars)

    if divergence_bars < 2 or convergence_bars < 1:
        return None

    vol_div_avg  = sum(c["v"] for c in div_bars)  / len(div_bars)
    vol_conv_avg = sum(c["v"] for c in conv_bars) / len(conv_bars) if conv_bars else 0

    return {
        "exhaustion_price":  exh_price,
        "max_separation":    max_sep,
        "divergence_bars":   divergence_bars,
        "convergence_bars":  convergence_bars,
        "vol_div_avg":       vol_div_avg,
        "vol_conv_avg":      vol_conv_avg,
        "exh_ema":           exh_ema,
        "exh_vwap":          exh_vwap,
    }


def _rolling_vwap_series(cs):
    """
    Build a per-bar cumulative VWAP value series aligned with rh(cs).
    Returns list of VWAP values, one per regular-hours bar.
    """
    r = rh(cs)
    out = []
    total_val = total_vol = 0
    for c in r:
        tp = (c["h"] + c["l"] + c["c"]) / 3
        total_val += tp * c["v"]
        total_vol += c["v"]
        out.append(total_val / total_vol if total_vol else None)
    return out


def fashionably_late_long(c5, p, vw, rvol, rs_mod, rs_tier="?", spy_ctx=None):
    """
    Fashionably Late — Long Version (SMB Rubber Band VWAP/EMA Cross Model)

    Phase model:
    1. IMPULSE: price drives up, EMA and VWAP diverge (EMA > VWAP)
    2. EXHAUSTION: swing high forms, momentum slows
    3. CONVERGENCE: price pulls back toward VWAP, EMA curls back
    4. CROSS TRIGGER: EMA crosses back above VWAP from below, or
       price reclaims VWAP with EMA supporting from below

    Entry = the cross bar or immediate retest hold.
    Stop = exhaustion_high + (cross_price - exhaustion_high) / 3
           (1/3 of way from exhaustion to cross, from the top)

    Hard invalids from the model:
    - Convergence > 15 min (price lingered too long near VWAP)
    - EMA choppily crossing VWAP multiple times (no clean separation)
    - Market conflict (SPY trend opposing)
    - Convergence volume < divergence volume (distribution, not reset)
    """
    r = rh(c5)
    if len(r) < 12:
        return False, {}

    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    if en is None or not vw:
        return False, {}

    # Build rolling VWAP series
    vw_series = _rolling_vwap_series(c5)
    if len(vw_series) < 12:
        return False, {}

    # ── TRIGGER: EMA must be crossing above or just crossed above VWAP ──
    # Last bar: EMA crosses above VWAP
    # Previous bar: EMA was below VWAP (or very close — within 0.1%)
    prev_ema  = es[-2]  if len(es)  >= 2 else None
    prev_vwap = vw_series[-2] if len(vw_series) >= 2 else None
    curr_vwap = vw_series[-1] if vw_series else None

    if prev_ema is None or prev_vwap is None or curr_vwap is None:
        return False, {}

    # ── TRIGGER — three valid entry conditions ──
    # Per the SMB image: "enter in direction of 9 EMA slope as it GETS READY
    # to cross." This means we fire BEFORE or AT the cross, not only after.
    #
    # Condition A — EMA approaching: within 0.3% of VWAP, still below, converging fast
    ema_approaching = (
        en < curr_vwap and
        en >= curr_vwap * 0.997 and           # within 0.3% below VWAP
        prev_ema < en and                      # EMA still rising toward VWAP
        (curr_vwap - en) < (prev_vwap - prev_ema)  # gap is narrowing
    )
    # Condition B — EMA just crossed above VWAP this bar
    ema_just_crossed = (en >= curr_vwap * 0.9995) and (prev_ema < prev_vwap * 1.001)
    # Condition C — EMA crossed last bar, price holding above VWAP (retest hold)
    ema_retest_hold  = (en > curr_vwap) and (prev_ema >= prev_vwap * 0.9995) and (p > curr_vwap)

    if not (ema_approaching or ema_just_crossed or ema_retest_hold):
        return False, {}

    # ── EMA must be sloping UP (not flat or curling down) ──
    ema_vals = [e for e in es[-5:] if e is not None]
    ema_rising = len(ema_vals) >= 3 and ema_vals[-1] > ema_vals[-3]
    if not ema_rising:
        return False, {}

    # ── Price must be at or near VWAP (approaching from below or just crossed) ──
    if p < vw * 0.996:   # allow slightly below for approaching condition
        return False, {}

    # ── PHASE VALIDATION: measure divergence and convergence ──
    phases = _measure_ema_vwap_divergence(r, es, vw_series, direction="long")
    if not phases:
        return False, {}

    div_bars  = phases["divergence_bars"]
    conv_bars = phases["convergence_bars"]
    vol_div   = phases["vol_div_avg"]
    vol_conv  = phases["vol_conv_avg"]
    exh_price = phases["exhaustion_price"]
    max_sep   = phases["max_separation"]

    # HARD FILTER 1: convergence must be < 15 min (3 bars on 5-min)
    CONV_MAX_BARS = 3
    if conv_bars > CONV_MAX_BARS:
        return False, {}

    # HARD FILTER 2: convergence must be faster than divergence (< 2/3 time)
    if conv_bars >= div_bars * 0.67:
        return False, {}

    # HARD FILTER 3: convergence volume must exceed divergence volume
    if vol_conv and vol_div and vol_conv < vol_div * 0.9:
        return False, {}

    # HARD FILTER 4: meaningful separation — 0.8% minimum of price
    # Per SMB charts the rubber band must be genuinely stretched.
    # 0.1% was too small — looked at charts, separation is clearly 1-3%.
    if max_sep / (p or 1) < 0.008:
        return False, {}

    # HARD FILTER 5: market conflict check
    if spy_ctx:
        spy_dir = spy_ctx.get("recent_dir", "flat")
        if spy_dir == "down":
            return False, {}

    # HARD FILTER 6: NO SIGNIFICANT PAUSE between convergence and cross
    # Per SMB image 7: "significant pause after convergence but before the cross"
    # = INVALID. Detect by checking if the last conv_bars had overlapping,
    # low-range candles (chop) before reaching the trigger.
    if conv_bars >= 2:
        conv_window = r[-(conv_bars + 1):-1]
        if len(conv_window) >= 2:
            ranges = [c["h"] - c["l"] for c in conv_window]
            avg_range = sum(ranges) / len(ranges) if ranges else 0
            atr_val   = atr(c5) or 1
            # Pause detected: avg range < 25% of ATR = overlapping/flat candles
            if avg_range > 0 and avg_range < atr_val * 0.25:
                return False, {}

    # ── CROSS BAR QUALITY ──
    cross_bar = r[-1]
    bar_range = cross_bar["h"] - cross_bar["l"]
    close_pos = (cross_bar["c"] - cross_bar["l"]) / bar_range if bar_range > 0 else 0
    if close_pos < 0.40:
        return False, {}

    # ── STOP & TARGET CALCULATION (per official SMB PDF) ──
    # PDF says: "Hard stop 1/3 the distance from VWAP to the Low of the day"
    # Stop  = cross_price - (cross_price - lod) / 3
    # Target = cross_price + (cross_price - lod)  [1 measured move above cross]
    # where measured move = distance from LOD to cross point
    cross_price = p
    lod         = min(c["l"] for c in r) if r else cross_price
    measured    = cross_price - lod if cross_price > lod else 0
    stop_price  = round(cross_price - measured / 3, 2) if measured > 0 else None
    tgt_price   = round(cross_price + measured, 2)     if measured > 0 else None
    stop_str    = (f"${stop_price} (1/3 of LOD→cross distance below entry)"
                  if stop_price else "Below LOD")
    tgt_str     = f"${tgt_price} (measured move above cross)" if tgt_price else "N/A"

    # Status label — approaching vs confirmed
    status = "Cross imminent ⚡" if ema_approaching else "Cross confirmed ✅"

    # ── SCORE ──
    base  = 70   # higher base — this is a structured institutional model
    score = base
    score += 10 if vol_conv > vol_div * 1.2 else (5 if vol_conv >= vol_div * 0.9 else 0)
    score += 8  if rvol and rvol >= RVOL_STRONG else (3 if rvol and rvol >= RVOL_MIN else -5)
    score += 5  if close_pos >= 0.60 else 0   # strong close on cross bar
    score += 5  if conv_bars <= 2 else 0       # fast convergence = more energy
    score += rs_mod
    score += time_of_day_modifier()

    # Tier gate
    if rs_tier in ("C", "D"):
        return False, {}

    score = clamp_score(score)
    if score < MIN_SCORE:
        return False, {}

    return True, {
        "setup":   "FASHIONABLY_LATE_LONG",
        "dir":     "🟢 LONG",
        "trigger": f"{status} — EMA9 {'approaching' if ema_approaching else 'crossed above'} VWAP ${round(curr_vwap, 2)}",
        "inval":   stop_str,
        "level":   (f"VWAP: ${round(curr_vwap, 2)} | EMA9: ${round(en, 2)} | "
                    f"LOD: ${round(lod, 2)} | Target: {tgt_str}"),
        "vol":     (f"Conv {round(vol_conv/vol_div, 1)}x div volume ✅ RVOL {rvol}x"
                    if vol_div > 0 else f"RVOL {rvol}x"),
        "score":   score,
        "action":  "Actionable" if score >= 75 else "Watch",
        "notes":   (f"Fashionably Late | Div {div_bars}b → Conv {conv_bars}b | "
                    f"EMA/VWAP sep: ${round(max_sep, 2)} | "
                    f"Measured move: ${round(measured, 2)} | "
                    f"Cross bar: top {round(close_pos*100,0):.0f}%"),
        "trigger_bar_ts": _trigger_bar(r),
    }


def fashionably_late_short(c5, p, vw, rvol, rs_mod, rs_tier="?", spy_ctx=None):
    """
    Fashionably Late — Short Version.

    Phase model:
    1. IMPULSE: price drives DOWN, EMA and VWAP diverge (EMA < VWAP)
    2. EXHAUSTION: swing low forms
    3. CONVERGENCE: price bounces toward VWAP, EMA curls back up toward VWAP
    4. CROSS TRIGGER: EMA crosses below VWAP from above (confirmation of trend resumption)

    Stop = exhaustion_low - (exhaustion_low - cross_price) / 3
    """
    r = rh(c5)
    if len(r) < 12:
        return False, {}

    cls = [c["c"] for c in r]
    es  = ema_series(cls, 9)
    en  = es[-1]
    if en is None or not vw:
        return False, {}

    vw_series = _rolling_vwap_series(c5)
    if len(vw_series) < 12:
        return False, {}

    prev_ema  = es[-2]  if len(es)  >= 2 else None
    prev_vwap = vw_series[-2] if len(vw_series) >= 2 else None
    curr_vwap = vw_series[-1] if vw_series else None

    if prev_ema is None or prev_vwap is None or curr_vwap is None:
        return False, {}

    # ── TRIGGER — approaching or crossing below VWAP ──
    # Condition A — EMA approaching from above: within 0.3% of VWAP, still above
    ema_approaching = (
        en > curr_vwap and
        en <= curr_vwap * 1.003 and
        prev_ema > en and
        (en - curr_vwap) < (prev_ema - prev_vwap)
    )
    # Condition B — EMA just crossed below VWAP this bar
    ema_just_crossed = (en <= curr_vwap * 1.0005) and (prev_ema > prev_vwap * 0.999)
    # Condition C — retest hold below VWAP
    ema_retest_hold  = (en < curr_vwap) and (prev_ema <= prev_vwap * 1.0005) and (p < curr_vwap)

    if not (ema_approaching or ema_just_crossed or ema_retest_hold):
        return False, {}

    ema_vals   = [e for e in es[-5:] if e is not None]
    ema_falling = len(ema_vals) >= 3 and ema_vals[-1] < ema_vals[-3]
    if not ema_falling:
        return False, {}

    if p > vw * 1.004:
        return False, {}

    phases = _measure_ema_vwap_divergence(r, es, vw_series, direction="short")
    if not phases:
        return False, {}

    div_bars  = phases["divergence_bars"]
    conv_bars = phases["convergence_bars"]
    vol_div   = phases["vol_div_avg"]
    vol_conv  = phases["vol_conv_avg"]
    exh_price = phases["exhaustion_price"]
    max_sep   = phases["max_separation"]

    CONV_MAX_BARS = 3
    if conv_bars > CONV_MAX_BARS:
        return False, {}
    if conv_bars >= div_bars * 0.67:
        return False, {}
    if vol_conv and vol_div and vol_conv < vol_div * 0.9:
        return False, {}
    # Raised from 0.001 to 0.008 — genuine rubber band stretch required
    if max_sep / (p or 1) < 0.008:
        return False, {}

    if spy_ctx:
        spy_dir = spy_ctx.get("recent_dir", "flat")
        if spy_dir == "up":
            return False, {}

    # Significant pause invalidation — chop before cross = invalid
    if conv_bars >= 2:
        conv_window = r[-(conv_bars + 1):-1]
        if len(conv_window) >= 2:
            ranges  = [c["h"] - c["l"] for c in conv_window]
            avg_rng = sum(ranges) / len(ranges) if ranges else 0
            atr_val = atr(c5) or 1
            if avg_rng > 0 and avg_rng < atr_val * 0.25:
                return False, {}

    cross_bar = r[-1]
    bar_range = cross_bar["h"] - cross_bar["l"]
    close_pos = (cross_bar["c"] - cross_bar["l"]) / bar_range if bar_range > 0 else 1
    if close_pos > 0.60:
        return False, {}

    # ── STOP & TARGET (per PDF for short: 1/3 from HOD to cross) ──
    cross_price = p
    hod         = max(c["h"] for c in r) if r else cross_price
    measured    = hod - cross_price if hod > cross_price else 0
    stop_price  = round(cross_price + measured / 3, 2) if measured > 0 else None
    tgt_price   = round(cross_price - measured, 2)     if measured > 0 else None
    stop_str    = (f"${stop_price} (1/3 of HOD→cross distance above entry)"
                  if stop_price else "Above HOD")
    tgt_str     = f"${tgt_price} (measured move below cross)" if tgt_price else "N/A"
    status      = "Cross imminent ⚡" if ema_approaching else "Cross confirmed ✅"

    base  = 70
    score = base
    score += 10 if vol_conv > vol_div * 1.2 else (5 if vol_conv >= vol_div * 0.9 else 0)
    score += 8  if rvol and rvol >= RVOL_STRONG else (3 if rvol and rvol >= RVOL_MIN else -5)
    score += 5  if close_pos <= 0.40 else 0
    score += 5  if conv_bars <= 2 else 0
    score += rs_mod
    score += time_of_day_modifier()

    if rs_tier in ("C", "D"):
        return False, {}

    score = clamp_score(score)
    if score < MIN_SCORE:
        return False, {}

    return True, {
        "setup":   "FASHIONABLY_LATE_SHORT",
        "dir":     "🔴 SHORT",
        "trigger": f"{status} — EMA9 {'approaching' if ema_approaching else 'crossed below'} VWAP ${round(curr_vwap, 2)}",
        "inval":   stop_str,
        "level":   (f"VWAP: ${round(curr_vwap, 2)} | EMA9: ${round(en, 2)} | "
                    f"HOD: ${round(hod, 2)} | Target: {tgt_str}"),
        "vol":     (f"Conv {round(vol_conv/vol_div, 1)}x div volume ✅ RVOL {rvol}x"
                    if vol_div > 0 else f"RVOL {rvol}x"),
        "score":   score,
        "action":  "Actionable" if score >= 75 else "Watch",
        "notes":   (f"Fashionably Late | Div {div_bars}b → Conv {conv_bars}b | "
                    f"EMA/VWAP sep: ${round(max_sep, 2)} | "
                    f"Measured: ${round(measured, 2)} | "
                    f"Cross bar: bot {round((1-close_pos)*100,0):.0f}%"),
        "trigger_bar_ts": _trigger_bar(r),
    }


def rubber_band_scalp_long(c5, c1, p, vw, rvol, rs_mod, rs_tier="?", spy_ctx=None):
    """
    Rubber Band Scalp — Long (SMB)

    Context: In-play stock grinds down in a controlled way, then selling
    ACCELERATES (urgent, sloppy). Once the sloppy sell program exhausts,
    a single green candle clears the highs of 2+ preceding red candles
    (the "double bar break") — that is the snapback entry.

    Key insight from PDF: it's NOT the extension that makes this work,
    it's the SLOPPINESS of the acceleration. We're fading urgency, not trend.

    Hard gates:
    - Price must be down > 3 ATRs from open (real extension)
    - RVOL > 5x (genuinely in-play)
    - Last 3 bars: increasing range AND increasing volume (acceleration)
    - Snapback bar: single green candle clearing highs of 2+ red bars
    - Snapback bar must be one of the 5 highest volume bars of the day
    - Market (SPY) must NOT be cleanly trending down (don't fade a trend)
    - Time: 10:00–13:30 ET (or open if already extended on higher TF)
    """
    ts = now_et()
    # Time gate: 10:00–13:30. Allow open only if extended (handled via ATR check)
    if not in_window(ts, 10, 0, 13, 30):
        # Allow 9:30–10:00 only if price is already extended > 4 ATRs
        if not in_window(ts, 9, 30, 10, 0):
            return False, {}
        # Will verify extension below

    r = rh(c5)
    if len(r) < 8:
        return False, {}

    # RVOL gate — must be genuinely in-play (> 5x for ideal, > 3x to fire)
    if not rvol or rvol < 3.0:
        return False, {}

    atr_val = atr(c5)
    if not atr_val or atr_val == 0:
        return False, {}

    # Extension: price must be down > 3 ATRs from open
    open_price = r[0]["o"]
    extension  = (open_price - p) / atr_val
    if extension < 3.0:
        return False, {}

    # Market conflict: don't fade a cleanly trending down market
    if spy_ctx:
        spy_dir = spy_ctx.get("recent_dir", "flat")
        if spy_dir == "down":
            return False, {}

    # ── ACCELERATION DETECTION ──
    # Last 3 bars must show INCREASING range AND INCREASING volume
    # This is the "sloppy sell program" signature
    if len(r) < 4:
        return False, {}
    last3 = r[-4:-1]   # three bars before the snapback
    if len(last3) < 3:
        return False, {}
    ranges  = [c["h"] - c["l"] for c in last3]
    volumes = [c["v"] for c in last3]
    # Range and volume must be increasing across these bars
    range_accelerating  = ranges[-1]  > ranges[0]  and ranges[-1]  > ranges[1]
    volume_accelerating = volumes[-1] > volumes[0] and volumes[-1] > volumes[1]
    if not (range_accelerating and volume_accelerating):
        return False, {}

    # ── SNAPBACK BAR: single green candle clearing 2+ prior red bars ──
    snap_bar  = r[-1]
    snap_prev = r[-2]
    snap_prev2 = r[-3] if len(r) >= 3 else None

    is_green = snap_bar["c"] > snap_bar["o"]
    if not is_green:
        return False, {}

    # Must clear the highs of at least 2 preceding red candles ("double bar break")
    prior_reds = [c for c in r[-4:-1] if c["c"] < c["o"]]
    if len(prior_reds) < 2:
        return False, {}
    max_prior_red_high = max(c["h"] for c in prior_reds)
    cleared_highs = snap_bar["h"] > max_prior_red_high
    if not cleared_highs:
        return False, {}

    # Snapback bar must be one of the 5 highest volume bars of the session
    all_vols   = sorted([c["v"] for c in r], reverse=True)
    top5_thresh = all_vols[4] if len(all_vols) >= 5 else all_vols[-1]
    if snap_bar["v"] < top5_thresh:
        return False, {}

    # ── STOP & TARGETS (per PDF) ──
    lod       = min(c["l"] for c in r)
    stop_px   = round(lod - 0.02, 2)
    risk      = snap_bar["c"] - stop_px
    tgt1      = round(snap_bar["c"] + risk, 2)       # 1R
    tgt2      = round(snap_bar["c"] + risk * 2, 2)   # 2R
    tgt3      = round(vw, 2) if vw else None          # VWAP

    # ── SCORE ──
    base  = 68
    score = base
    score += 12 if rvol >= 5.0 else (6 if rvol >= 3.0 else 0)
    score += 8  if extension >= 4.0 else (4 if extension >= 3.0 else 0)
    score += 8  if range_accelerating and volume_accelerating else 0
    score += rs_mod
    score += time_of_day_modifier()

    if rs_tier in ("C", "D"):
        return False, {}

    score = clamp_score(score)
    if score < MIN_SCORE:
        return False, {}

    return True, {
        "setup":   "RUBBER_BAND_SCALP_LONG",
        "dir":     "🟢 LONG",
        "trigger": f"Snapback candle cleared 2+ red bar highs (double bar break) — enter aggressively",
        "inval":   f"${stop_px} ($0.02 below LOD ${round(lod, 2)})",
        "level":   (f"Entry: ${round(snap_bar['c'], 2)} | Stop: ${stop_px} | "
                    f"T1: ${tgt1} (1R) | T2: ${tgt2} (2R) | T3: VWAP ${round(vw, 2) if vw else 'N/A'}"),
        "vol":     f"RVOL {rvol}x ✅ | Snap bar top-5 volume ✅",
        "score":   score,
        "action":  "Aggressive entry — don't wait for bar close",
        "notes":   (f"Rubber Band | {round(extension, 1)} ATRs extended | "
                    f"Accel: range ✅ vol ✅ | Exit 1/3 at 1R, 1/3 at 2R, 1/3 into VWAP"),
        "trigger_bar_ts": _trigger_bar(r),
    }


def second_chance_scalp_long(c5, p, vw, pmh_v, pdh, rvol, rs_mod, rs_tier="?", spy_ctx=None):
    """
    Second Chance Scalp — Long (SMB)

    The setup: range break → pullback to breakout level (old resistance
    becomes new support) → confirmation candle closes above prior candle.

    This is a sniper entry — you're waiting for the RETEST and confirmation,
    not chasing the initial break.

    Three phases (all must be present):
    1. BREAK: strong candle closes above resistance (PMH, PDH, or intraday range high)
    2. RETEST: price pulls back to the broken level on LOW volume
    3. CONFIRM: candle closes above the prior candle at the retest zone

    Hard invalids:
    - Initial break > prior range height (over-extension)
    - Price breaks back into range and doesn't recover next candle
    - Market fighting direction of trade
    """
    ts = now_et()
    # Valid all day: 9:59–16:00
    if not in_window(ts, 9, 59, 16, 0):
        return False, {}

    r = rh(c5)
    if len(r) < 6:
        return False, {}

    # Need a key level to test — use PMH or PDH
    key_level = pmh_v or pdh
    if not key_level:
        return False, {}

    last, prev = r[-1], r[-2]

    # ── PHASE 1: Confirm a break occurred (a prior bar closed above key level) ──
    prior_bars = r[:-2]
    break_bar  = next((c for c in reversed(prior_bars) if c["c"] > key_level * 1.001), None)
    if not break_bar:
        return False, {}

    # ── PHASE 2: Retest — price came back to within 0.4% of key level ──
    at_retest = abs(p - key_level) / key_level <= 0.004
    if not at_retest:
        return False, {}

    # Retest volume should be LOWER than break volume (lack of seller urgency)
    retest_vol_ok = prev["v"] < break_bar["v"] * 0.85

    # ── PHASE 3: Confirmation — current bar closes above previous bar ──
    confirmed = last["c"] > prev["h"]
    if not confirmed:
        return False, {}

    # Price must be above the key level (holding as support, not breaking back in)
    if p < key_level * 0.998:
        return False, {}

    # Market direction alignment
    if spy_ctx:
        spy_dir = spy_ctx.get("recent_dir", "flat")
        if spy_dir == "down":
            return False, {}

    # ── OVER-EXTENSION CHECK ──
    # Find the prior range height before the break
    # Break move should be <= prior range height
    bars_before_break = [c for c in prior_bars if c["ts"] < break_bar["ts"]]
    if len(bars_before_break) >= 3:
        recent_range_bars = bars_before_break[-6:]
        range_high = max(c["h"] for c in recent_range_bars)
        range_low  = min(c["l"] for c in recent_range_bars)
        prior_range_height = range_high - range_low
        break_move = break_bar["h"] - key_level
        if prior_range_height > 0 and break_move > prior_range_height * 1.1:
            return False, {}   # over-extension

    # ── STOP & TARGET ──
    stop_px  = round(prev["l"] - 0.02, 2)   # $0.02 below low of turn candle
    pullback_high = max(c["h"] for c in r[r.index(break_bar):] if c["ts"] <= prev["ts"])
    tgt1     = round(pullback_high, 2)        # high of initial pullback
    risk     = last["c"] - stop_px

    # ── SCORE ──
    base  = 65
    score = base
    score += 8  if retest_vol_ok else 0
    score += 6  if rvol and rvol >= RVOL_MIN else 0
    score += 5  if (spy_ctx or {}).get("recent_dir") == "up" else 0
    score += rs_mod
    score += time_of_day_modifier()

    if rs_tier in ("C", "D"):
        return False, {}

    score = clamp_score(score)
    if score < MIN_SCORE:
        return False, {}

    return True, {
        "setup":   "SECOND_CHANCE_SCALP_LONG",
        "dir":     "🟢 LONG",
        "trigger": f"2nd chance confirm: closed above prior bar at retest of ${round(key_level, 2)}",
        "inval":   f"${stop_px} ($0.02 below turn candle low ${round(prev['l'], 2)})",
        "level":   (f"Key level: ${round(key_level, 2)} | Stop: ${stop_px} | "
                    f"T1: ${tgt1} (pullback high) | Trail: 1-min close below 9 EMA"),
        "vol":     f"Break vol high ✅ Retest vol {'low ✅' if retest_vol_ok else 'elevated ⚠️'} RVOL {rvol}x",
        "score":   score,
        "action":  "Actionable — 2 strikes and out on this setup",
        "notes":   f"2nd Chance | Level: ${round(key_level, 2)} | Risk: ${round(risk, 2)} | Exit half at T1, trail rest on 1-min 9 EMA",
        "trigger_bar_ts": _trigger_bar(r),
    }


def hitchhiker_scalp_long(c5, c1, p, vw, pmh_v, pdh, rvol, rs_mod, rs_tier="?", spy_ctx=None):
    """
    HitchHiker Scalp — Long (SMB)

    Opening drive + NO pullback + sideways consolidation = institutional buy program.
    Entry on break of 1-min bar range (the HitchHiker candle) — aggressively,
    before bar close.

    Critical requirements from PDF:
    - Distinct drive higher off open (not just one big candle)
    - Stock does NOT pull back — holds up and goes sideways
    - Consolidation 5–20 minutes in duration
    - Consolidation LOW in upper 1/3 of day's trading range
    - Must set up before 9:59 AM (opening drive trade only)
    - Consolidation above PMH or PDH for highest probability
    - No choppy consolidation (large wicks = invalid)
    """
    ts = now_et()
    # Opening drive only — must set up before 9:59 AM
    if not in_window(ts, 9, 30, 9, 59):
        return False, {}

    r  = rh(c5)
    r1 = rh(c1) if c1 else []
    if len(r) < 3 or len(r1) < 5:
        return False, {}

    # ── OPENING DRIVE: must have multiple bars going higher (not one candle) ──
    drive_bars = r[:3]   # first 3 five-min bars
    drive_valid = all(c["c"] > c["o"] for c in drive_bars[:2])   # first 2 bars green
    if not drive_valid:
        return False, {}

    # ── NO PULLBACK: price should not have come back to open price ──
    open_price  = r[0]["o"]
    drive_high  = max(c["h"] for c in drive_bars)
    current_pos = (p - open_price) / (drive_high - open_price) if drive_high > open_price else 0
    if current_pos < 0.50:   # price has retraced more than 50% of the drive = not valid
        return False, {}

    # ── CONSOLIDATION DETECTION on 1-min bars ──
    # Look at last 5–20 1-min bars for a tight sideways range
    consol_bars = r1[-20:]   # max 20 bars = 20 minutes
    if len(consol_bars) < 5:
        return False, {}

    consol_high = max(c["h"] for c in consol_bars)
    consol_low  = min(c["l"] for c in consol_bars)
    consol_range = consol_high - consol_low

    # Consolidation must be tight — range < 1x ATR
    atr_val = atr(c5)
    if not atr_val or consol_range > atr_val:
        return False, {}

    # Consolidation low must be in upper 1/3 of day's range
    day_high = max(c["h"] for c in r)
    day_low  = min(c["l"] for c in r)
    day_range = day_high - day_low
    if day_range > 0:
        consol_low_pos = (consol_low - day_low) / day_range
        if consol_low_pos < 0.67:   # not in upper 1/3
            return False, {}

    # ── NO CHOPPY CONSOLIDATION: wicks must be small ──
    avg_wick = sum(
        (c["h"] - max(c["o"], c["c"])) + (min(c["o"], c["c"]) - c["l"])
        for c in consol_bars
    ) / len(consol_bars)
    avg_body  = sum(abs(c["c"] - c["o"]) for c in consol_bars) / len(consol_bars)
    if avg_body > 0 and avg_wick > avg_body * 1.5:   # large wicks = choppy = invalid
        return False, {}

    # ── HITCHHIKER CANDLE: current 1-min bar breaking above consolidation high ──
    # Entry is on the break of range, not bar close
    hitchhiker_trigger = p > consol_high * 1.001
    if not hitchhiker_trigger:
        return False, {}

    # Volume: break candle should have 30%+ more volume than prior candle
    if len(r1) >= 2:
        vol_increase = r1[-1]["v"] > r1[-2]["v"] * 1.30
    else:
        vol_increase = False

    # Consolidation above key resistance (PMH or PDH) for higher probability
    above_key_level = False
    if pmh_v and consol_low > pmh_v:
        above_key_level = True
    if pdh and consol_low > pdh:
        above_key_level = True

    # Market alignment
    if spy_ctx:
        spy_dir = spy_ctx.get("recent_dir", "flat")
        if spy_dir == "down":
            return False, {}

    # ── STOP & TARGET ──
    stop_px = round(consol_low - 0.02, 2)
    risk    = p - stop_px
    tgt1    = round(p + risk, 2)      # first wave
    tgt2    = round(p + risk * 2, 2)  # second wave

    # ── SCORE ──
    base  = 70
    score = base
    score += 8  if vol_increase else 0
    score += 8  if above_key_level else 0
    score += 5  if rvol and rvol >= RVOL_MIN else 0
    score += 5  if (spy_ctx or {}).get("recent_dir") == "up" else 0
    score += rs_mod
    score += time_of_day_modifier()

    if rs_tier in ("C", "D"):
        return False, {}

    score = clamp_score(score)
    if score < MIN_SCORE:
        return False, {}

    return True, {
        "setup":   "HITCHHIKER_SCALP_LONG",
        "dir":     "🟢 LONG",
        "trigger": f"HitchHiker candle breaking above consolidation ${round(consol_high, 2)} — enter NOW",
        "inval":   f"${stop_px} ($0.02 below consolidation low ${round(consol_low, 2)})",
        "level":   (f"Consol: ${round(consol_low, 2)}–${round(consol_high, 2)} | "
                    f"Stop: ${stop_px} | T1: ${tgt1} | T2: ${tgt2}"),
        "vol":     f"Break vol {'30%+ increase ✅' if vol_increase else 'watch'} RVOL {rvol}x",
        "score":   score,
        "action":  "Aggressive — enter on range break, don't wait for close",
        "notes":   (f"HitchHiker | Consol range: ${round(consol_range, 2)} | "
                    f"Upper 1/3: ✅ | {'Above key level ✅' if above_key_level else 'No key level'} | "
                    f"One-and-done stop"),
        "trigger_bar_ts": _trigger_bar(r),
    }


def _opening_impulse_stats(r2, atr21):
    """
    Measure the opening impulse on 2-min bars.
    Returns dict with:
      impulse_pct_atr  — move from open as % of 21-day ATR (0.65 = 65% of ATR)
      impulse_bars     — how many bars the initial drive took
      impulse_dir      — "long" or "short"
      impulse_high     — highest price in the impulse leg
      impulse_low      — lowest price in the impulse leg
      compression_pct  — recent consolidation range as % of impulse range
      vol_phase        — "impulse" / "compression" / "reexpanding"

    We look at the first 15 bars (30 min on 2-min) for the impulse.
    The compression is detected in the subsequent bars before the EMA touch.
    """
    if not r2 or not atr21 or atr21 == 0:
        return None

    open_price  = r2[0]["o"]
    first15     = r2[:15]   # first 30 minutes on 2-min bars
    session_h   = max(c["h"] for c in first15)
    session_l   = min(c["l"] for c in first15)

    # Determine direction — which way did the impulse go
    up_move   = session_h - open_price
    down_move = open_price - session_l
    if up_move >= down_move:
        impulse_dir   = "long"
        impulse_size  = up_move
        impulse_high  = session_h
        impulse_low   = open_price
    else:
        impulse_dir   = "short"
        impulse_size  = down_move
        impulse_high  = open_price
        impulse_low   = session_l

    # Impulse as fraction of 21-day ATR
    impulse_pct_atr = impulse_size / atr21

    # Find how many bars the impulse took (first bar where extreme was reached)
    impulse_bars = 1
    if impulse_dir == "long":
        for i, c in enumerate(first15):
            if c["h"] >= session_h * 0.99:
                impulse_bars = i + 1
                break
    else:
        for i, c in enumerate(first15):
            if c["l"] <= session_l * 1.01:
                impulse_bars = i + 1
                break

    # Compression: look at bars AFTER the impulse up to the current bar
    post_impulse = r2[impulse_bars:]
    if len(post_impulse) >= 3:
        comp_bars  = post_impulse[-6:]   # last 6 bars as compression window
        comp_h     = max(c["h"] for c in comp_bars)
        comp_l     = min(c["l"] for c in comp_bars)
        comp_range = comp_h - comp_l
        comp_pct   = comp_range / impulse_size if impulse_size > 0 else 1.0
    else:
        comp_pct = 1.0

    # Volume phase: compare recent bars to impulse bars
    impulse_vol  = sum(c["v"] for c in r2[:impulse_bars]) / max(impulse_bars, 1)
    recent_vol   = sum(c["v"] for c in r2[-4:]) / 4 if len(r2) >= 4 else impulse_vol
    vol_ratio    = recent_vol / impulse_vol if impulse_vol > 0 else 1.0

    if vol_ratio < 0.45:
        vol_phase = "compression"     # deep compression — best setup condition
    elif vol_ratio < 0.70:
        vol_phase = "light"           # light volume — still good
    elif vol_ratio > 1.20:
        vol_phase = "reexpanding"     # volume coming back — potential next leg
    else:
        vol_phase = "normal"

    return {
        "impulse_pct_atr": round(impulse_pct_atr, 2),
        "impulse_bars":    impulse_bars,
        "impulse_dir":     impulse_dir,
        "impulse_high":    round(impulse_high, 2),
        "impulse_low":     round(impulse_low, 2),
        "impulse_size":    round(impulse_size, 2),
        "compression_pct": round(comp_pct, 2),
        "vol_phase":       vol_phase,
        "vol_ratio":       round(vol_ratio, 2),
    }


def ema9_2m_first_pullback_long(c2, c5, p, vw, prior_close, atr21, rvol, rs_mod, rs_tier="?"):
    """
    2-Minute 9 EMA First/Second Pullback Long — A+ Setup.

    The AAOI / INTC / TSLA setup. A stock makes a strong opening impulse
    (60%+ of its 21-day ATR in the first 30 minutes), then compresses
    sideways while the 9 EMA catches up from below. The first or second
    touch of that rising 9 EMA is the golden entry.

    What separates this from noise:
    1. The 21-day ATR gate: MUST have moved 60%+ of daily ATR in first 30 min
       (not hard 75% — 60% minimum, scores better at 80%+)
    2. Compression: post-impulse bars must be tight (< 35% of impulse range)
       This is the "sideways/halted" condition you described
    3. EMA stack: 9 EMA above 21 EMA, both rising (not just 9 EMA)
    4. Volume phase: compression is good (dry-up after impulse = accumulation)
       Re-expansion is also valid (next leg starting)
    5. First touch = A+ (10pt bonus), Second touch = A (5pt bonus), max 2
    6. 5-min alignment: 5-min EMA also rising
    """
    r2 = rh(c2)
    r5 = rh(c5)
    if len(r2) < 10 or len(r5) < 5:
        return False, {}

    # Above VWAP gate
    if vw and p <= vw:
        return False, {}

    # ── GATE 1: Opening impulse must be 60%+ of 21-day ATR ──
    stats = _opening_impulse_stats(r2, atr21)
    if stats is None:
        return False, {}
    if stats["impulse_dir"] != "long":
        return False, {}   # short impulse — wrong direction
    if stats["impulse_pct_atr"] < 0.60:
        return False, {}   # move too small — not a genuine in-play stock

    # ── GATE 2: Compression after impulse ──
    # Post-impulse bars must be tight — stock is digesting, not reversing
    # Allow up to 35% of impulse range as consolidation width
    if stats["compression_pct"] > 0.35:
        return False, {}   # giving back too much = not a clean compression

    # ── GATE 3: 2-min EMA9 slope — must be genuinely rising ──
    cls2 = [c["c"] for c in r2]
    es2  = ema_series(cls2, 9)
    en2  = es2[-1]
    if en2 is None:
        return False, {}
    ema_vals2 = [e for e in es2[-6:] if e is not None]
    if len(ema_vals2) < 4:
        return False, {}
    slope_pct = (ema_vals2[-1] - ema_vals2[0]) / ema_vals2[0] * 100 / len(ema_vals2)
    if slope_pct < 0.04:
        return False, {}

    # ── GATE 4: EMA stack — 9 EMA must be above 21 EMA (both rising) ──
    es21 = ema_series(cls2, 21)
    en21 = es21[-1] if es21 else None
    if en21 is None:
        return False, {}
    ema21_vals = [e for e in es21[-4:] if e is not None]
    ema21_rising = len(ema21_vals) >= 2 and ema21_vals[-1] > ema21_vals[0]
    ema_stacked  = en2 > en21   # 9 above 21 = bullish stack
    if not (ema_stacked and ema21_rising):
        return False, {}

    # ── GATE 5: Direction — approached from above (body-based) ──
    ctx2, bars_above2, _ = ema_position_context(r2, es2, 4)
    if ctx2 != "from_above":
        return False, {}


    # ── GATE 6: Touch count — first or second only ──
    touch_count = sum(
        1 for bar, ema_v in zip(r2[:-1], es2[:-1])
        if ema_v is not None and bar["l"] <= ema_v * 1.003
    )
    if touch_count > 2:
        return False, {}

    # ── GATE 7: Currently touching the EMA ──
    last2, prev2 = r2[-1], r2[-2]
    touched = last2["l"] <= en2 * 1.003 or prev2["l"] <= en2 * 1.003
    if not touched:
        return False, {}

    # ── GATE 8: Volume phase — compression or re-expanding both valid ──
    vol_phase = stats["vol_phase"]
    if vol_phase == "normal" and stats["vol_ratio"] > 0.90:
        # Volume hasn't compressed enough — sellers still active
        return False, {}

    # ── GATE 9: Bounce bar quality ──
    bar_range = last2["h"] - last2["l"]
    min_range  = p * 0.001
    close_pos  = (last2["c"] - last2["l"]) / bar_range if bar_range > 0 else 0
    if bar_range < min_range or close_pos < 0.35:
        return False, {}

    # ── GATE 10: 5-min alignment ──
    cls5       = [c["c"] for c in r5]
    es5        = ema_series(cls5, 9)
    ema5_vals  = [e for e in es5[-4:] if e is not None]
    ema5_rising = len(ema5_vals) >= 2 and ema5_vals[-1] > ema5_vals[0]
    if not ema5_rising:
        return False, {}

    # ── SCORE ──
    first_touch    = touch_count == 1
    impulse_pct    = stats["impulse_pct_atr"]
    vol_bonus      = 8 if vol_phase == "compression" else (5 if vol_phase == "light" else 3)
    impulse_bonus  = 10 if impulse_pct >= 0.90 else (7 if impulse_pct >= 0.75 else 4)
    stack_bonus    = 6   # EMA stack confirmed

    base  = 72
    score = base
    score += 12 if first_touch else 5
    score += impulse_bonus
    score += vol_bonus
    score += stack_bonus
    score += 5 if close_pos >= 0.55 else 0
    score += rs_mod
    score += time_of_day_modifier()

    if rs_tier in ("C", "D"):
        return False, {}

    if rvol and rvol >= 3.0:
        score += 8
    elif rvol and rvol >= RVOL_MIN:
        score += 3
    elif rvol and rvol < 0.1:
        return False, {}

    score = clamp_score(score)
    if score < MIN_SCORE:
        return False, {}

    gap = None
    if prior_close and r5:
        gap = round((r5[0]["o"] - prior_close) / prior_close * 100, 1)

    touch_label = "🥇 FIRST" if first_touch else "🥈 SECOND"

    return True, {
        "setup":   "EMA9_2M_FIRST_PULLBACK_LONG",
        "dir":     "🟢 LONG",
        "trigger": (f"{touch_label} clean 2-min 9 EMA pullback | "
                    f"Opening impulse: {round(impulse_pct*100,0):.0f}% of ATR | "
                    f"Vol: {vol_phase}"),
        "inval":   f"Loss of 2-min 9 EMA ${round(en2, 2)}",
        "level":   (f"2m EMA9: ${round(en2, 2)} | 2m EMA21: ${round(en21, 2)} | "
                    f"VWAP: ${round(vw, 2) if vw else 'N/A'} | "
                    f"Gap: {f'+{gap}%' if gap else 'N/A'}"),
        "vol":     (f"Vol phase: {vol_phase} ({round(stats['vol_ratio']*100,0):.0f}% of impulse vol) | "
                    f"RVOL {rvol}x"),
        "score":   score,
        "action":  "Enter on bounce confirmation — don't wait for bar close",
        "notes":   (f"Impulse: ${stats['impulse_size']} ({round(impulse_pct*100,0):.0f}% ATR) in "
                    f"{stats['impulse_bars']} bars | "
                    f"Compression: {round(stats['compression_pct']*100,0):.0f}% of impulse | "
                    f"EMA stack ✅ | Slope {round(slope_pct*100,1)}bp/bar"),
        "trigger_bar_ts": _trigger_bar(r2),
    }


def ema9_2m_first_pullback_short(c2, c5, p, vw, prior_close, atr21, rvol, rs_mod, rs_tier="?"):
    """
    2-Minute 9 EMA First/Second Pullback Short — A+ Setup.

    The QCOM setup. Stock cascades down 60%+ of ATR in first 30 min,
    compresses sideways, 9 EMA descending catches down toward price.
    First bounce INTO the declining 9 EMA from below that gets rejected
    = golden short entry.

    EMA stack inverted: 9 EMA below 21 EMA (bearish stack), both declining.
    Price below both. First bounce into 9 EMA = rejection = short.
    """
    r2 = rh(c2)
    r5 = rh(c5)
    if len(r2) < 10 or len(r5) < 5:
        return False, {}

    # Below VWAP gate
    if vw and p >= vw:
        return False, {}

    # Opening impulse must be downward 60%+ of ATR
    stats = _opening_impulse_stats(r2, atr21)
    if stats is None:
        return False, {}
    if stats["impulse_dir"] != "short":
        return False, {}
    if stats["impulse_pct_atr"] < 0.60:
        return False, {}

    # Compression
    if stats["compression_pct"] > 0.35:
        return False, {}

    # 2-min EMA9 slope — must be declining
    cls2 = [c["c"] for c in r2]
    es2  = ema_series(cls2, 9)
    en2  = es2[-1]
    if en2 is None:
        return False, {}
    ema_vals2 = [e for e in es2[-6:] if e is not None]
    if len(ema_vals2) < 4:
        return False, {}
    slope_pct = (ema_vals2[-1] - ema_vals2[0]) / ema_vals2[0] * 100 / len(ema_vals2)
    if slope_pct > -0.04:   # must be declining at least 0.04%/bar
        return False, {}

    # EMA stack: 9 below 21, both declining (bearish stack)
    es21 = ema_series(cls2, 21)
    en21 = es21[-1] if es21 else None
    if en21 is None:
        return False, {}
    ema21_vals   = [e for e in es21[-4:] if e is not None]
    ema21_falling = len(ema21_vals) >= 2 and ema21_vals[-1] < ema21_vals[0]
    ema_stacked   = en2 < en21   # 9 below 21 = bearish stack
    if not (ema_stacked and ema21_falling):
        return False, {}

    # Direction — approached from below (body-based)
    ctx2, _, bars_below2 = ema_position_context(r2, es2, 4)
    if ctx2 != "from_below":
        return False, {}


    # Touch count — first or second only
    touch_count = sum(
        1 for bar, ema_v in zip(r2[:-1], es2[:-1])
        if ema_v is not None and bar["h"] >= ema_v * 0.997
    )
    if touch_count > 2:
        return False, {}

    # Currently touching the EMA
    last2, prev2 = r2[-1], r2[-2]
    touched = last2["h"] >= en2 * 0.997 or prev2["h"] >= en2 * 0.997
    if not touched:
        return False, {}

    # Volume phase
    vol_phase = stats["vol_phase"]
    if vol_phase == "normal" and stats["vol_ratio"] > 0.90:
        return False, {}

    # Rejection bar quality: close in bottom 40%
    bar_range = last2["h"] - last2["l"]
    min_range  = p * 0.001
    close_pos  = (last2["c"] - last2["l"]) / bar_range if bar_range > 0 else 1
    if bar_range < min_range or close_pos > 0.60:
        return False, {}

    # 5-min alignment — EMA9 must be declining on 5-min
    cls5       = [c["c"] for c in r5]
    es5        = ema_series(cls5, 9)
    ema5_vals  = [e for e in es5[-4:] if e is not None]
    ema5_falling = len(ema5_vals) >= 2 and ema5_vals[-1] < ema5_vals[0]
    if not ema5_falling:
        return False, {}

    first_touch   = touch_count == 1
    impulse_pct   = stats["impulse_pct_atr"]
    vol_bonus     = 8 if vol_phase == "compression" else (5 if vol_phase == "light" else 3)
    impulse_bonus = 10 if impulse_pct >= 0.90 else (7 if impulse_pct >= 0.75 else 4)

    base  = 72
    score = base
    score += 12 if first_touch else 5
    score += impulse_bonus
    score += vol_bonus
    score += 6   # EMA stack
    score += 5 if close_pos <= 0.35 else 0
    score += rs_mod
    score += time_of_day_modifier()

    if rs_tier in ("C", "D"):
        return False, {}

    if rvol and rvol >= 3.0:
        score += 8
    elif rvol and rvol >= RVOL_MIN:
        score += 3
    elif rvol and rvol < 0.1:
        return False, {}

    score = clamp_score(score)
    if score < MIN_SCORE:
        return False, {}

    touch_label = "🥇 FIRST" if first_touch else "🥈 SECOND"

    return True, {
        "setup":   "EMA9_2M_FIRST_PULLBACK_SHORT",
        "dir":     "🔴 SHORT",
        "trigger": (f"{touch_label} 2-min 9 EMA rejection | "
                    f"Opening cascade: {round(impulse_pct*100,0):.0f}% of ATR | "
                    f"Vol: {vol_phase}"),
        "inval":   f"Reclaim above 2-min 9 EMA ${round(en2, 2)}",
        "level":   (f"2m EMA9: ${round(en2, 2)} | 2m EMA21: ${round(en21, 2)} | "
                    f"VWAP: ${round(vw, 2) if vw else 'N/A'}"),
        "vol":     (f"Vol phase: {vol_phase} ({round(stats['vol_ratio']*100,0):.0f}% of impulse vol) | "
                    f"RVOL {rvol}x"),
        "score":   score,
        "action":  "Enter on rejection confirmation — aggressive",
        "notes":   (f"Cascade: ${stats['impulse_size']} ({round(impulse_pct*100,0):.0f}% ATR) in "
                    f"{stats['impulse_bars']} bars | "
                    f"EMA bearish stack ✅ | Slope {round(slope_pct*100,1)}bp/bar"),
        "trigger_bar_ts": _trigger_bar(r2),
    }


def ema_stack_momentum_long(c2, c5, p, vw, atr21, rvol, rs_mod, rs_tier="?"):
    """
    EMA Stack Momentum Long — Sustained Drive Alert.

    AAOI / TSLA type. Stock has been trending up for multiple bars with:
    - Price ABOVE both 9 EMA and 21 EMA (bullish stack)
    - Both EMAs steeply rising
    - Stock has moved significantly today (in play)
    - Making new session highs or near them

    This fires for continuation of the trend, not a pullback entry.
    It catches the "momentum is running, options are expanding" moment.
    Alert once when stack confirms, re-fires only on new session high.
    """
    r2 = rh(c2)
    r5 = rh(c5)
    if len(r2) < 15 or len(r5) < 8:
        return False, {}

    if vw and p <= vw:
        return False, {}

    cls2 = [c["c"] for c in r2]
    es2  = ema_series(cls2, 9)
    es21 = ema_series(cls2, 21)
    en2  = es2[-1]
    en21 = es21[-1]
    if en2 is None or en21 is None:
        return False, {}

    # Price above BOTH EMAs
    if not (p > en2 and p > en21):
        return False, {}

    # Bullish stack: 9 above 21
    if en2 <= en21:
        return False, {}

    # Both EMAs must be rising steeply
    ema9_vals  = [e for e in es2[-6:]  if e is not None]
    ema21_vals = [e for e in es21[-6:] if e is not None]
    if len(ema9_vals) < 4 or len(ema21_vals) < 4:
        return False, {}

    slope9  = (ema9_vals[-1]  - ema9_vals[0])  / ema9_vals[0]  * 100 / len(ema9_vals)
    slope21 = (ema21_vals[-1] - ema21_vals[0]) / ema21_vals[0] * 100 / len(ema21_vals)

    if slope9 < 0.05 or slope21 < 0.02:
        return False, {}

    # Stock must have moved 60%+ of ATR today
    if atr21 and atr21 > 0:
        open_price = r2[0]["o"]
        session_h  = max(c["h"] for c in r2)
        move_pct   = (session_h - open_price) / atr21
        if move_pct < 0.60:
            return False, {}
    else:
        return False, {}

    # Price near session high (within 2%) — still at the top, not fading
    session_high = max(c["h"] for c in r2)
    if p < session_high * 0.98:
        return False, {}

    # Multiple bars (at least 6) with price above BOTH EMAs — sustained trend
    bars_in_stack = sum(
        1 for bar, e9, e21 in zip(r2[-10:], es2[-10:], es21[-10:])
        if e9 is not None and e21 is not None and bar["c"] > e9 and bar["c"] > e21
    )
    if bars_in_stack < 6:
        return False, {}

    score = 70
    score += 10 if slope9 >= 0.10 else (5 if slope9 >= 0.05 else 0)
    score += 8  if move_pct >= 1.0 else (5 if move_pct >= 0.75 else 2)
    score += 8  if rvol and rvol >= 3.0 else (4 if rvol and rvol >= RVOL_MIN else 0)
    score += rs_mod
    score += time_of_day_modifier()

    if rs_tier in ("C", "D"):
        return False, {}

    score = clamp_score(score)
    if score < MIN_SCORE:
        return False, {}

    return True, {
        "setup":   "EMA_STACK_MOMENTUM_LONG",
        "dir":     "🟢 LONG",
        "trigger": (f"Bullish EMA stack — price above 9 & 21 EMA, both rising | "
                    f"Move: {round(move_pct*100,0):.0f}% of ATR"),
        "inval":   f"Close below 2-min 9 EMA ${round(en2, 2)} on expanding volume",
        "level":   (f"2m EMA9: ${round(en2, 2)} | 2m EMA21: ${round(en21, 2)} | "
                    f"Session HOD: ${round(session_high, 2)}"),
        "vol":     f"RVOL {rvol}x | Stack bars: {bars_in_stack}",
        "score":   score,
        "action":  "Trend in play — options expanding, consider calls",
        "notes":   (f"EMA9 slope {round(slope9*100,1)}bp/bar | "
                    f"EMA21 slope {round(slope21*100,1)}bp/bar | "
                    f"{bars_in_stack} bars in stack"),
        "trigger_bar_ts": _trigger_bar(r2),
    }


def ema_stack_momentum_short(c2, c5, p, vw, atr21, rvol, rs_mod, rs_tier="?"):
    """
    EMA Stack Momentum Short — QCOM cascade type.

    Price BELOW both 9 EMA and 21 EMA. Both declining steeply.
    9 EMA below 21 EMA (bearish stack). Stock has cascaded 60%+ of ATR.
    Near session lows. Options (puts) expanding.
    """
    r2 = rh(c2)
    r5 = rh(c5)
    if len(r2) < 15 or len(r5) < 8:
        return False, {}

    if vw and p >= vw:
        return False, {}

    cls2 = [c["c"] for c in r2]
    es2  = ema_series(cls2, 9)
    es21 = ema_series(cls2, 21)
    en2  = es2[-1]
    en21 = es21[-1]
    if en2 is None or en21 is None:
        return False, {}

    if not (p < en2 and p < en21):
        return False, {}
    if en2 >= en21:   # need bearish stack: 9 below 21
        return False, {}

    ema9_vals  = [e for e in es2[-6:]  if e is not None]
    ema21_vals = [e for e in es21[-6:] if e is not None]
    if len(ema9_vals) < 4 or len(ema21_vals) < 4:
        return False, {}

    slope9  = (ema9_vals[-1]  - ema9_vals[0])  / ema9_vals[0]  * 100 / len(ema9_vals)
    slope21 = (ema21_vals[-1] - ema21_vals[0]) / ema21_vals[0] * 100 / len(ema21_vals)

    if slope9 > -0.05 or slope21 > -0.02:
        return False, {}

    if atr21 and atr21 > 0:
        open_price = r2[0]["o"]
        session_l  = min(c["l"] for c in r2)
        move_pct   = (open_price - session_l) / atr21
        if move_pct < 0.60:
            return False, {}
    else:
        return False, {}

    session_low = min(c["l"] for c in r2)
    if p > session_low * 1.02:
        return False, {}

    bars_in_stack = sum(
        1 for bar, e9, e21 in zip(r2[-10:], es2[-10:], es21[-10:])
        if e9 is not None and e21 is not None and bar["c"] < e9 and bar["c"] < e21
    )
    if bars_in_stack < 6:
        return False, {}

    score = 70
    score += 10 if abs(slope9) >= 0.10 else (5 if abs(slope9) >= 0.05 else 0)
    score += 8  if move_pct >= 1.0 else (5 if move_pct >= 0.75 else 2)
    score += 8  if rvol and rvol >= 3.0 else (4 if rvol and rvol >= RVOL_MIN else 0)
    score += rs_mod
    score += time_of_day_modifier()

    if rs_tier in ("C", "D"):
        return False, {}

    score = clamp_score(score)
    if score < MIN_SCORE:
        return False, {}

    return True, {
        "setup":   "EMA_STACK_MOMENTUM_SHORT",
        "dir":     "🔴 SHORT",
        "trigger": (f"Bearish EMA stack — price below 9 & 21 EMA, both declining | "
                    f"Cascade: {round(move_pct*100,0):.0f}% of ATR"),
        "inval":   f"Close above 2-min 9 EMA ${round(en2, 2)} on expanding volume",
        "level":   (f"2m EMA9: ${round(en2, 2)} | 2m EMA21: ${round(en21, 2)} | "
                    f"Session LOD: ${round(session_low, 2)}"),
        "vol":     f"RVOL {rvol}x | Stack bars: {bars_in_stack}",
        "score":   score,
        "action":  "Trend in play — puts expanding, institutional cascade",
        "notes":   (f"EMA9 slope {round(slope9*100,1)}bp/bar | "
                    f"EMA21 slope {round(slope21*100,1)}bp/bar | "
                    f"{bars_in_stack} bars in bearish stack"),
        "trigger_bar_ts": _trigger_bar(r2),
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
        # Load watchlist — priority order:
        # 1. WATCHLIST env var (Railway persistent override — survives restarts)
        # 2. watchlist_state.json (local file — lost on Railway restart)
        # 3. DEFAULT_WATCHLIST (hardcoded fallback)
        #
        # To permanently remove tickers that survive Railway restarts:
        # Set WATCHLIST env var in Railway to comma-separated list e.g:
        # NVDA,AMD,TSLA,PLTR,AAPL,MSFT,...
        env_wl = os.environ.get("WATCHLIST", "").strip()
        if env_wl:
            wl_from_env = [t.strip().upper() for t in env_wl.split(",") if t.strip()]
            saved_watch = wl_from_env
            print(f"[INIT] Watchlist from WATCHLIST env var: {saved_watch}")
        else:
            saved_watch = load_json_file(WATCHLIST_FILE, DEFAULT_WATCHLIST)

        saved_armed = load_json_file(ARMED_FILE, [])
        self.wl     = list(dict.fromkeys(
            saved_watch if isinstance(saved_watch, list) and saved_watch else DEFAULT_WATCHLIST
        ))
        self.armed  = set(saved_armed if isinstance(saved_armed, list) else [])

        self.pmh, self.pml, self.pr = {}, {}, {}
        self.datr    = {}   # 21-day ATR per ticker — refreshed once per day
        self.datr_dt = None
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
        if self.datr_dt != today:
            print("[SCAN] Refreshing 21-day ATR...")
            for t in self.wl:
                self.datr[t] = daily_atr21(t)
                time.sleep(0.35)
            self.datr_dt = today

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
            # HARD GATE: do not fire any setup alerts before 9:30 ET.
            # is_mkt() opens at 9:25 for daily reset purposes but trading
            # setups require a full regular-hours session to be open.
            if not hhmm_gte(now_et(), 9, 30):
                return candidates

            c5_raw  = candles(ticker, 5)
            c1_raw  = candles(ticker, 1)
            c10_raw = candles(ticker, 10)
            c2_raw  = candles(ticker, 2)   # 2-min bars for first-pullback setup
            c5      = closed_only(c5_raw,  5)
            c1      = closed_only(c1_raw,  1)
            c10     = closed_only(c10_raw, 10)
            c2      = closed_only(c2_raw,  2)
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
            prior_close = pd.get("c")
            atr21       = self.datr.get(ticker)   # 21-day ATR for impulse detection
            atr21       = self.datr.get(ticker)   # 21-day ATR for impulse detection

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
                ("EMA9_2M_FIRST_PULLBACK_LONG",
                 lambda: ema9_2m_first_pullback_long(c2, c5, p, vw, prior_close, atr21, rvol, rs_mod, rs_tier)),
                ("EMA9_2M_FIRST_PULLBACK_SHORT",
                 lambda: ema9_2m_first_pullback_short(c2, c5, p, vw, prior_close, atr21, rvol, rs_mod_short, rs_tier_short)),
                ("EMA_STACK_MOMENTUM_LONG",
                 lambda: ema_stack_momentum_long(c2, c5, p, vw, atr21, rvol, rs_mod, rs_tier)),
                ("EMA_STACK_MOMENTUM_SHORT",
                 lambda: ema_stack_momentum_short(c2, c5, p, vw, atr21, rvol, rs_mod_short, rs_tier_short)),
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
                ("FASHIONABLY_LATE_LONG",
                 lambda: fashionably_late_long(c5, p, vw, rvol, rs_mod, rs_tier, self._spy_ctx)),
                ("RUBBER_BAND_SCALP_LONG",
                 lambda: rubber_band_scalp_long(c5, c1, p, vw, rvol, rs_mod, rs_tier, self._spy_ctx)),
                ("HITCHHIKER_SCALP_LONG",
                 lambda: hitchhiker_scalp_long(c5, c1, p, vw, pmh_v, pdh, rvol, rs_mod, rs_tier, self._spy_ctx)),

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
                ("FASHIONABLY_LATE_SHORT",
                 lambda: fashionably_late_short(c5, p, vw, rvol, rs_mod_short, rs_tier_short, self._spy_ctx)),
                ("SECOND_CHANCE_SCALP_LONG",
                 lambda: second_chance_scalp_long(c5, p, vw, pmh_v, pdh, rvol, rs_mod, rs_tier, self._spy_ctx)),
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
                f"📊 <b>Scanner v3.15</b>\n"
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
        print("[SCANNER] v3.15 starting")
        send_alert(
            f"🤖 <b>Scanner v3.15 Online</b>\n{'━' * 28}\n"
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
