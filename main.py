"""
main_v3_1.py - Andre's Trading Scanner v3.1
Refactored single-file deployment with cleaner auth, safer IO,
expanded Telegram controls, and new structured pullback/curl setups.
"""
import os
import time
import json
import base64
import threading
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode

import pytz
import requests


# ── ENV / CONFIG ──────────────────────────────────────────────────────────────
SCHWAB_CLIENT_ID = os.environ.get("SCHWAB_CLIENT_ID", "").strip()
SCHWAB_CLIENT_SECRET = os.environ.get("SCHWAB_CLIENT_SECRET", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

ET = pytz.timezone("America/New_York")
AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
REDIRECT = "https://127.0.0.1"
TOKEN_FILE = "schwab_tokens.json"
BASE = "https://api.schwabapi.com/marketdata/v1"

DEFAULT_WATCHLIST = [
    "NVDA", "AMD", "TSLA", "PLTR", "AMZN", "MU", "MSFT", "GOOGL", "AAPL",
    "AVGO", "META", "CVX", "DELL", "RKLB", "MRVL", "ANET", "CRDO", "LITE",
    "COHR", "COIN", "AAOI", "XOM", "ARM", "INTC"
]

MIN_SCORE = 60
COOLDOWN = 15
SCAN_SLEEP_SECONDS = 60
API_PAUSE_SECONDS = 0.35
NEAR_LEVEL_PCT = 0.004
PULLBACK_LIGHT_VOL = 0.85
EXPAND_VOL = 1.25

_pending_auth = False


# ── UTIL ──────────────────────────────────────────────────────────────────────
def now_et() -> datetime:
    return datetime.now(ET)


def send_telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[ALERT]\n{msg}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        ).raise_for_status()
        print(f"[SENT] {now_et().strftime('%H:%M:%S')}")
    except Exception as exc:
        print(f"[TELEGRAM ERR] {exc}")


def safe_round(value, digits=2):
    try:
        return round(float(value), digits)
    except Exception:
        return value


def clamp_score(value: int) -> int:
    return max(0, min(int(value), 100))


def pct_diff(a: float, b: float) -> float:
    if not a or not b:
        return 999.0
    return abs(a - b) / b


# ── TOKEN / AUTH ──────────────────────────────────────────────────────────────
def _b64() -> str:
    raw = f"{SCHWAB_CLIENT_ID}:{SCHWAB_CLIENT_SECRET}".encode()
    return base64.b64encode(raw).decode()


def _save(tokens: dict) -> None:
    tokens = dict(tokens or {})
    tokens["saved_at"] = time.time()
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)


def _load() -> dict:
    if not os.path.exists(TOKEN_FILE):
        return {}
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _expired(tokens: dict) -> bool:
    if not tokens:
        return True
    saved_at = tokens.get("saved_at", 0)
    expires_in = tokens.get("expires_in", 1800)
    return time.time() > saved_at + expires_in - 300


def _refresh(tokens: dict) -> dict:
    if not tokens.get("refresh_token"):
        return {}
    headers = {
        "Authorization": f"Basic {_b64()}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    resp = requests.post(
        TOKEN_URL,
        headers=headers,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
        timeout=20,
    )
    resp.raise_for_status()
    new_tokens = resp.json()
    if "refresh_token" not in new_tokens:
        new_tokens["refresh_token"] = tokens.get("refresh_token")
    _save(new_tokens)
    return new_tokens


def _login():
    global _pending_auth
    _pending_auth = True
    url = (
        f"{AUTH_URL}?"
        f"{urlencode({'response_type': 'code', 'client_id': SCHWAB_CLIENT_ID, 'redirect_uri': REDIRECT, 'scope': 'readonly'})}"
    )
    print(f"[AUTH] Login required. URL: {url}")
    send_telegram(
        "🔐 <b>Schwab Authorization Required</b>\n"
        f"{'━' * 30}\n"
        "<b>1)</b> Open this link:\n"
        f"<code>{url}</code>\n\n"
        "<b>2)</b> Log in and approve\n"
        "<b>3)</b> Copy the full redirect URL from your browser\n"
        "(starts with <code>https://127.0.0.1/?code=...</code>)\n"
        "<b>4)</b> Send it back like:\n"
        "<code>/auth https://127.0.0.1/?code=PASTE_FULL_URL_HERE</code>"
    )
    start = time.time()
    while _pending_auth:
        if time.time() - start > 600:
            send_telegram("⏰ Auth timed out. Use /reauth when ready.")
            return None
        time.sleep(2)
    return _load()


