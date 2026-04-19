"""
main.py - Complete Trading Scanner (Single File Version)
Everything in one file. No folders, no imports, no nesting issues.
"""

import os, sys, time, json, base64, threading, requests, pytz
from datetime import datetime
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

SCHWAB_CLIENT_ID     = os.environ.get("SCHWAB_CLIENT_ID", "")
SCHWAB_CLIENT_SECRET = os.environ.get("SCHWAB_CLIENT_SECRET", "")
TELEGRAM_TOKEN       = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")

ET         = pytz.timezone("America/New_York")
AUTH_URL   = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL  = "https://api.schwabapi.com/v1/oauth/token"
REDIRECT   = "https://127.0.0.1"
TOKEN_FILE = "schwab_tokens.json"
BASE       = "https://api.schwabapi.com/marketdata/v1"
DEFAULT_WATCHLIST = ["NVDA","AMD","TSLA","PLTR","AMZN","MU","MSFT","AAPL","META","DELL"]
MIN_SCORE_TO_ALERT = 3
ALERT_COOLDOWN_MINUTES = 15

# ── TELEGRAM ──────────────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[ALERT]\n{message}"); return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10).raise_for_status()
        print(f"[SENT] {datetime.now(ET).strftime('%H:%M:%S')}")
    except Exception as e:
        print(f"[ALERT ERROR] {e}")

# ── SCHWAB AUTH ───────────────────────────────────────────────
def _b64creds():
    return base64.b64encode(f"{SCHWAB_CLIENT_ID}:{SCHWAB_CLIENT_SECRET}".encode()).decode()

def _save_tokens(t):
    t["saved_at"] = time.time()
    open(TOKEN_FILE,"w").write(json.dumps(t, indent=2))
    print("[AUTH] Tokens saved.")

def _load_tokens():
    if not os.path.exists(TOKEN_FILE): return {}
    try: return json.loads(open(TOKEN_FILE).read())
    except: return {}

def _expired(t):
    return not t or time.time() > t.get("saved_at",0) + t.get("expires_in",1800) - 300

