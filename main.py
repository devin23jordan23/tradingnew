"""
momentum_scanner_v2.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Pure momentum & volume scanner — broad universe edition.

TIER 1  — Core names  (2-min candles, full precision, every 60s loop)
TIER 2  — Extended names (5-min candles, volume+price alerts, every 60s loop)
/watch  — Manual additions treated as Tier 1 immediately

THREE INDEPENDENT TRIGGERS (any one fires an alert):
  T1 — Price Velocity   : moved X% of 21d ATR in rolling N-bar window
  T2 — Volume Surge     : bar volume spikes vs session avg AND historical avg
  T3 — Confirmed Move   : BOTH price velocity AND volume surge together

Volume compared against THREE baselines simultaneously:
  • Historical avg bar volume (21-day)
  • Stock's own session average so far today
  • Raw dollar volume per bar (absolute size filter)

Infrastructure mirrors v3.16 exactly.
"""

import os, time, json, math, base64, threading, requests
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode
from zoneinfo import ZoneInfo

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

ET = ZoneInfo("America/New_York")

CLIENT_ID     = os.environ["SCHWAB_CLIENT_ID"]
CLIENT_SECRET = os.environ["SCHWAB_CLIENT_SECRET"]
REDIRECT      = "https://127.0.0.1"
BASE          = "https://api.schwabapi.com/marketdata/v1"
AUTH_URL      = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL     = "https://api.schwabapi.com/v1/oauth/token"
TOKEN_FILE    = "tokens_momentum_v2.json"

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DISCORD_URL      = os.environ.get("DISCORD_WEBHOOK_URL", "")

SCAN_INTERVAL    = 60       # seconds between full scans
DEDUP_TTL_T1     = 600      # 10 min dedup — core names (2-min bars, moves fast)
DEDUP_TTL_T2     = 1800     # 30 min dedup — extended names (don't re-spam same ticker)
PRICE_GATE_PCT   = 0.005    # 0.5% price move required to re-alert same level
REFIRE_MINUTES   = 30       # confirmed move refire window

# Dollar volume minimums per tier (filters thin tape)
MIN_DOLLAR_VOL_T1 = 10_000_000   # $10M per 2-min bar  — core names
MIN_DOLLAR_VOL_T2 = 50_000_000   # $50M per 5-min bar  — extended must show serious size

# ATR velocity thresholds — Tier 2 must move TWICE as hard to alert
VELOCITY_PCT_T1  = 0.25   # 25% of ATR in 3 bars — core
VELOCITY_PCT_T2  = 0.50   # 50% of ATR in 3 bars — extended only on real momentum

# Volume surge multipliers — completely separate per tier
VOL_HIST_MULT_T1    = 2.0    # core: 2x historical avg fires
VOL_HIST_MULT_T2    = 4.0    # extended: 4x historical — needs real institutional energy
VOL_SESSION_MULT_T1 = 1.8    # core: 1.8x today's session avg fires
VOL_SESSION_MULT_T2 = 3.0    # extended: 3x today's session avg — eliminates routine bumps

# ─────────────────────────────────────────────────────────────
# TIER 1 — CORE WATCHLIST  (2-min precision, always scanned)
# ─────────────────────────────────────────────────────────────

CORE_WATCHLIST = [
    # Mega-cap tech & semis
    "NVDA", "AMD", "MSFT", "AAPL", "META", "GOOGL", "AMZN", "TSLA",
    "AVGO", "QCOM", "MU", "INTC", "ARM", "MRVL", "AAOI",
    # High-beta momentum names
    "PLTR", "DELL", "RKLB",
    # New additions
    "CRWV", "NBIS", "CSCO", "SNDK", "TSM", "NFLX", "ASTS",
    # Index ETFs
    "SPY", "QQQ",
]

# ─────────────────────────────────────────────────────────────
# TIER 2 — EXTENDED UNIVERSE  (5-min, volume+price alerts only)
# ─────────────────────────────────────────────────────────────

EXTENDED_WATCHLIST = [
    # Financials / fintech
    "HOOD", "COIN", "SOFI", "GS", "JPM", "BAC", "MS", "C", "V", "MA",
    # EV / clean energy
    "RIVN", "LCID", "NIO", "ENPH", "SEDG", "PLUG", "FCEL",
    # Crypto-adjacent
    "MARA", "RIOT", "CLSK",
    # Healthcare / biotech
    "LLY", "BMY", "PFE", "MRNA", "ABBV", "JNJ",
    # Consumer / retail
    "LULU", "SHOP", "AMZN",   # AMZN in both tiers for extended coverage
    # Enterprise / cloud
    "CRM", "TEAM", "NOW", "SNOW", "PANW",
    # China ADRs
    "BIDU", "FUTU", "BABA", "JD",
    # Airlines / industrials
    "DAL", "UAL", "AAL", "BA", "GE",
    # Autos / legacy
    "F", "GM",
    # Other liquid movers
    "IBM", "ORCL", "NFLX", "DIS", "UBER", "LYFT",
    "ASTR", "ACHR", "JOBY",
]