def _complete_auth(full_redirect_url: str) -> bool:
    global _pending_auth
    try:
        parsed = urlparse(full_redirect_url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if not code:
            send_telegram("❌ I could not find an auth code in that URL.")
            return False
        headers = {
            "Authorization": f"Basic {_b64()}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        resp = requests.post(
            TOKEN_URL,
            headers=headers,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
            },
            timeout=20,
        )
        resp.raise_for_status()
        _save(resp.json())
        _pending_auth = False
        print("[AUTH] Tokens saved.")
        send_telegram("✅ <b>Schwab connected successfully.</b>\nScanner auth is now live.")
        return True
    except Exception as exc:
        print(f"[AUTH ERR] {exc}")
        send_telegram(f"❌ Auth failed: {exc}\nUse /reauth to try again.")
        return False


def tok() -> str:
    tokens = _load()
    if not tokens:
        tokens = _login()
        if not tokens:
            return ""
    elif _expired(tokens):
        try:
            tokens = _refresh(tokens)
        except Exception as exc:
            print(f"[TOKEN REFRESH ERR] {exc}")
            return ""
    return tokens.get("access_token", "")


def _headers() -> dict:
    return {"Authorization": f"Bearer {tok()}", "Accept": "application/json"}


def _get(endpoint: str, params=None) -> dict:
    for attempt in range(2):
        try:
            resp = requests.get(
                f"{BASE}{endpoint}",
                headers=_headers(),
                params=params or {},
                timeout=15,
            )
            if resp.status_code == 401 and attempt == 0:
                try:
                    _refresh(_load())
                except Exception as exc:
                    print(f"[401 REFRESH ERR] {exc}")
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            print(f"[DATA ERR] {endpoint} | {exc}")
            if attempt == 0:
                time.sleep(2)
    return {}


# ── MARKET DATA ───────────────────────────────────────────────────────────────
def candles(ticker: str, minutes: int = 5):
    now = now_et()
    start = int(now.replace(hour=4, minute=0, second=0, microsecond=0).timestamp() * 1000)
    end = int(now.timestamp() * 1000)
    data = _get(
        f"/pricehistory?symbol={ticker}",
        {
            "periodType": "day",
            "period": 1,
            "frequencyType": "minute",
            "frequency": minutes,
            "startDate": start,
            "endDate": end,
            "needExtendedHoursData": "true",
        },
    )
    out = []
    for c in data.get("candles", []):
        try:
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
        except Exception:
            continue
    return out


def price(ticker: str):
    try:
        data = _get(f"/quotes/{ticker}")
        quote = data.get(ticker, {}).get("quote", {})
        return quote.get("lastPrice") or quote.get("mark")
    except Exception:
        return None


def premarket_extreme(ticker: str, which: str = "high"):
    try:
        now = now_et()
        start = int(now.replace(hour=4, minute=0, second=0, microsecond=0).timestamp() * 1000)
        end = int(now.replace(hour=9, minute=30, second=0, microsecond=0).timestamp() * 1000)
        data = _get(
            f"/pricehistory?symbol={ticker}",
            {
                "periodType": "day",
                "period": 1,
                "frequencyType": "minute",
                "frequency": 1,
                "startDate": start,
                "endDate": end,
                "needExtendedHoursData": "true",
            },
        )
        vals = [c[which] for c in data.get("candles", []) if which in c]
        if not vals:
            return None
        return max(vals) if which == "high" else min(vals)
    except Exception:
        return None


def prior_day(ticker: str):
    try:
        data = _get(
            f"/pricehistory?symbol={ticker}",
            {
                "periodType": "month",
                "period": 1,
                "frequencyType": "daily",
                "frequency": 1,
                "needExtendedHoursData": "false",
            },
        )
        today = now_et().date()
        previous = None
        for c in data.get("candles", []):
            dt = datetime.fromtimestamp(c["datetime"] / 1000, tz=ET).date()
            if dt < today:
                previous = c
        if previous:
            return {
                "h": previous["high"],
                "l": previous["low"],
                "c": previous["close"],
                "vwap": round((previous["high"] + previous["low"] + previous["close"]) / 3, 2),
            }
    except Exception:
        pass
    return {}


def vwap(cs):
    total_value, total_vol = 0.0, 0.0
    for c in cs:
        ts = c.get("ts")
        if ts and (ts.hour < 9 or (ts.hour == 9 and ts.minute < 30)):
            continue
        tp = (c["h"] + c["l"] + c["c"]) / 3
        total_value += tp * c["v"]
        total_vol += c["v"]
    return total_value / total_vol if total_vol else None


def ema(values, period=9):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(values, period=9):
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    result = [None] * (period - 1)
    e = sum(values[:period]) / period
    result.append(e)
    for v in values[period:]:
        e = v * k + e * (1 - k)
        result.append(e)
    return result


def regular_hours(cs):
    return [
        c for c in cs
        if c.get("ts") and (c["ts"].hour > 9 or (c["ts"].hour == 9 and c["ts"].minute >= 30))
    ]