def _refresh(t):
    print("[AUTH] Refreshing token...")
    h = {"Authorization": f"Basic {_b64creds()}", "Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(TOKEN_URL, headers=h, data={"grant_type":"refresh_token","refresh_token":t.get("refresh_token","")}, timeout=15)
    r.raise_for_status()
    new = r.json()
    if "refresh_token" not in new: new["refresh_token"] = t.get("refresh_token")
    _save_tokens(new); return new

_auth_code = None

class _CB(BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code
        p = parse_qs(urlparse(self.path).query)
        if "code" in p:
            _auth_code = p["code"][0]
            self.send_response(200); self.end_headers()
            self.wfile.write(b"<h2 style='color:green'>Auth successful! Close this tab.</h2>")
        else:
            self.send_response(400); self.end_headers()
    def log_message(self, *a): pass

def _first_login():
    global _auth_code; _auth_code = None
    url = f"{AUTH_URL}?{urlencode({'response_type':'code','client_id':SCHWAB_CLIENT_ID,'redirect_uri':REDIRECT,'scope':'readonly'})}"
    print(f"\n{'='*50}\nSCHWAB LOGIN REQUIRED\nOpen this URL:\n{url}\n{'='*50}\n")
    send_telegram(f"🔐 <b>Schwab Login Required</b>\n\nOpen this URL in your browser:\n<code>{url}</code>")
    threading.Thread(target=lambda: HTTPServer(("127.0.0.1",443),_CB).handle_request(), daemon=True).start()
    start = time.time()
    while _auth_code is None:
        if time.time()-start > 300: raise TimeoutError("Login timeout")
        time.sleep(1)
    h = {"Authorization": f"Basic {_b64creds()}", "Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(TOKEN_URL, headers=h, data={"grant_type":"authorization_code","code":_auth_code,"redirect_uri":REDIRECT}, timeout=15)
    r.raise_for_status(); t = r.json(); _save_tokens(t)
    print("[AUTH] Login complete!"); return t

def get_access_token():
    t = _load_tokens()
    if not t: t = _first_login()
    elif _expired(t): t = _refresh(t)
    return t.get("access_token","")

# ── DATA ──────────────────────────────────────────────────────
def _hdr(): return {"Authorization": f"Bearer {get_access_token()}", "Accept": "application/json"}

def _get(ep, params=None):
    for attempt in range(2):
        try:
            r = requests.get(f"{BASE}{ep}", headers=_hdr(), params=params or {}, timeout=10)
            if r.status_code == 401 and attempt == 0: _refresh(_load_tokens()); continue
            r.raise_for_status(); return r.json()
        except Exception as e:
            print(f"[DATA] {e}")
            if attempt == 0: time.sleep(2)
    return {}

def get_candles(ticker, multiplier=5):
    now = datetime.now(ET)
    s   = int(now.replace(hour=4,minute=0,second=0,microsecond=0).timestamp()*1000)
    e   = int(now.timestamp()*1000)
    d   = _get(f"/pricehistory?symbol={ticker}", {"periodType":"day","period":1,"frequencyType":"minute","frequency":multiplier,"startDate":s,"endDate":e,"needExtendedHoursData":"true"})
    return [{"open":c["open"],"high":c["high"],"low":c["low"],"close":c["close"],"volume":c["volume"],
             "timestamp":datetime.fromtimestamp(c["datetime"]/1000,tz=ET)} for c in d.get("candles",[])]

def get_price(ticker):
    try:
        d = _get(f"/quotes/{ticker}")
        q = d.get(ticker,{}).get("quote",{})
        return q.get("lastPrice") or q.get("mark")
    except: return None

def get_pm_high(ticker):
    try:
        now = datetime.now(ET)
        s = int(now.replace(hour=4,minute=0,second=0,microsecond=0).timestamp()*1000)
        e = int(now.replace(hour=9,minute=30,second=0,microsecond=0).timestamp()*1000)
        d = _get(f"/pricehistory?symbol={ticker}", {"periodType":"day","period":1,"frequencyType":"minute","frequency":1,"startDate":s,"endDate":e,"needExtendedHoursData":"true"})
        c = d.get("candles",[])
        return max(x["high"] for x in c) if c else None
    except: return None

def get_prior_day(ticker):
    try:
        d = _get(f"/pricehistory?symbol={ticker}", {"periodType":"day","period":2,"frequencyType":"daily","frequency":1,"needExtendedHoursData":"false"})
        today = datetime.now(ET).date()
        for c in reversed(d.get("candles",[])):
            if datetime.fromtimestamp(c["datetime"]/1000,tz=ET).date() < today:
                v = (c["high"]+c["low"]+c["close"])/3
                return {"high":c["high"],"low":c["low"],"close":c["close"],"vwap":round(v,2),"volume":c["volume"]}
    except: pass
    return {}

def get_open_price(ticker):
    try:
        for c in get_candles(ticker, multiplier=1):
            ts = c.get("timestamp")
            if ts and ts.hour==9 and ts.minute==30: return c["open"]
    except: pass
    return None

def calc_vwap(candles):
    tv, vol = 0, 0
    for c in candles:
        ts = c.get("timestamp")
        if ts and (ts.hour < 9 or (ts.hour==9 and ts.minute<30)): continue
        tp = (c["high"]+c["low"]+c["close"])/3; tv += tp*c["volume"]; vol += c["volume"]
    return tv/vol if vol else None

def calc_ema(vals, p=9):
    if len(vals)<p: return None
    k=2/(p+1); e=sum(vals[:p])/p
    for v in vals[p:]: e=v*k+e*(1-k)
    return e

def calc_atr(ticker, p=14):
    try:
        d = _get(f"/pricehistory?symbol={ticker}", {"periodType":"day","period":1,"frequencyType":"daily","frequency":1})
        r = d.get("candles",[])
        if len(r)<2: return None
        trs = [max(r[i]["high"]-r[i]["low"],abs(r[i]["high"]-r[i-1]["close"]),abs(r[i]["low"]-r[i-1]["close"])) for i in range(1,len(r))]
        return sum(trs[-p:])/min(len(trs),p)
    except: return None

def avg_vol(candles, n=20):
    rh = [c for c in candles if c.get("timestamp") and (c["timestamp"].hour>9 or (c["timestamp"].hour==9 and c["timestamp"].minute>=30))]
    v  = [c["volume"] for c in rh[-n:]]
    return sum(v)/len(v) if v else None

def session_high(candles):
    rh = [c for c in candles if c.get("timestamp") and (c["timestamp"].hour>9 or (c["timestamp"].hour==9 and c["timestamp"].minute>=30))]
    return max(c["high"] for c in rh) if rh else None

# ── CONDITIONS ────────────────────────────────────────────────
def cond_pm_high(p, pmh, tol=0.002):
    if not pmh: return False,{}
    d=(p-pmh)/pmh; return d>=-tol, {"condition":"Above PM High","pm_high":round(pmh,2),"current":round(p,2),"diff_pct":round(d*100,2)}

def cond_ema(p, ema, tol=0.003):
    if not ema: return False,{}
    d=abs(p-ema)/ema; return d<=tol, {"condition":"9 EMA Touch","ema":round(ema,2),"current":round(p,2),"diff_pct":round(d*100,2)}

def cond_vwap(p, vwap):
    if not vwap: return False,{}
    above=p>vwap; return True, {"condition":"Above VWAP" if above else "Below VWAP","vwap":round(vwap,2),"current":round(p,2),"diff_pct":round(((p-vwap)/vwap)*100,2)}

def cond_vol(vol, av, thr=1.5):
    if not av: return False,{}
    r=vol/av; return r>=thr, {"condition":"Elevated Volume","ratio":round(r,2),"current_vol":int(vol),"avg_vol":int(av)}

def cond_pdh(p, pdh, vol, av, thr=1.5):
    if not pdh: return False,{}
    vr=vol/av if av else 0; return p>pdh and vr>=thr, {"condition":"Prior Day High Break","prior_day_high":round(pdh,2),"current":round(p,2),"vol_ratio":round(vr,2)}

def cond_atr_retrace(p, op, atr, sh, pct=0.50):
    if not atr or not sh: return False,{}
    if sh < op+atr*0.5: return False,{}
    m=sh-op; pb=(sh-p)/m if m else 0
    return abs(pb-pct)<=0.05, {"condition":f"{int(pct*100)}% Retrace from Session High","session_high":round(sh,2),"current":round(p,2),"pullback_pct":round(pb*100,2)}

def cond_ob(p, lo, hi, tol=0.005):
    if lo is None or hi is None: return False,{}
    return (lo*(1-tol))<=p<=(hi*(1+tol)), {"condition":"Order Block Return","ob_zone":f"${round(lo,2)}–${round(hi,2)}","current":round(p,2)}

def cond_opt(op, sh, levels=(0.50,0.625)):
    if not sh: return False,{}
    for lvl in levels:
        t=sh*(1-lvl)
        if abs(op-t)/sh<=0.03: return True, {"condition":f"Option {int(lvl*100)}% Retrace","session_high":round(sh,2),"current":round(op,2),"retrace_level":f"{int(lvl*100)}%"}
    return False,{}

def cond_flag(candles, min_legs=2):
    if len(candles)<min_legs+2: return False,{}
    lows=[c["low"] for c in candles[-5:]]; hls=sum(1 for i in range(1,len(lows)) if lows[i]>lows[i-1])
    rngs=[c["high"]-c["low"] for c in candles[-5:]]; comp=rngs[-1]<rngs[0]
    return hls>=min_legs and comp, {"condition":"Bull Flag (Higher Lows)","higher_lows_count":hls,"compressing":comp}

def cond_earnings_vwap(p, pv, tol=0.008):
    if not pv: return False,{}
    d=abs(p-pv)/pv; return d<=tol, {"condition":"Earnings Gap → Prior VWAP Pullback","prior_vwap":round(pv,2),"current":round(p,2),"distance_pct":round(d*100,2)}

def score(triggered):
    W={"order block":2,"option":2,"earnings":2,"above pm high":1,"9 ema":1,"vwap":1,"volume":1,"prior day":1,"retrace":1,"bull flag":1}
    s=sum(next((w for k,w in W.items() if k in d.get("condition","").lower()),1) for d in triggered)
    return s, ("A+" if s>=7 else "A" if s>=5 else "B" if s>=3 else "BELOW_THRESHOLD")

# ── FORMAT ALERT ─────────────────────────────────────────────
def fmt_alert(ticker, grade, sc, triggered):
    ICONS={"above pm high":"🔼","9 ema":"📈","vwap":"📊","volume":"🔥","prior day":"💥","retrace":"🎯","order block":"🧱","option":"📉","bull flag":"🚩","earnings":"📋"}
    em={"A+":"🔥","A":"✅","B":"⚠️"}.get(grade,"📊")
    lines=[f"{em} <b>SETUP ALERT — {ticker}</b>",f"Grade: <b>{grade}</b>  |  {sc} pts","━"*28]
    for d in triggered:
        c=d.get("condition",""); icon=next((v for k,v in ICONS.items() if k in c.lower()),"•")
        lines.append(f"{icon} <b>{c}</b>")
    lines+=["━"*28,f"⏰ {datetime.now(ET).strftime('%I:%M %p ET')}","👉 Manual review → place trade if confirmed"]
    return "\n".join(lines)

# ── SCANNER ───────────────────────────────────────────────────
class Scanner:
    def __init__(self):
        self.watchlist=list(DEFAULT_WATCHLIST); self.order_blocks={}; self.flagged_drives=set()
        self.option_watches={}; self.option_highs={}; self.last_alert=defaultdict(lambda:None)
        self.earnings=set(); self.pm_highs={}; self.pm_date=None; self.prior={}; self.prior_date=None

    def is_mkt(self):
        now=datetime.now(ET)
        if now.weekday()>=5: return False
        return now.replace(hour=9,minute=25,second=0,microsecond=0)<=now<=now.replace(hour=16,minute=5,second=0,microsecond=0)

    def refresh(self):
        today=datetime.now(ET).date()
        if self.pm_date!=today:
            print("[SCANNER] Refreshing PM highs...")
            for t in self.watchlist: self.pm_highs[t]=get_pm_high(t); time.sleep(0.5)
            self.pm_date=today
        if self.prior_date!=today:
            print("[SCANNER] Refreshing prior day data...")
            for t in self.watchlist: self.prior[t]=get_prior_day(t); time.sleep(0.5)
            self.prior_date=today

    def can_alert(self,ticker,key):
        last=self.last_alert[f"{ticker}:{key}"]
        return last is None or (datetime.now(ET)-last).total_seconds()/60>=ALERT_COOLDOWN_MINUTES

    def scan(self,ticker):
        triggered=[]; candles=get_candles(ticker)
        if not candles: return []
        p=get_price(ticker)
        if not p: return []
        closes=[c["close"] for c in candles]; ema9=calc_ema(closes); vwap=calc_vwap(candles)
        atr=calc_atr(ticker); av=avg_vol(candles); sh=session_high(candles)
        op=get_open_price(ticker); pmh=self.pm_highs.get(ticker); prior=self.prior.get(ticker,{})
        cvol=candles[-1]["volume"] if candles else 0
        for fn,args in [(cond_pm_high,(p,pmh)),(cond_ema,(p,ema9)),(cond_vol,(cvol,av)),(cond_pdh,(p,prior.get("high"),cvol,av))]:
            ok,d=fn(*args)
            if ok: triggered.append(d)
        if vwap:
            ok,d=cond_vwap(p,vwap)
            if ok: triggered.append(d)
        if op and atr and sh:
            ok,d=cond_atr_retrace(p,op,atr,sh)
            if ok: triggered.append(d)
        if ticker in self.order_blocks:
            ok,d=cond_ob(p,*self.order_blocks[ticker])
            if ok: triggered.append(d)
        if ticker in self.flagged_drives:
            ok,d=cond_flag(candles[-6:])
            if ok: triggered.append(d)
        if ticker in self.earnings:
            ok,d=cond_earnings_vwap(p,prior.get("vwap"))
            if ok: triggered.append(d)
        if ticker in self.option_watches:
            ct=self.option_watches[ticker]; oq=None
            try:
                d2=_get(f"/quotes/{ct}"); q=d2.get(ct,{}).get("quote",{}); b,a=q.get("bid",0),q.get("ask",0)
                oq=(b+a)/2 if b and a else q.get("lastPrice")
            except: pass
            if oq:
                if oq>self.option_highs.get(ct,0): self.option_highs[ct]=oq
                ok,d=cond_opt(oq,self.option_highs.get(ct))
                if ok: triggered.append(d)
        return triggered

    def cmd(self,command):
        parts=command.strip().split(); cmd=parts[0].lower() if parts else ""
        if cmd=="/ob" and len(parts)==4:
            t=parts[1].upper(); self.order_blocks[t]=(float(parts[2]),float(parts[3]))
            send_telegram(f"🧱 Order block set for {t}: ${parts[2]}–${parts[3]}")
        elif cmd=="/watch" and len(parts)==2:
            t=parts[1].upper()
            if t not in self.watchlist: self.watchlist.append(t)
            send_telegram(f"✅ Added {t}")
        elif cmd=="/remove" and len(parts)==2:
            t=parts[1].upper(); self.watchlist=[x for x in self.watchlist if x!=t]
            send_telegram(f"🗑️ Removed {t}")
        elif cmd=="/status":
            send_telegram(f"📋 Watching: {', '.join(self.watchlist)}\n🧱 OBs: {self.order_blocks}")
        elif cmd=="/flag" and len(parts)==2:
            t=parts[1].upper(); self.flagged_drives.add(t); send_telegram(f"🚩 Drive flagged: {t}")
        elif cmd=="/earnings" and len(parts)==2:
            t=parts[1].upper(); self.earnings.add(t); send_telegram(f"📋 Earnings flagged: {t}")
        elif cmd=="/option" and len(parts)==3:
            self.option_watches[parts[1].upper()]=parts[2]; send_telegram(f"📉 Watching option: {parts[2]}")
        else:
            send_telegram("Commands: /watch /remove /ob /flag /earnings /option /status")

    def run(self):
        print("[SCANNER] Starting..."); send_telegram(f"🤖 <b>Scanner Online</b>\nWatching: {', '.join(self.watchlist)}\nScanning every 60s | Market hours only\nCommands: /watch /remove /ob /flag /earnings /option /status")
        while True:
            if not self.is_mkt(): print("[SCANNER] Outside hours. Sleep 5m."); time.sleep(300); continue
            self.refresh()
            for ticker in list(self.watchlist):
                try:
                    print(f"[SCANNER] {ticker}..."); triggered=self.scan(ticker)
                    if not triggered: continue
                    sc,grade=score(triggered)
                    if sc<MIN_SCORE_TO_ALERT: continue
                    key="|".join(d.get("condition","") for d in triggered)
                    if not self.can_alert(ticker,key): continue
                    send_telegram(fmt_alert(ticker,grade,sc,triggered))
                    self.last_alert[f"{ticker}:{key}"]=datetime.now(ET)
                except Exception as e: print(f"[ERROR] {ticker}: {e}")
                time.sleep(0.5)
            print("[SCANNER] Cycle done. Sleep 60s."); time.sleep(60)

# ── ENTRY ─────────────────────────────────────────────────────
def listen(sc):
    offset=None
    while True:
        try:
            r=requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",params={"timeout":30,"offset":offset},timeout=35).json()
            for u in r.get("result",[]):
                offset=u["update_id"]+1; txt=u.get("message",{}).get("text","")
                if txt.startswith("/"): print(f"[CMD] {txt}"); sc.cmd(txt)
        except Exception as e: print(f"[CMD ERR] {e}"); time.sleep(5)

if __name__=="__main__":
    print(f"[MAIN] Starting | Schwab: {'OK' if SCHWAB_CLIENT_ID else 'MISSING'} | Telegram: {'OK' if TELEGRAM_TOKEN else 'MISSING'}")
    sc=Scanner()
    threading.Thread(target=listen,args=(sc,),daemon=True).start()
    sc.run()