# Remove any extended names already in core (no double scanning)
EXTENDED_WATCHLIST = [t for t in EXTENDED_WATCHLIST if t not in CORE_WATCHLIST]

# ─────────────────────────────────────────────────────────────
# AUTH  (identical to v3.16)
# ─────────────────────────────────────────────────────────────

_pending_auth = False

def _b64():
    return base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

def _save_tokens(t):
    t["saved_at"] = time.time()
    with open(TOKEN_FILE, "w") as f:
        json.dump(t, f)

def _load_tokens():
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def _expired(t):
    age = time.time() - t.get("saved_at", 0)
    return age > t.get("expires_in", 1800) - 120

def _refresh_tokens(t):
    try:
        r = requests.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {_b64()}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"]},
            timeout=15,
        )
        r.raise_for_status()
        new = r.json()
        _save_tokens(new)
        print("[AUTH] Tokens refreshed.")
        return new
    except Exception as e:
        print(f"[AUTH] Refresh failed: {e}")
        return t

def _login():
    global _pending_auth
    params = {"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT}
    url = f"{AUTH_URL}?{urlencode(params)}"
    send_telegram(
        f"🔐 <b>Schwab Authorization Required</b>\n"
        f"<a href='{url}'>Click to authorize</a>\n\n"
        f"After login, send the full redirect URL:\n/auth &lt;url&gt;"
    )
    _pending_auth = True
    for _ in range(150):
        time.sleep(2)
        if not _pending_auth:
            return _load_tokens()
    send_telegram("⏰ Auth timed out. Retry with /reauth")
    return None

def _complete_auth(full_redirect_url):
    global _pending_auth
    try:
        parsed = urlparse(full_redirect_url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if not code:
            send_telegram("❌ Could not find auth code in that URL.")
            return False
        r = requests.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {_b64()}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "code": code,
                  "redirect_uri": REDIRECT},
            timeout=15,
        )
        r.raise_for_status()
        _save_tokens(r.json())
        _pending_auth = False
        send_telegram("✅ <b>Schwab connected successfully!</b>")
        return True
    except Exception as e:
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
            r = requests.get(f"{BASE}{ep}", headers=_hdr(),
                             params=params or {}, timeout=10)
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

# ─────────────────────────────────────────────────────────────
# MARKET DATA
# ─────────────────────────────────────────────────────────────

def candles(ticker, minutes=2):
    data = _get(f"/pricehistory?symbol={ticker}", {
        "periodType":      "day",
        "period":          1,
        "frequencyType":   "minute",
        "frequency":       minutes,
        "needExtendedHoursData": "true",
    })
    out = []
    for c in data.get("candles", []):
        out.append({
            "o": c["open"],  "h": c["high"],
            "l": c["low"],   "c": c["close"],
            "v": c["volume"],
            "ts": datetime.fromtimestamp(c["datetime"] / 1000, tz=ET),
        })
    return out

def rh_bars(cs):
    """Regular-hours bars only: 9:30–16:00."""
    return [
        b for b in cs
        if (b["ts"].hour * 60 + b["ts"].minute) >= 570   # 9:30
        and (b["ts"].hour * 60 + b["ts"].minute) < 960   # 16:00
    ]

def closed_only(cs, bar_minutes):
    """Drop the still-forming bar."""
    now = datetime.now(tz=ET)
    return [b for b in cs
            if (now - b["ts"]).total_seconds() >= bar_minutes * 60]

def daily_atr21(ticker):
    """21-day ATR — same formula as TOS. Cached per day."""
    data = _get(f"/pricehistory?symbol={ticker}", {
        "periodType":   "month", "period": 2,
        "frequencyType":"daily", "frequency": 1,
        "needExtendedHoursData": "false",
    })
    bars = data.get("candles", [])[-22:]
    if len(bars) < 2:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return round(sum(trs[-21:]) / len(trs[-21:]), 4) if trs else None

def daily_avg_bar_vol(ticker, bar_minutes=5):
    """
    Historical average volume per bar over past 5 sessions.
    Used as baseline: is today unusual vs history?
    """
    data = _get(f"/pricehistory?symbol={ticker}", {
        "periodType":   "day", "period": 5,
        "frequencyType":"minute", "frequency": bar_minutes,
        "needExtendedHoursData": "false",
    })
    bars = data.get("candles", [])
    if not bars:
        return None
    total_vol  = sum(b["volume"] for b in bars)
    return total_vol / len(bars) if bars else None