def avg_volume(cs, n=20):
    bars = regular_hours(cs)
    vols = [c["v"] for c in bars[-n:]]
    return sum(vols) / len(vols) if vols else None


def opening_range_5m(c1):
    bars = [
        c for c in c1
        if c.get("ts") and c["ts"].hour == 9 and 30 <= c["ts"].minute < 35
    ]
    if not bars:
        return None, None
    return max(c["h"] for c in bars), min(c["l"] for c in bars)


# ── SETUP HELPERS ─────────────────────────────────────────────────────────────
def score_pack(setup, direction, trigger, inval, level, vol, notes, score, action="Actionable"):
    return {
        "setup": setup,
        "dir": direction,
        "trigger": trigger,
        "inval": inval,
        "level": level,
        "vol": vol,
        "notes": notes,
        "score": clamp_score(score),
        "action": action,
    }


# ── EXISTING SETUPS (cleaned) ────────────────────────────────────────────────
def orb_long(c5, c1, p, vw, pmh):
    oh, ol = opening_range_5m(c1)
    if not oh:
        return False, {}
    bars = regular_hours(c5)
    if len(bars) < 2:
        return False, {}
    avgv = avg_volume(c5)
    last, prev = bars[-1], bars[-2]
    vol = bool(avgv and last["v"] > avgv * 1.3)
    if not (p > oh and p > (vw or 0) and prev["c"] <= oh):
        return False, {}
    score = 60 + (15 if vol else 0) + (10 if pmh and p > pmh else 0)
    return True, score_pack(
        "ORB_5M_LONG", "🟢 LONG",
        f"Break above OR high ${safe_round(oh)}",
        f"Loss of OR low ${safe_round(ol)}",
        f"5m OR high: ${safe_round(oh)}",
        "Expanding ✅" if vol else "Weak ⚠️",
        "Opening range breakout with confirmation" if vol else "Breakout needs better volume",
        score,
        "Actionable" if vol else "Watch",
    )


def orb_short(c5, c1, p, vw, pml):
    oh, ol = opening_range_5m(c1)
    if not ol:
        return False, {}
    bars = regular_hours(c5)
    if len(bars) < 2:
        return False, {}
    avgv = avg_volume(c5)
    last, prev = bars[-1], bars[-2]
    vol = bool(avgv and last["v"] > avgv * 1.3)
    if not (p < ol and p < (vw or float("inf")) and prev["c"] >= ol):
        return False, {}
    score = 60 + (15 if vol else 0) + (10 if pml and p < pml else 0)
    return True, score_pack(
        "ORB_5M_SHORT", "🔴 SHORT",
        f"Break below OR low ${safe_round(ol)}",
        f"Reclaim above OR high ${safe_round(oh)}",
        f"5m OR low: ${safe_round(ol)}",
        "Expanding ✅" if vol else "Weak ⚠️",
        "Opening range breakdown with confirmation" if vol else "Breakdown needs better volume",
        score,
        "Actionable" if vol else "Watch",
    )


def vwap_reclaim(c5, p, vw):
    if not vw:
        return False, {}
    bars = regular_hours(c5)
    if len(bars) < 4:
        return False, {}
    last, prev = bars[-1], bars[-2]
    if not (last["c"] > vw and last["c"] > last["o"] and prev["c"] < vw):
        return False, {}
    avgv = avg_volume(c5)
    vol = bool(avgv and last["v"] > avgv * 1.2)
    first = any(c["c"] < vw for c in bars[-5:-1])
    if not first:
        return False, {}
    score = 60 + (15 if vol else 0) + 10
    return True, score_pack(
        "VWAP_RECLAIM_LONG", "🟢 LONG",
        f"Hold above VWAP ${safe_round(vw)} and clear local high",
        f"Fail back below VWAP ${safe_round(vw)}",
        f"VWAP: ${safe_round(vw)}",
        "Expanding ✅" if vol else "Light — watch",
        "First clean reclaim after trading below VWAP",
        score,
        "Actionable" if vol else "Watch",
    )


def vwap_reject(c5, p, vw):
    if not vw:
        return False, {}
    bars = regular_hours(c5)
    if len(bars) < 4:
        return False, {}
    last, prev = bars[-1], bars[-2]
    avgv = avg_volume(c5)
    near = pct_diff(prev["h"], vw) <= 0.005
    vol = bool(avgv and last["v"] > avgv * 1.1)
    if not (near and last["c"] < vw and last["c"] < last["o"] and any(c["c"] < vw for c in bars[-6:-3])):
        return False, {}
    score = 65 + (10 if vol else 0)
    return True, score_pack(
        "VWAP_REJECT_SHORT", "🔴 SHORT",
        f"Break local pivot below VWAP ${safe_round(vw)}",
        f"Acceptance above VWAP ${safe_round(vw * 1.003)}",
        f"VWAP resistance: ${safe_round(vw)}",
        "Expanding ✅" if vol else "Light",
        "Rejected at VWAP and rolling over",
        score,
    )