def ema_series(values, period):
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    e = [sum(values[:period]) / period]
    for v in values[period:]:
        e.append(v * k + e[-1] * (1 - k))
    return e

# ─────────────────────────────────────────────────────────────
# ALERTS
# ─────────────────────────────────────────────────────────────

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                  "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as e:
        print(f"[TG ERR] {e}")

def send_discord(msg):
    if not DISCORD_URL:
        return
    try:
        clean = msg.replace("<b>","**").replace("</b>","**")
        clean = clean.replace("<i>","*").replace("</i>","*")
        for tag in ["<a href='","'>","</a>"]:
            clean = clean.replace(tag, "")
        requests.post(DISCORD_URL, json={"content": clean}, timeout=8)
    except Exception as e:
        print(f"[DISCORD ERR] {e}")

def send_alert(msg):
    send_telegram(msg)
    send_discord(msg)

# ─────────────────────────────────────────────────────────────
# DEDUPLICATION
# ─────────────────────────────────────────────────────────────

class Dedup:
    def __init__(self):
        self._store = {}  # key -> (timestamp, price)

    def should_fire(self, key, price, ttl=DEDUP_TTL_T1):
        now = time.time()
        if key in self._store:
            last_ts, last_px = self._store[key]
            if now - last_ts < ttl:
                if last_px and abs(price - last_px) / last_px < PRICE_GATE_PCT:
                    return False
        self._store[key] = (now, price)
        return True

    def clear(self):
        self._store.clear()

# ─────────────────────────────────────────────────────────────
# VOLUME ANALYSIS  — three baselines simultaneously
# ─────────────────────────────────────────────────────────────

def analyze_volume(cur_bar, rh_session_bars, hist_avg_bar_vol, price):
    """
    Returns dict with all three volume readings.

    vs_hist     : current bar vol vs 21d historical avg bar vol
    vs_session  : current bar vol vs today's own session avg (catches second surges)
    dollar_vol  : raw dollar volume this bar (price × volume)
    """
    cur_vol    = cur_bar["v"]
    dollar_vol = price * cur_vol

    # Session avg: use all closed rh bars except the current one
    prior_session = rh_session_bars[:-1]
    if prior_session:
        session_avg = sum(b["v"] for b in prior_session) / len(prior_session)
        vs_session  = cur_vol / session_avg if session_avg > 0 else None
    else:
        vs_session = None

    vs_hist = cur_vol / hist_avg_bar_vol if hist_avg_bar_vol and hist_avg_bar_vol > 0 else None

    return {
        "vs_hist":    round(vs_hist,    2) if vs_hist    else None,
        "vs_session": round(vs_session, 2) if vs_session else None,
        "dollar_vol": dollar_vol,
        "cur_vol":    cur_vol,
    }

def volume_is_surging(vol_data, tier=1):
    """
    Tier 1 (core): either hist OR session surge triggers — lower bar, faster alerts.
    Tier 2 (extended): BOTH hist AND session must surge, at higher multipliers.
    Dollar volume minimum also much higher for Tier 2.
    """
    min_dollar = MIN_DOLLAR_VOL_T1 if tier == 1 else MIN_DOLLAR_VOL_T2
    if vol_data["dollar_vol"] < min_dollar:
        return False

    if tier == 1:
        hist_surge    = vol_data["vs_hist"]    and vol_data["vs_hist"]    >= VOL_HIST_MULT_T1
        session_surge = vol_data["vs_session"] and vol_data["vs_session"] >= VOL_SESSION_MULT_T1
        return hist_surge or session_surge   # either triggers on core
    else:
        # Tier 2: BOTH baselines must confirm at elevated thresholds
        hist_surge    = vol_data["vs_hist"]    and vol_data["vs_hist"]    >= VOL_HIST_MULT_T2
        session_surge = vol_data["vs_session"] and vol_data["vs_session"] >= VOL_SESSION_MULT_T2
        return hist_surge and session_surge  # both required on extended

# ─────────────────────────────────────────────────────────────
# PRICE VELOCITY
# ─────────────────────────────────────────────────────────────

def check_price_velocity(closed_bars, atr, window=3, threshold_pct=0.25):
    """
    Multi-window rolling check: catches moves at different speeds.
    Checks 2-bar (fastest), 3-bar (standard), 5-bar (sustained) simultaneously.
    Fires on whichever window triggers first.
    Returns (fired, move_dollars, move_pct_atr, direction, window_used)
    """
    windows_to_check = [w for w in [2, 3, 5] if len(closed_bars) >= w]
    for w in windows_to_check:
        window_bars = closed_bars[-w:]
        high = max(b["h"] for b in window_bars)
        low  = min(b["l"] for b in window_bars)
        move = high - low
        # 5-bar window uses 1.5x threshold to avoid wide-ranging day false fires
        scale = 1.5 if w == 5 else 1.0
        if move >= atr * threshold_pct * scale:
            direction = "🟢 LONG" if window_bars[-1]["c"] > window_bars[0]["o"] else "🔴 SHORT"
            pct_atr   = round(move / atr * 100, 1)
            return True, round(move, 2), pct_atr, direction, w
    return False, 0, 0, None, 0

def check_range_expansion(closed_bars, atr):
    """
    Flat-to-vertical: consolidation followed by sharp expansion.
    Quiet gate widened to 35% ATR so high-ATR mega-caps (META, AMZN) are caught.
    META $8 quiet range on $29.50 ATR = 27% — now passes the 35% gate.
    Returns (fired, quiet_range, accel_move, vol_mult, direction)
    """
    if len(closed_bars) < 13:
        return False, 0, 0, 0, None
    quiet_bars = closed_bars[-13:-3]
    accel_bars = closed_bars[-3:]
    quiet_high  = max(b["h"] for b in quiet_bars)
    quiet_low   = min(b["l"] for b in quiet_bars)
    quiet_range = quiet_high - quiet_low
    if quiet_range >= atr * 0.35:
        return False, 0, 0, 0, None
    accel_high = max(b["h"] for b in accel_bars)
    accel_low  = min(b["l"] for b in accel_bars)
    accel_move = accel_high - accel_low
    if accel_move < atr * 0.25:
        return False, 0, 0, 0, None
    avg_quiet_vol = sum(b["v"] for b in quiet_bars) / len(quiet_bars)
    avg_accel_vol = sum(b["v"] for b in accel_bars) / len(accel_bars)
    if avg_quiet_vol == 0 or avg_accel_vol < avg_quiet_vol * 2.0:
        return False, 0, 0, 0, None
    vol_mult  = round(avg_accel_vol / avg_quiet_vol, 1)
    direction = "🟢 LONG" if accel_bars[-1]["c"] > quiet_high else "🔴 SHORT"
    return True, round(quiet_range, 2), round(accel_move, 2), vol_mult, direction

# ─────────────────────────────────────────────────────────────
# DETECTOR 4 — AFTERNOON HOD / ATH BREAKOUT
# ─────────────────────────────────────────────────────────────

def get_52w_high(ticker):
    """Fetch 52-week high from daily candles."""
    try:
        data = _get(f"/pricehistory?symbol={ticker}", {
            "periodType":    "year", "period": 1,
            "frequencyType": "daily", "frequency": 1,
            "needExtendedHoursData": "false",
        })
        bars = data.get("candles", [])
        if not bars:
            return None
        return max(b["high"] for b in bars)
    except Exception:
        return None

def check_hod_breakout(closed_rh_bars, cur_price, atr, w52_high=None, tier=1):
    """
    Returns (fired, hod_price, vol_expanding, pct_above_hod, velocity_pct_atr) or (False,...)

    FIRES when ANY of these are true after 12 PM:
      A) ATH territory (within 2% of 52w high) + new HOD close — no volume req
      B) New HOD + velocity (Tier 1: 25%+ ATR | Tier 2: 50%+ ATR) — price is the signal
      C) New HOD + volume expanding 20%+ above recent 10-bar avg — classic breakout

    Tier 2 extended names require Tier 1 energy: velocity threshold doubles,
    vol expansion still applies the same way (it's already a relative check).
    ATH territory always fires regardless of tier — price discovery at ATH is never noise.
    
    price structure at all-time highs IS the signal regardless of vol.
    """
    now = datetime.now(tz=ET)
    if now.hour < 12:
        return False, 0, False, 0, 0

    if len(closed_rh_bars) < 12:
        return False, 0, False, 0, 0

    prior_bars  = closed_rh_bars[:-1]
    session_hod = max(b["h"] for b in prior_bars)

    # Must close above prior HOD
    if cur_price <= session_hod:
        return False, 0, False, 0, 0

    pct_above = round((cur_price - session_hod) / session_hod * 100, 3)

    # Must clear HOD by at least 0.1% — filters flat tape noise
    if pct_above < 0.10:
        return False, 0, False, 0, 0

    # Volume expansion check
    lookback      = prior_bars[-10:]
    avg_recent_vol = sum(b["v"] for b in lookback) / len(lookback) if lookback else 0
    cur_vol        = closed_rh_bars[-1]["v"]
    vol_expanding  = avg_recent_vol > 0 and cur_vol >= avg_recent_vol * 1.2

    # Velocity check — Tier 2 must clear the higher bar (same as scan_ticker vel_thresh)
    vel_threshold = VELOCITY_PCT_T1 * 100 if tier == 1 else VELOCITY_PCT_T2 * 100  # convert to pct
    window_bars   = closed_rh_bars[-3:]
    window_move   = max(b["h"] for b in window_bars) - min(b["l"] for b in window_bars)
    velocity_pct  = round(window_move / atr * 100, 1) if atr else 0
    vel_confirmed = velocity_pct >= vel_threshold

    # ATH territory check
    is_ath_zone = w52_high and cur_price >= w52_high * 0.98

    # Gate: needs at least ONE of (ATH zone, velocity, vol expanding) to fire
    if not any([is_ath_zone, vel_confirmed, vol_expanding]):
        return False, 0, False, 0, 0

    return True, round(session_hod, 2), vol_expanding, pct_above, velocity_pct