def ema9_pb_long(c5, p, vw):
    bars = regular_hours(c5)
    if len(bars) < 12:
        return False, {}
    closes = [c["c"] for c in bars]
    es = ema_series(closes, 9)
    en = es[-1]
    if not en:
        return False, {}
    ema_vals = [e for e in es[-5:] if e is not None]
    rising = len(ema_vals) > 1 and ema_vals[-1] > ema_vals[0]
    last, prev = bars[-1], bars[-2]
    avgv = avg_volume(c5)
    light_pb = bool(avgv and last["v"] < avgv * PULLBACK_LIGHT_VOL)
    touched = last["l"] <= en * 1.003 or prev["l"] <= en * 1.003
    bouncing = last["c"] > prev["h"] or last["c"] > en
    if not (rising and p > (vw or 0) and touched and bouncing):
        return False, {}
    score = 65 + (10 if light_pb else 0) + (10 if p > (vw or 0) else 0)
    return True, score_pack(
        "EMA9_5M_PULLBACK_LONG", "🟢 LONG",
        f"Bounce above ${safe_round(prev['h'])} after 9 EMA touch",
        f"Clean loss of 9 EMA ${safe_round(en)}",
        f"9 EMA: ${safe_round(en)} | VWAP: ${safe_round(vw)}",
        "Pullback light ✅" if light_pb else "Watch volume",
        "Rising 9 EMA with controlled pullback and bounce",
        score,
    )


def ema9_pb_short(c5, p, vw):
    bars = regular_hours(c5)
    if len(bars) < 12:
        return False, {}
    closes = [c["c"] for c in bars]
    es = ema_series(closes, 9)
    en = es[-1]
    if not en:
        return False, {}
    ema_vals = [e for e in es[-5:] if e is not None]
    falling = len(ema_vals) > 1 and ema_vals[-1] < ema_vals[0]
    last, prev = bars[-1], bars[-2]
    avgv = avg_volume(c5)
    light_bounce = bool(avgv and last["v"] < avgv * PULLBACK_LIGHT_VOL)
    touched = last["h"] >= en * 0.997 or prev["h"] >= en * 0.997
    rejecting = last["c"] < last["o"] and last["c"] < en
    if not (falling and p < (vw or float("inf")) and touched and rejecting):
        return False, {}
    score = 65 + (10 if light_bounce else 0) + 10
    return True, score_pack(
        "EMA9_5M_PULLBACK_SHORT", "🔴 SHORT",
        f"Break below ${safe_round(prev['l'])} after EMA rejection",
        f"Reclaim through 9 EMA ${safe_round(en)}",
        f"9 EMA resistance: ${safe_round(en)}",
        "Bounce light ✅" if light_bounce else "Watch",
        "Falling 9 EMA, weak bounce, rejection",
        score,
    )


def flag_long(c5, p, vw):
    bars = regular_hours(c5)
    if len(bars) < 8:
        return False, {}
    avgv = avg_volume(c5)
    impulse = None
    for c in bars[-10:-3]:
        if (c["c"] - c["o"]) > 0 and avgv and c["v"] > avgv * 1.5:
            impulse = c
            break
    if not impulse:
        return False, {}
    cons = bars[-5:]
    flag_high = max(c["h"] for c in cons)
    flag_low = min(c["l"] for c in cons)
    flag_range = flag_high - flag_low
    impulse_size = impulse["c"] - impulse["o"]
    tight = flag_range < impulse_size * 0.5 if impulse_size > 0 else False
    last = bars[-1]
    breakout = last["c"] > flag_high and last["c"] > bars[-2]["h"]
    dryup = bool(avgv and all(c["v"] < avgv * 0.8 for c in cons[:-1]))
    vol = bool(avgv and last["v"] > avgv * 1.2)
    if not (tight and breakout and p > (vw or 0)):
        return False, {}
    score = 65 + (10 if dryup else 0) + (15 if vol else 0)
    return True, score_pack(
        "FLAG_BREAKOUT_LONG", "🟢 LONG",
        f"Break above flag high ${safe_round(flag_high)}",
        f"Loss of flag low ${safe_round(flag_low)}",
        f"Flag: ${safe_round(flag_low)}–${safe_round(flag_high)}",
        "Dry-up + expansion ✅" if dryup and vol else "Watch volume",
        f"Tight flag after impulse. Range {safe_round(flag_range)} vs impulse {safe_round(impulse_size)}",
        score,
        "Actionable" if vol else "Watch",
    )