# ─────────────────────────────────────────────────────────────
# ALERT BUILDERS
# ─────────────────────────────────────────────────────────────

def fmt_dollar(n):
    if n >= 1_000_000_000:
        return f"${n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    return f"${n:,.0f}"

def build_alert(label, emoji, ticker, tier, price, move_dollars, pct_atr,
                direction, vol_data, atr, now):
    tier_str = "Core" if tier == 1 else "Extended"
    bar_str  = "2-min" if tier == 1 else "5-min"

    vs_hist_str    = f"{vol_data['vs_hist']:.1f}x hist avg"    if vol_data["vs_hist"]    else "hist N/A"
    vs_session_str = f"{vol_data['vs_session']:.1f}x today avg" if vol_data["vs_session"] else "session N/A"
    dv_str         = fmt_dollar(vol_data["dollar_vol"])

    price_chg_str = f"${move_dollars:+.2f}" if move_dollars else "—"
    atr_str       = f"{pct_atr}% of ATR" if pct_atr else "—"

    return (
        f"{emoji} <b>{ticker} — {label}</b>  {direction}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Price: ${price:.2f}  |  Move: {price_chg_str}  ({atr_str})\n"
        f"📊 Vol vs hist: {vs_hist_str}\n"
        f"📈 Vol vs today: {vs_session_str}\n"
        f"💰 Dollar vol this bar: {dv_str}\n"
        f"📐 21d ATR: ${atr:.2f}  |  {tier_str} ({bar_str} bars)\n"
        f"⏰ {now.strftime('%I:%M %p ET')}"
    )

# ─────────────────────────────────────────────────────────────
# CORE SCANNER CLASS
# ─────────────────────────────────────────────────────────────