def pdh_retest(c5, p, vw, pdh):
    if not pdh:
        return False, {}
    bars = regular_hours(c5)
    if len(bars) < 3:
        return False, {}
    if not any(c["h"] > pdh for c in bars[:-2]):
        return False, {}
    avgv = avg_volume(c5)
    near = pct_diff(p, pdh) <= 0.005
    light = bool(avgv and all(c["v"] < avgv * 0.9 for c in bars[-2:]))
    if not (p >= pdh * 0.998 and p > (vw or 0)):
        return False, {}
    score = 70 + (10 if near else 0) + (10 if light else 0)
    return True, score_pack(
        "PDH_BREAK_RETEST_LONG", "🟢 LONG",
        f"Reclaim above PDH ${safe_round(pdh)} and push",
        f"Loss of ${safe_round(pdh * 0.997)}",
        f"Prior day high: ${safe_round(pdh)}",
        "Pullback light ✅" if light else "Watch",
        "Daily breakout retest at institutional level",
        score,
    )


def pdl_retest(c5, p, vw, pdl):
    if not pdl:
        return False, {}
    bars = regular_hours(c5)
    if len(bars) < 3:
        return False, {}
    if not any(c["l"] < pdl for c in bars[:-2]):
        return False, {}
    avgv = avg_volume(c5)
    near = pct_diff(p, pdl) <= 0.005
    light = bool(avgv and all(c["v"] < avgv * 0.9 for c in bars[-2:]))
    if not (p <= pdl * 1.002 and p < (vw or float("inf"))):
        return False, {}
    score = 70 + (10 if near else 0) + (10 if light else 0)
    return True, score_pack(
        "PDL_RETEST_SHORT", "🔴 SHORT",
        f"Reject under PDL ${safe_round(pdl)} and break low",
        f"Reclaim ${safe_round(pdl * 1.003)}",
        f"Prior day low: ${safe_round(pdl)}",
        "Bounce light ✅" if light else "Watch",
        "Prior day low breakdown retest",
        score,
    )


# ── NEW SETUPS ────────────────────────────────────────────────────────────────
def ema_curl_long(candles_in, timeframe_label, p, vw):
    bars = regular_hours(candles_in)
    if len(bars) < 12:
        return False, {}
    closes = [c["c"] for c in bars]
    es = ema_series(closes, 9)
    if not es[-1]:
        return False, {}
    last = bars[-1]
    avgv = avg_volume(candles_in)
    recent_ema = [e for e in es[-4:] if e is not None]
    recent_bars = bars[-4:]
    pullback_bars = recent_bars[:-1]

    lows_rising = all(pullback_bars[i]["l"] >= pullback_bars[i - 1]["l"] for i in range(1, len(pullback_bars)))
    bodies_rising = all(pullback_bars[i]["c"] >= pullback_bars[i - 1]["c"] for i in range(1, len(pullback_bars)))
    ema_up = len(recent_ema) >= 2 and recent_ema[-1] > recent_ema[0]
    touched = any(b["l"] <= es[idx] * 1.003 for idx, b in zip(range(len(es) - 4, len(es)), recent_bars) if es[idx] is not None)
    reclaim = last["c"] > es[-1] and last["c"] > last["o"]
    above_vwap = bool(vw and p > vw)
    vol = bool(avgv and last["v"] > avgv * 1.15)

    if not ((lows_rising or bodies_rising) and ema_up and touched and reclaim and above_vwap):
        return False, {}

    score = 68 + (7 if lows_rising else 0) + (7 if bodies_rising else 0) + (8 if vol else 0)
    return True, score_pack(
        f"EMA9_{timeframe_label}_CURL_LONG",
        "🟢 LONG",
        f"{timeframe_label} curl back above 9 EMA ${safe_round(es[-1])}",
        f"Loss of recent pullback low ${safe_round(min(b['l'] for b in recent_bars))}",
        f"9 EMA: ${safe_round(es[-1])} | VWAP: ${safe_round(vw)}",
        "Expansion on reclaim ✅" if vol else "Needs more volume",
        f"{timeframe_label} pullback/curl with {len(pullback_bars)} structured bars into 9 EMA",
        score,
        "Actionable" if vol else "Watch",
    )


def ema_curl_short(candles_in, timeframe_label, p, vw):
    bars = regular_hours(candles_in)
    if len(bars) < 12:
        return False, {}
    closes = [c["c"] for c in bars]
    es = ema_series(closes, 9)
    if not es[-1]:
        return False, {}
    last = bars[-1]
    avgv = avg_volume(candles_in)
    recent_ema = [e for e in es[-4:] if e is not None]
    recent_bars = bars[-4:]
    bounce_bars = recent_bars[:-1]

    highs_falling = all(bounce_bars[i]["h"] <= bounce_bars[i - 1]["h"] for i in range(1, len(bounce_bars)))
    bodies_falling = all(bounce_bars[i]["c"] <= bounce_bars[i - 1]["c"] for i in range(1, len(bounce_bars)))
    ema_down = len(recent_ema) >= 2 and recent_ema[-1] < recent_ema[0]
    touched = any(b["h"] >= es[idx] * 0.997 for idx, b in zip(range(len(es) - 4, len(es)), recent_bars) if es[idx] is not None)
    reject = last["c"] < es[-1] and last["c"] < last["o"]
    below_vwap = bool(vw and p < vw)
    vol = bool(avgv and last["v"] > avgv * 1.15)

    if not ((highs_falling or bodies_falling) and ema_down and touched and reject and below_vwap):
        return False, {}

    score = 68 + (7 if highs_falling else 0) + (7 if bodies_falling else 0) + (8 if vol else 0)
    return True, score_pack(
        f"EMA9_{timeframe_label}_CURL_SHORT",
        "🔴 SHORT",
        f"{timeframe_label} reject back below 9 EMA ${safe_round(es[-1])}",
        f"Reclaim of recent bounce high ${safe_round(max(b['h'] for b in recent_bars))}",
        f"9 EMA: ${safe_round(es[-1])} | VWAP: ${safe_round(vw)}",
        "Expansion on reject ✅" if vol else "Needs more volume",
        f"{timeframe_label} bounce/reject with structured bars into 9 EMA",
        score,
        "Actionable" if vol else "Watch",
    )


def prior_vwap_earnings_pullback_long(c5, p, vw, prior_vw, earnings_flag):
    if not (earnings_flag and prior_vw and vw):
        return False, {}
    bars = regular_hours(c5)
    if len(bars) < 5:
        return False, {}
    last = bars[-1]
    avgv = avg_volume(c5)
    tagged = last["l"] <= prior_vw * 1.002
    held = last["c"] >= prior_vw and p > vw
    vol = bool(avgv and last["v"] > avgv * 1.1)
    if not (tagged and held):
        return False, {}
    score = 72 + (8 if vol else 0)
    return True, score_pack(
        "EARNINGS_PRIOR_VWAP_PULLBACK_LONG",
        "🟢 LONG",
        f"Hold off prior-day VWAP ${safe_round(prior_vw)}",
        f"Loss of prior-day VWAP ${safe_round(prior_vw)}",
        f"Prior-day VWAP: ${safe_round(prior_vw)} | Current VWAP: ${safe_round(vw)}",
        "Holding with volume ✅" if vol else "Needs more demand",
        "Earnings name pulling into prior-day value and holding",
        score,
        "Actionable" if vol else "Watch",
    )


# ── FORMAT ────────────────────────────────────────────────────────────────────
def fmt(ticker, data):
    score = data.get("score", 0)
    em = "🔥" if score >= 85 else "✅" if score >= 70 else "⚠️"
    return "\n".join(
        [
            f"{em} <b>{ticker} — {data.get('setup')}</b>  {data.get('dir')}",
            f"Confidence: <b>{score}/100</b>  |  {data.get('action')}",
            "━" * 30,
            f"📍 <b>Trigger:</b> {data.get('trigger')}",
            f"🛑 <b>Stop:</b> {data.get('inval')}",
            f"🔑 <b>Level:</b> {data.get('level')}",
            f"📊 <b>Volume:</b> {data.get('vol')}",
            f"📝 {data.get('notes', '')}",
            "━" * 30,
            f"⏰ {now_et().strftime('%I:%M %p ET')}",
            f"👉 {data.get('action')} — review before entry",
        ]
    )