class MomentumScanner:

    def __init__(self):
        self.core_list     = list(CORE_WATCHLIST)
        self.extended_list = list(EXTENDED_WATCHLIST)
        self.manual_watch  = []   # /watch additions → treated as Tier 1

        self.dedup         = Dedup()
        self.datr          = {}   # ticker -> 21d ATR
        self.hist_vol      = {}   # ticker -> historical avg bar vol
        self.w52_high      = {}   # ticker -> 52-week high (cached daily)
        self._cache_date   = None
        self.refire_ts     = {}   # key -> last fire timestamp

    # ── helpers ───────────────────────────────────────────────

    def _all_tier1(self):
        return list(dict.fromkeys(self.core_list + self.manual_watch))

    def _all_tier2(self):
        t1 = set(self._all_tier1())
        return [t for t in self.extended_list if t not in t1]

    def _refire_ok(self, key, minutes=REFIRE_MINUTES):
        now = time.time()
        if now - self.refire_ts.get(key, 0) < minutes * 60:
            return False
        self.refire_ts[key] = now
        return True

    # ── daily reset ───────────────────────────────────────────

    def daily_reset(self):
        today = datetime.now(tz=ET).date()
        if self._cache_date == today:
            return
        print(f"[RESET] Loading ATR + hist vol for all tickers...")

        all_tickers = list(dict.fromkeys(
            self._all_tier1() + self._all_tier2()
        ))

        for ticker in all_tickers:
            atr = daily_atr21(ticker)
            if atr:
                self.datr[ticker] = atr

            # Historical vol: use matching bar size per tier
            bar_min = 2 if ticker in self._all_tier1() else 5
            hv = daily_avg_bar_vol(ticker, bar_min)
            if hv:
                self.hist_vol[ticker] = hv

            # 52-week high for ATH detection
            w52 = get_52w_high(ticker)
            if w52:
                self.w52_high[ticker] = w52

            time.sleep(0.3)

        self.dedup.clear()
        self.refire_ts.clear()
        self._cache_date = today

        now = datetime.now(tz=ET)
        send_alert(
            f"🔄 <b>Momentum Scanner v2 — Daily Reset</b>\n"
            f"Tier 1 (2-min): {len(self._all_tier1())} names\n"
            f"Tier 2 (5-min): {len(self._all_tier2())} names\n"
            f"ATRs loaded: {len(self.datr)} | {now.strftime('%b %d, %Y')}"
        )

    # ── single ticker scan ────────────────────────────────────

    def scan_ticker(self, ticker, tier):
        atr      = self.datr.get(ticker)
        hist_vol = self.hist_vol.get(ticker)
        if not atr:
            return

        # Suppress all alerts during MOC window — closing flow makes everything look like a surge
        if self.is_moc_window():
            return

        bar_min    = 2 if tier == 1 else 5
        vel_thresh = VELOCITY_PCT_T1 if tier == 1 else VELOCITY_PCT_T2
        dedup_ttl  = DEDUP_TTL_T1   if tier == 1 else DEDUP_TTL_T2

        try:
            cs     = candles(ticker, bar_min)
            rh     = rh_bars(cs)
            closed = closed_only(rh, bar_min)

            if len(closed) < 4:
                return

            cur_bar   = closed[-1]
            cur_price = cur_bar["c"]

            # ── Volume analysis ───────────────────────────────
            vol_data = analyze_volume(cur_bar, closed, hist_vol, cur_price)
            vol_surge = volume_is_surging(vol_data, tier)

            # ── Price velocity (multi-window) ─────────────────
            vel_fired, move_dollars, pct_atr, direction, vel_window = check_price_velocity(
                closed, atr, window=3, threshold_pct=vel_thresh
            )

            # ── Range expansion (flat-to-vertical) ───────────
            rng_fired, quiet_range, accel_move, vol_mult, rng_direction = check_range_expansion(
                closed, atr
            )
            if rng_fired and not vel_fired:
                # Range expansion fires independently — not double-counted with velocity
                key = f"{ticker}:RANGE_EXP"
                if self.dedup.should_fire(key, cur_price, ttl=dedup_ttl):
                    now_re = datetime.now(tz=ET)
                    dv  = fmt_dollar(vol_data["dollar_vol"])
                    vss = f"{vol_data['vs_session']:.1f}x" if vol_data["vs_session"] else "N/A"
                    pct_atr_re = round(accel_move / atr * 100, 1)
                    msg = (
                        f"⚡ <b>{ticker} — FLAT-TO-VERTICAL</b>  {rng_direction}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📍 Was ranging ${quiet_range:.2f} for 10+ bars → exploded ${accel_move:.2f} ({pct_atr_re}% ATR)\n"
                        f"📊 Vol: {vol_mult}x vs quiet period | Dollar vol: {dv}\n"
                        f"📈 Vol vs session: {vss} | Price: ${cur_price:.2f}\n"
                        f"📐 21d ATR: ${atr:.2f}  |  {'Core' if tier==1 else 'Extended'}\n"
                        f"⏰ {now_re.strftime('%I:%M %p ET')}"
                    )
                    send_alert(msg)
                    print(f"[RANGE_EXP] {ticker} T{tier} — quiet ${quiet_range:.2f} → accel ${accel_move:.2f}, vol {vol_mult}x")

            now = datetime.now(tz=ET)

            # ── TRIGGER 4: Afternoon HOD / ATH Breakout ───────
            w52 = self.w52_high.get(ticker)
            hod_fired, session_hod, vol_expanding, pct_above, velocity_pct = check_hod_breakout(
                closed, cur_price, atr, w52_high=w52, tier=tier
            )
            if hod_fired:
                is_ath = w52 and cur_price >= w52 * 0.98

                label  = "ATH BREAKOUT 🚨" if is_ath else "NEW HIGH OF DAY"
                emoji  = "🚀🚀" if is_ath else "🚀"

                # Build confirmation tags — show what triggered it
                confirms = []
                if is_ath:
                    confirms.append("ATH territory")
                if velocity_pct >= 25:
                    confirms.append(f"velocity {velocity_pct}% ATR")
                if vol_expanding:
                    confirms.append("vol expanding")
                confirm_str = " | ".join(confirms) if confirms else "HOD break"

                open_price = closed[0]["o"] if closed else cur_price
                day_move   = round(cur_price - open_price, 2)
                day_pct    = round(day_move / open_price * 100, 2) if open_price else 0

                dv  = fmt_dollar(vol_data["dollar_vol"])
                vss = f"{vol_data['vs_session']:.1f}x today avg" if vol_data["vs_session"] else "N/A"

                ath_line = f"🏔 52w High: ${w52:.2f} | Price: {pct_above:.2f}% into ATH zone\n" if is_ath else ""

                key = f"{ticker}:HOD:{round(cur_price, 1)}"
                if self.dedup.should_fire(key, cur_price, ttl=dedup_ttl):
                    msg = (
                        f"{emoji} <b>{ticker} — {label}</b>  🟢 LONG\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📍 Prior HOD: ${session_hod:.2f} → Now: ${cur_price:.2f} (+{pct_above:.2f}%)\n"
                        f"📈 Day move: ${day_move:+.2f} ({day_pct:+.2f}%)\n"
                        f"{ath_line}"
                        f"✅ Triggered by: {confirm_str}\n"
                        f"💰 Dollar vol this bar: {dv} | {vss}\n"
                        f"📐 21d ATR: ${atr:.2f}  |  {'Core' if tier==1 else 'Extended'}\n"
                        f"⏰ {now.strftime('%I:%M %p ET')}"
                    )
                    send_alert(msg)
                    print(f"[HOD{'*ATH' if is_ath else ''}] {ticker} — broke ${session_hod:.2f} → ${cur_price:.2f} | {confirm_str}")

            # ── TRIGGER 3: Confirmed Move (both) ──────────────
            if vel_fired and vol_surge:
                key = f"{ticker}:CONFIRMED"
                if self.dedup.should_fire(key, cur_price, ttl=dedup_ttl) and self._refire_ok(key):
                    msg = build_alert(
                        "CONFIRMED MOVE", "🔥🔥",
                        ticker, tier, cur_price, move_dollars, pct_atr,
                        direction, vol_data, atr, now
                    )
                    send_alert(msg)
                    print(f"[CONFIRMED] {ticker} T{tier} — ${move_dollars} move + vol surge")
                return   # don't double-alert with T1/T2 below

            # ── TRIGGER 1: Price Velocity only ───────────────
            if vel_fired:
                key = f"{ticker}:VELOCITY"
                if self.dedup.should_fire(key, cur_price, ttl=dedup_ttl):
                    bar_label = f"{vel_window * (2 if tier == 1 else 5)}-min"
                    msg = build_alert(
                        f"PRICE VELOCITY ({bar_label})", "📈",
                        ticker, tier, cur_price, move_dollars, pct_atr,
                        direction, vol_data, atr, now
                    )
                    send_alert(msg)
                    print(f"[VELOCITY] {ticker} T{tier} — ${move_dollars} in {vel_window} bars ({bar_label}), {pct_atr}% ATR")

            # ── TRIGGER 2: Volume Surge only ──────────────────
            elif vol_surge:
                key = f"{ticker}:VOLUME"
                if self.dedup.should_fire(key, cur_price, ttl=dedup_ttl):
                    # Compute price change from session open for context
                    open_price = closed[0]["o"] if closed else cur_price
                    open_move  = round(cur_price - open_price, 2)
                    open_pct   = round(open_move / open_price * 100, 2) if open_price else 0
                    direction  = "🟢" if open_move >= 0 else "🔴"

                    dv  = fmt_dollar(vol_data["dollar_vol"])
                    vsh = f"{vol_data['vs_hist']:.1f}x"    if vol_data["vs_hist"]    else "N/A"
                    vss = f"{vol_data['vs_session']:.1f}x" if vol_data["vs_session"] else "N/A"

                    msg = (
                        f"📊 <b>{ticker} — VOLUME SURGE</b>  {direction}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"💵 Price: ${cur_price:.2f}  |  Day move: ${open_move:+.2f} ({open_pct:+.2f}%)\n"
                        f"📊 Vol vs hist: {vsh} | Vol vs today: {vss}\n"
                        f"💰 Dollar vol this bar: {dv}\n"
                        f"📐 21d ATR: ${atr:.2f}  |  {'Core' if tier==1 else 'Extended'}\n"
                        f"⏰ {now.strftime('%I:%M %p ET')}"
                    )
                    send_alert(msg)
                    print(f"[VOLUME] {ticker} T{tier} — hist {vsh}, session {vss}, dv {dv}")

        except Exception as e:
            print(f"[ERR] {ticker}: {e}")

    # ── main scan loop ────────────────────────────────────────

    def is_market_hours(self):
        now = datetime.now(tz=ET)
        if now.weekday() >= 5:
            return False
        t = now.hour * 60 + now.minute
        return 9 * 60 + 25 <= t < 16 * 60

    def is_moc_window(self):
        """Suppress alerts 3:30–4:00 PM — MOC order flow makes every stock look like it's surging."""
        now = datetime.now(tz=ET)
        t = now.hour * 60 + now.minute
        return t >= 15 * 60 + 30   # 3:30 PM ET onward

    def run(self):
        print("[MOMv2] Scanner starting...")
        while True:
            now = datetime.now(tz=ET)

            if not self.is_market_hours():
                print(f"[WAIT] Market closed — {now.strftime('%H:%M ET')}")
                time.sleep(30)
                continue

            if now.hour == 9 and now.minute == 25:
                self.daily_reset()

            if not self.datr:
                self.daily_reset()

            t1 = self._all_tier1()
            t2 = self._all_tier2()
            print(f"[SCAN] {now.strftime('%H:%M:%S')} — T1:{len(t1)} T2:{len(t2)}")

            # Tier 1 — 2-min bars, highest priority
            for ticker in t1:
                self.scan_ticker(ticker, tier=1)
                time.sleep(0.4)

            # Tier 2 — 5-min bars
            for ticker in t2:
                self.scan_ticker(ticker, tier=2)
                time.sleep(0.4)

            time.sleep(SCAN_INTERVAL)

# ─────────────────────────────────────────────────────────────
# TELEGRAM COMMAND LISTENER
# ─────────────────────────────────────────────────────────────

_last_update_id = 0

def poll_telegram(scanner):
    global _last_update_id
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": _last_update_id + 1, "timeout": 20},
                timeout=25,
            )
            for u in r.json().get("result", []):
                _last_update_id = u["update_id"]
                msg  = u.get("message", {})
                text = msg.get("text", "").strip()
                chat = str(msg.get("chat", {}).get("id", ""))

                if chat != TELEGRAM_CHAT_ID:
                    continue

                if text.startswith("/watch "):
                    sym = text.split(None, 1)[1].upper().strip()
                    if sym not in scanner.manual_watch:
                        scanner.manual_watch.append(sym)
                        # Immediately fetch ATR + hist vol + 52w high for new name
                        atr = daily_atr21(sym)
                        hv  = daily_avg_bar_vol(sym, 2)
                        w52 = get_52w_high(sym)
                        if atr: scanner.datr[sym]     = atr
                        if hv:  scanner.hist_vol[sym] = hv
                        if w52: scanner.w52_high[sym] = w52
                        send_telegram(
                            f"✅ <b>{sym}</b> added to Tier 1 watch\n"
                            f"ATR: ${atr:.2f}" if atr else f"✅ <b>{sym}</b> added (ATR fetch failed)"
                        )
                    else:
                        send_telegram(f"ℹ️ {sym} already being watched")

                elif text.startswith("/remove "):
                    sym = text.split(None, 1)[1].upper().strip()
                    if sym in scanner.manual_watch:
                        scanner.manual_watch.remove(sym)
                        send_telegram(f"🗑 <b>{sym}</b> removed from manual watch")
                    else:
                        send_telegram(f"ℹ️ {sym} is not in manual watch list")

                elif text == "/list":
                    t1 = scanner._all_tier1()
                    t2 = scanner._all_tier2()
                    send_telegram(
                        f"📋 <b>Scanner Universe</b>\n\n"
                        f"<b>Tier 1 (2-min, full scan):</b>\n{', '.join(sorted(t1))}\n\n"
                        f"<b>Tier 2 (5-min, vol+price):</b>\n{', '.join(sorted(t2))}"
                    )

                elif text == "/status":
                    t1 = scanner._all_tier1()
                    t2 = scanner._all_tier2()
                    now = datetime.now(tz=ET)
                    send_telegram(
                        f"📊 <b>Momentum Scanner v2 Status</b>\n"
                        f"Time: {now.strftime('%H:%M ET')}\n"
                        f"Tier 1: {len(t1)} names | Tier 2: {len(t2)} names\n"
                        f"ATRs cached: {len(scanner.datr)}\n"
                        f"Manual watches: {', '.join(scanner.manual_watch) or 'none'}"
                    )

                elif text.startswith("/auth "):
                    _complete_auth(text.split(None, 1)[1])

                elif text == "/reauth":
                    global _pending_auth
                    _pending_auth = False
                    _login()

                elif text == "/help":
                    send_telegram(
                        "📖 <b>Commands</b>\n"
                        "/watch TICKER — add to Tier 1 scan immediately\n"
                        "/remove TICKER — remove from manual watch\n"
                        "/list — show full scanner universe\n"
                        "/status — scanner health + counts\n"
                        "/auth URL — complete Schwab OAuth\n"
                        "/reauth — restart auth flow"
                    )

        except Exception as e:
            print(f"[TG POLL ERR] {e}")
            time.sleep(5)

# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    scanner = MomentumScanner()
    tg_thread = threading.Thread(target=poll_telegram, args=(scanner,), daemon=True)
    tg_thread.start()
    scanner.run()