# ── SCANNER ───────────────────────────────────────────────────────────────────
class Scanner:
    def __init__(self):
        self.wl = list(DEFAULT_WATCHLIST)
        self.pmh = {}
        self.pml = {}
        self.pr = {}
        self.pm_dt = None
        self.pr_dt = None
        self.last = defaultdict(lambda: None)
        self.earnings = set()
        self.obs = {}
        self.opt = {}
        self.opt_h = {}

    def is_market_hours(self):
        n = now_et()
        if n.weekday() >= 5:
            return False
        return n.replace(hour=9, minute=25, second=0, microsecond=0) <= n <= n.replace(hour=16, minute=5, second=0, microsecond=0)

    def refresh_reference_levels(self):
        today = now_et().date()
        if self.pm_dt != today:
            print("[SCAN] Refreshing premarket levels...")
            for t in self.wl:
                self.pmh[t] = premarket_extreme(t, "high")
                self.pml[t] = premarket_extreme(t, "low")
                time.sleep(API_PAUSE_SECONDS)
            self.pm_dt = today
        if self.pr_dt != today:
            print("[SCAN] Refreshing prior day levels...")
            for t in self.wl:
                self.pr[t] = prior_day(t)
                time.sleep(API_PAUSE_SECONDS)
            self.pr_dt = today

    def can_alert(self, ticker, setup_name):
        key = f"{ticker}:{setup_name}"
        last_sent = self.last[key]
        if last_sent is None:
            return True
        return (now_et() - last_sent).total_seconds() / 60 >= COOLDOWN

    def _bulk_add(self, tickers):
        added = []
        for t in tickers:
            symbol = t.upper().strip().replace(",", "")
            if symbol and symbol not in self.wl:
                self.wl.append(symbol)
                added.append(symbol)
        return added

    def scan(self, ticker):
        alerts = []
        try:
            c1 = candles(ticker, 1)
            c2 = candles(ticker, 2)
            c5 = candles(ticker, 5)
            if not c1 or not c2 or not c5:
                return alerts

            p = price(ticker)
            if not p:
                return alerts

            vw2 = vwap(c2)
            vw5 = vwap(c5)
            pmh = self.pmh.get(ticker)
            pml = self.pml.get(ticker)
            pd = self.pr.get(ticker, {})
            pdh = pd.get("h")
            pdl = pd.get("l")
            prior_vw = pd.get("vwap")
            is_earnings = ticker in self.earnings

            setups = [
                ("ORB_5M_LONG", lambda: orb_long(c5, c1, p, vw5, pmh)),
                ("ORB_5M_SHORT", lambda: orb_short(c5, c1, p, vw5, pml)),
                ("VWAP_RECLAIM_LONG", lambda: vwap_reclaim(c5, p, vw5)),
                ("VWAP_REJECT_SHORT", lambda: vwap_reject(c5, p, vw5)),
                ("EMA9_5M_PULLBACK_LONG", lambda: ema9_pb_long(c5, p, vw5)),
                ("EMA9_5M_PULLBACK_SHORT", lambda: ema9_pb_short(c5, p, vw5)),
                ("FLAG_BREAKOUT_LONG", lambda: flag_long(c5, p, vw5)),
                ("PDH_RETEST_LONG", lambda: pdh_retest(c5, p, vw5, pdh)),
                ("PDL_RETEST_SHORT", lambda: pdl_retest(c5, p, vw5, pdl)),
                ("EMA9_2M_CURL_LONG", lambda: ema_curl_long(c2, "2M", p, vw2)),
                ("EMA9_2M_CURL_SHORT", lambda: ema_curl_short(c2, "2M", p, vw2)),
                ("EMA9_5M_CURL_LONG", lambda: ema_curl_long(c5, "5M", p, vw5)),
                ("EMA9_5M_CURL_SHORT", lambda: ema_curl_short(c5, "5M", p, vw5)),
                ("EARNINGS_PRIOR_VWAP_PULLBACK_LONG", lambda: prior_vwap_earnings_pullback_long(c5, p, vw5, prior_vw, is_earnings)),
            ]

            for setup_name, fn in setups:
                try:
                    ok, data = fn()
                    if ok and data.get("score", 0) >= MIN_SCORE and self.can_alert(ticker, setup_name):
                        alerts.append((setup_name, data))
                except Exception as exc:
                    print(f"[SETUP ERR] {ticker} | {setup_name} | {exc}")

        except Exception as exc:
            print(f"[SCAN ERR] {ticker} | {exc}")
        return alerts

    def cmd(self, command: str):
        global MIN_SCORE
        pts = command.strip().split()
        c = pts[0].lower() if pts else ""

        if c == "/watch" and len(pts) >= 2:
            added = self._bulk_add(pts[1:])
            send_telegram(f"✅ Added: {', '.join(added) if added else 'nothing new'}\nWatching {len(self.wl)} stocks")

        elif c == "/remove" and len(pts) >= 2:
            removed = []
            for raw in pts[1:]:
                symbol = raw.upper().strip().replace(",", "")
                if symbol in self.wl:
                    self.wl = [x for x in self.wl if x != symbol]
                    removed.append(symbol)
            send_telegram(f"🗑️ Removed: {', '.join(removed) if removed else 'none'}")

        elif c == "/list":
            send_telegram(f"📋 Watching ({len(self.wl)}):\n{', '.join(self.wl)}")

        elif c == "/status":
            send_telegram(
                f"📊 <b>Scanner v3.1</b>\n"
                f"Stocks: {len(self.wl)} | Min score: {MIN_SCORE}/100 | Cooldown: {COOLDOWN}m\n"
                f"Earnings flags: {', '.join(sorted(self.earnings)) or 'none'}\n"
                f"Order blocks: {len(self.obs)}\n"
                f"New: 2M/5M EMA curl + earnings prior-VWAP pullback"
            )

        elif c == "/setups":
            send_telegram(
                "📊 <b>Active Setups</b>\n"
                "1. ORB 5M Long\n"
                "2. ORB 5M Short\n"
                "3. VWAP Reclaim Long\n"
                "4. VWAP Reject Short\n"
                "5. 9 EMA 5M Pullback Long\n"
                "6. 9 EMA 5M Pullback Short\n"
                "7. Flag Breakout Long\n"
                "8. PDH Retest Long\n"
                "9. PDL Retest Short\n"
                "10. 2M EMA Curl Long\n"
                "11. 2M EMA Curl Short\n"
                "12. 5M EMA Curl Long\n"
                "13. 5M EMA Curl Short\n"
                "14. Earnings Prior-VWAP Pullback Long"
            )

        elif c == "/threshold" and len(pts) == 2:
            try:
                MIN_SCORE = int(pts[1])
                send_telegram(f"⚙️ Min score updated: {MIN_SCORE}/100")
            except Exception:
                send_telegram("❌ Usage: /threshold 65")

        elif c == "/ob" and len(pts) == 4:
            try:
                t = pts[1].upper()
                self.obs[t] = (float(pts[2]), float(pts[3]))
                send_telegram(f"🧱 OB set {t}: ${pts[2]}–${pts[3]}")
            except Exception:
                send_telegram("❌ Usage: /ob TICKER LOW HIGH")

        elif c == "/earnings" and len(pts) >= 2:
            syms = [x.upper().strip().replace(",", "") for x in pts[1:]]
            for s in syms:
                if s:
                    self.earnings.add(s)
            send_telegram(f"📋 Earnings flagged: {', '.join(syms)}")

        elif c == "/unearnings" and len(pts) >= 2:
            syms = [x.upper().strip().replace(",", "") for x in pts[1:]]
            for s in syms:
                self.earnings.discard(s)
            send_telegram(f"🧹 Earnings removed: {', '.join(syms)}")

        elif c == "/auth" and len(pts) >= 2:
            full_url = " ".join(pts[1:])
            _complete_auth(full_url)

        elif c == "/reauth":
            _save({})
            send_telegram("🔄 Tokens cleared. Starting fresh auth...")
            threading.Thread(target=_login, daemon=True).start()

        else:
            send_telegram(
                "Commands:\n"
                "/watch NVDA AMD TSLA\n"
                "/remove NVDA TSLA\n"
                "/list\n"
                "/status\n"
                "/setups\n"
                "/threshold 65\n"
                "/ob TICKER LOW HIGH\n"
                "/earnings TICKER [MORE]\n"
                "/unearnings TICKER [MORE]\n"
                "/reauth"
            )

    def run(self):
        print("[SCANNER] v3.1 starting...")
        send_telegram(
            f"🤖 <b>Scanner v3.1 Online</b>\n"
            f"{'━' * 28}\n"
            f"Watching <b>{len(self.wl)} stocks</b> | Score ≥ {MIN_SCORE}/100\n"
            f"{'━' * 28}\n"
            "Core: ORB · VWAP reclaim/reject · 9 EMA pullback · Flag · PDH/PDL\n"
            "New: 2M/5M EMA curls · Earnings prior-VWAP pullback\n\n"
            "Commands: /status /setups /watch /remove /list /threshold /earnings /unearnings"
        )

        while True:
            if not self.is_market_hours():
                print("[SCAN] Outside market hours. Sleeping 5m.")
                time.sleep(300)
                continue

            self.refresh_reference_levels()

            for t in list(self.wl):
                try:
                    print(f"[SCAN] {t} ...")
                    for setup_name, data in self.scan(t):
                        send_telegram(fmt(t, data))
                        self.last[f"{t}:{setup_name}"] = now_et()
                        time.sleep(1)
                except Exception as exc:
                    print(f"[TICKER ERR] {t} | {exc}")
                time.sleep(API_PAUSE_SECONDS)

            print(f"[SCAN] Cycle complete. Sleep {SCAN_SLEEP_SECONDS}s.")
            time.sleep(SCAN_SLEEP_SECONDS)


def listen(scanner: Scanner):
    offset = None
    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"timeout": 30, "offset": offset},
                timeout=35,
            ).json()
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                text = update.get("message", {}).get("text", "")
                if text.startswith("/"):
                    print(f"[CMD] {text}")
                    scanner.cmd(text)
        except Exception as exc:
            print(f"[CMD ERR] {exc}")
            time.sleep(5)


if __name__ == "__main__":
    print(
        f"[MAIN] v3.1 | Schwab: {'OK' if SCHWAB_CLIENT_ID else 'MISSING'} | "
        f"Telegram: {'OK' if TELEGRAM_TOKEN else 'MISSING'}"
    )
    scanner = Scanner()
    threading.Thread(target=listen, args=(scanner,), daemon=True).start()
    scanner.run()
