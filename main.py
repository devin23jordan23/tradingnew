"""
main.py - Andre's Trading Scanner v3.0
10-setup professional scanner. Single file deployment.
"""
import os,sys,time,json,base64,threading,requests,pytz
from datetime import datetime
from collections import defaultdict
from http.server import HTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs,urlencode

SCHWAB_CLIENT_ID=os.environ.get("SCHWAB_CLIENT_ID","")
SCHWAB_CLIENT_SECRET=os.environ.get("SCHWAB_CLIENT_SECRET","")
TELEGRAM_TOKEN=os.environ.get("TELEGRAM_TOKEN","")
TELEGRAM_CHAT_ID=os.environ.get("TELEGRAM_CHAT_ID","")
ET=pytz.timezone("America/New_York")
AUTH_URL="https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL="https://api.schwabapi.com/v1/oauth/token"
REDIRECT="https://127.0.0.1"
TOKEN_FILE="schwab_tokens.json"
BASE="https://api.schwabapi.com/marketdata/v1"
DEFAULT_WATCHLIST=["NVDA","AMD","TSLA","PLTR","AMZN","MU","MSFT","GOOGL","AAPL","AVGO","META","CVX","DELL","RKLB","MRVL","ANET","CRDO","LITE","COHR","COIN","AAOI","XOM","ARM","INTC"]
MIN_SCORE=60
COOLDOWN=15

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: print(f"[ALERT]\n{msg}"); return
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"},timeout=10).raise_for_status(); print(f"[SENT]{datetime.now(ET).strftime('%H:%M')}")
    except Exception as e: print(f"[ERR]{e}")

def _b64(): return base64.b64encode(f"{SCHWAB_CLIENT_ID}:{SCHWAB_CLIENT_SECRET}".encode()).decode()
def _save(t): t["saved_at"]=time.time(); open(TOKEN_FILE,"w").write(json.dumps(t,indent=2))
def _load():
    if not os.path.exists(TOKEN_FILE): return {}
    try: return json.loads(open(TOKEN_FILE).read())
    except: return {}
def _expired(t): return not t or time.time()>t.get("saved_at",0)+t.get("expires_in",1800)-300
def _refresh(t):
    h={"Authorization":f"Basic {_b64()}","Content-Type":"application/x-www-form-urlencoded"}
    r=requests.post(TOKEN_URL,headers=h,data={"grant_type":"refresh_token","refresh_token":t.get("refresh_token","")},timeout=15)
    r.raise_for_status(); n=r.json()
    if "refresh_token" not in n: n["refresh_token"]=t.get("refresh_token")
    _save(n); return n

# ── CLOUD AUTH — manual URL paste via Telegram ───────────────
# Railway runs in the cloud so 127.0.0.1 redirect cannot be caught
# automatically. Instead: bot sends you the login URL, you log in,
# then paste the full redirect URL back to the bot as /auth <url>
# This happens only once — tokens are saved forever after.

_pending_auth = False   # True while waiting for you to paste the URL

def _login():
    global _pending_auth
    _pending_auth = True
    url = f"{AUTH_URL}?{urlencode({'response_type':'code','client_id':SCHWAB_CLIENT_ID,'redirect_uri':REDIRECT,'scope':'readonly'})}"
    print(f"[AUTH] Login required. URL: {url}")
    send_telegram(
        f"🔐 <b>Schwab Authorization Required</b>\n"
        f"{'━'*30}\n"
        f"<b>Step 1:</b> Open this link in your browser:\n"
        f"<code>{url}</code>\n\n"
        f"<b>Step 2:</b> Log in with your Schwab account\n\n"
        f"<b>Step 3:</b> After logging in, your browser will show a blank page or error.\n"
        f"Copy the <b>entire URL</b> from your browser address bar\n"
        f"(it starts with https://127.0.0.1/?code=...)\n\n"
        f"<b>Step 4:</b> Send it to me like this:\n"
        f"<code>/auth https://127.0.0.1/?code=PASTE_FULL_URL_HERE</code>"
    )
    # Wait up to 10 minutes for /auth command
    start = time.time()
    while _pending_auth:
        if time.time() - start > 600:
            send_telegram("⏰ Auth timed out. Will retry in 5 minutes.")
            return None
        time.sleep(2)
    return _load()

def _complete_auth(full_redirect_url):
    """Called when user sends /auth <full_redirect_url>"""
    global _pending_auth
    try:
        parsed = urlparse(full_redirect_url)
        params = parse_qs(parsed.query)
        code   = params.get("code", [None])[0]
        if not code:
            send_telegram("❌ Could not find auth code in that URL. Make sure you copied the full URL.")
            return False
        h = {"Authorization":f"Basic {_b64()}","Content-Type":"application/x-www-form-urlencoded"}
        r = requests.post(TOKEN_URL, headers=h,
                          data={"grant_type":"authorization_code","code":code,"redirect_uri":REDIRECT},
                          timeout=15)
        r.raise_for_status()
        t = r.json()
        _save(t)
        _pending_auth = False
        send_telegram("✅ <b>Schwab connected successfully!</b>\nYour scanner is now live. You won't need to do this again.")
        print("[AUTH] Tokens saved successfully.")
        return True
    except Exception as e:
        send_telegram(f"❌ Auth failed: {e}\n\nTry sending /reauth to start again.")
        print(f"[AUTH ERROR] {e}")
        return False

def tok():
    t=_load()
    if not t:
        t=_login()
        if not t: return ""
    elif _expired(t): t=_refresh(t)
    return t.get("access_token","")

def _hdr(): return {"Authorization":f"Bearer {tok()}","Accept":"application/json"}
def _get(ep,params=None):
    for i in range(2):
        try:
            r=requests.get(f"{BASE}{ep}",headers=_hdr(),params=params or {},timeout=10)
            if r.status_code==401 and i==0: _refresh(_load()); continue
            r.raise_for_status(); return r.json()
        except Exception as e: print(f"[DATA]{e}"); time.sleep(2) if i==0 else None
    return {}

def candles(ticker,m=5):
    now=datetime.now(ET); s=int(now.replace(hour=4,minute=0,second=0,microsecond=0).timestamp()*1000); e=int(now.timestamp()*1000)
    d=_get(f"/pricehistory?symbol={ticker}",{"periodType":"day","period":1,"frequencyType":"minute","frequency":m,"startDate":s,"endDate":e,"needExtendedHoursData":"true"})
    return [{"o":c["open"],"h":c["high"],"l":c["low"],"c":c["close"],"v":c["volume"],"ts":datetime.fromtimestamp(c["datetime"]/1000,tz=ET)} for c in d.get("candles",[])]

def price(ticker):
    try: d=_get(f"/quotes/{ticker}"); q=d.get(ticker,{}).get("quote",{}); return q.get("lastPrice") or q.get("mark")
    except: return None

def pm_high(ticker):
    try:
        now=datetime.now(ET); s=int(now.replace(hour=4,minute=0,second=0,microsecond=0).timestamp()*1000); e=int(now.replace(hour=9,minute=30,second=0,microsecond=0).timestamp()*1000)
        d=_get(f"/pricehistory?symbol={ticker}",{"periodType":"day","period":1,"frequencyType":"minute","frequency":1,"startDate":s,"endDate":e,"needExtendedHoursData":"true"})
        c=d.get("candles",[]); return max(x["high"] for x in c) if c else None
    except: return None

def pm_low(ticker):
    try:
        now=datetime.now(ET); s=int(now.replace(hour=4,minute=0,second=0,microsecond=0).timestamp()*1000); e=int(now.replace(hour=9,minute=30,second=0,microsecond=0).timestamp()*1000)
        d=_get(f"/pricehistory?symbol={ticker}",{"periodType":"day","period":1,"frequencyType":"minute","frequency":1,"startDate":s,"endDate":e,"needExtendedHoursData":"true"})
        c=d.get("candles",[]); return min(x["low"] for x in c) if c else None
    except: return None

def prior_day(ticker):
    try:
        d=_get(f"/pricehistory?symbol={ticker}",{"periodType":"day","period":2,"frequencyType":"daily","frequency":1,"needExtendedHoursData":"false"})
        today=datetime.now(ET).date()
        for c in reversed(d.get("candles",[])):
            if datetime.fromtimestamp(c["datetime"]/1000,tz=ET).date()<today:
                return {"h":c["high"],"l":c["low"],"c":c["close"],"vwap":round((c["high"]+c["low"]+c["close"])/3,2)}
    except: pass
    return {}

def vwap(cs):
    tv,vol=0,0
    for c in cs:
        ts=c.get("ts")
        if ts and (ts.hour<9 or (ts.hour==9 and ts.minute<30)): continue
        tp=(c["h"]+c["l"]+c["c"])/3; tv+=tp*c["v"]; vol+=c["v"]
    return tv/vol if vol else None

def ema(vals,p=9):
    if len(vals)<p: return None
    k=2/(p+1); e=sum(vals[:p])/p
    for v in vals[p:]: e=v*k+e*(1-k)
    return e

def ema_series(vals,p=9):
    if len(vals)<p: return [None]*len(vals)
    k=2/(p+1); res=[None]*(p-1); e=sum(vals[:p])/p; res.append(e)
    for v in vals[p:]: e=v*k+e*(1-k); res.append(e)
    return res

def rh(cs): return [c for c in cs if c.get("ts") and (c["ts"].hour>9 or (c["ts"].hour==9 and c["ts"].minute>=30))]
def av(cs,n=20): r=rh(cs); v=[c["v"] for c in r[-n:]]; return sum(v)/len(v) if v else None
def sh(cs): r=rh(cs); return max(c["h"] for c in r) if r else None
def sl(cs): r=rh(cs); return min(c["l"] for c in r) if r else None
def or5(c1m): bars=[c for c in c1m if c.get("ts") and c["ts"].hour==9 and 30<=c["ts"].minute<35]; return (max(c["h"] for c in bars),min(c["l"] for c in bars)) if bars else (None,None)

def atr(ticker,p=14):
    try:
        d=_get(f"/pricehistory?symbol={ticker}",{"periodType":"day","period":1,"frequencyType":"daily","frequency":1})
        r=d.get("candles",[])
        if len(r)<2: return None
        trs=[max(r[i]["high"]-r[i]["low"],abs(r[i]["high"]-r[i-1]["close"]),abs(r[i]["low"]-r[i-1]["close"])) for i in range(1,len(r))]
        return sum(trs[-p:])/min(len(trs),p)
    except: return None

# ── SETUPS ────────────────────────────────────────────────────

def orb_long(c5,c1,p,vw,pmh):
    oh,ol=or5(c1)
    if not oh: return False,{}
    r=rh(c5);
    if len(r)<2: return False,{}
    a=av(c5); last=r[-1]; prev=r[-2]
    brk=p>oh; abv=p>(vw or 0); vol=last["v"]>(a or 0)*1.3; was_below=prev["c"]<=oh
    if not(brk and abv and was_below): return False,{}
    sc=60+(15 if vol else 0)+(10 if pmh and p>pmh else 0)
    return True,{"setup":"ORB_5M_LONG","dir":"🟢 LONG","trigger":f"Break above OR High ${round(oh,2)}","inval":f"Loss of OR Low ${round(ol,2)}","level":f"5-min OR High: ${round(oh,2)}","vol":"Expanding ✅" if vol else "Weak ⚠️","score":min(sc,100),"action":"Actionable" if vol else "Watch — needs volume","notes":"ORB breakout with volume" if vol else "Break lacks volume — caution"}

def orb_short(c5,c1,p,vw,pml):
    oh,ol=or5(c1)
    if not ol: return False,{}
    r=rh(c5)
    if len(r)<2: return False,{}
    a=av(c5); last=r[-1]; prev=r[-2]
    brk=p<ol; blw=p<(vw or float("inf")); vol=last["v"]>(a or 0)*1.3; was_above=prev["c"]>=ol
    if not(brk and blw and was_above): return False,{}
    sc=60+(15 if vol else 0)+(10 if pml and p<pml else 0)
    return True,{"setup":"ORB_5M_SHORT","dir":"🔴 SHORT","trigger":f"Break below OR Low ${round(ol,2)}","inval":f"Reclaim above OR High ${round(oh,2)}","level":f"5-min OR Low: ${round(ol,2)}","vol":"Expanding ✅" if vol else "Weak ⚠️","score":min(sc,100),"action":"Actionable" if vol else "Watch","notes":"ORB breakdown"}

def pmh_retest(c5,p,vw,pmh):
    if not pmh: return False,{}
    r=rh(c5)
    if len(r)<3: return False,{}
    broke=any(c["h"]>pmh for c in r[:-2])
    if not broke: return False,{}
    near=abs(p-pmh)/pmh<=0.004; above=p>=pmh*0.998; abv_vw=p>(vw or 0)
    a=av(c5); vol_lt=all(c["v"]<(a or float("inf"))*0.9 for c in r[-2:])
    if not(above and abv_vw): return False,{}
    sc=65+(10 if near else 0)+(10 if vol_lt else 0)+(5 if abv_vw else 0)
    return True,{"setup":"PMH_BREAK_RETEST_LONG","dir":"🟢 LONG","trigger":f"Hold above PM High ${round(pmh,2)} + push","inval":f"Loss of ${round(pmh*0.997,2)}","level":f"PM High: ${round(pmh,2)}","vol":"Pullback light ✅" if vol_lt else "Watch volume","score":min(sc,100),"action":"Actionable" if near and abv_vw else "Actionable on retest","notes":"PM high broken earlier — now retesting"}

def pml_retest(c5,p,vw,pml):
    if not pml: return False,{}
    r=rh(c5)
    if len(r)<3: return False,{}
    broke=any(c["l"]<pml for c in r[:-2])
    if not broke: return False,{}
    near=abs(p-pml)/pml<=0.004; below=p<=pml*1.002; blw_vw=p<(vw or float("inf"))
    a=av(c5); vol_lt=all(c["v"]<(a or float("inf"))*0.9 for c in r[-2:])
    if not(below and blw_vw): return False,{}
    sc=65+(10 if near else 0)+(10 if vol_lt else 0)
    return True,{"setup":"PML_BREAK_RETEST_SHORT","dir":"🔴 SHORT","trigger":f"Reject under PM Low ${round(pml,2)}","inval":f"Reclaim ${round(pml*1.003,2)}","level":f"PM Low: ${round(pml,2)}","vol":"Bounce light ✅" if vol_lt else "Watch","score":min(sc,100),"action":"Actionable" if near and blw_vw else "Actionable on retest","notes":"PM low broke earlier — underside retest failing"}

def vwap_reclaim(c5,p,vw):
    if not vw: return False,{}
    r=rh(c5)
    if len(r)<4: return False,{}
    was_below=any(c["c"]<vw for c in r[-5:-1])
    if not was_below: return False,{}
    last=r[-1]; prev=r[-2]
    strong=last["c"]>vw and last["c"]>last["o"]; first=strong and prev["c"]<vw
    if not strong: return False,{}
    a=av(c5); vol=last["v"]>(a or 0)*1.2
    prior_fails=sum(1 for i,c in enumerate(r[-8:-2]) if c["c"]>vw and i>0 and r[-8:-2][i-1]["c"]<vw)
    sc=60+(15 if first else 0)+(15 if vol else 0)-(20 if prior_fails>1 else 0)
    if sc<MIN_SCORE: return False,{}
    return True,{"setup":"VWAP_RECLAIM_LONG","dir":"🟢 LONG","trigger":f"Hold above VWAP ${round(vw,2)} + push through local high","inval":f"Fail back below VWAP ${round(vw,2)}","level":f"VWAP: ${round(vw,2)}","vol":"Expanding ✅" if vol else "Light — watch","score":min(sc,100),"action":"Actionable" if first and vol else "Watch — needs follow-through","notes":"First clean reclaim" if first else "Reclaim attempt — watch for hold"}

def vwap_reject(c5,p,vw):
    if not vw: return False,{}
    r=rh(c5)
    if len(r)<4: return False,{}
    last=r[-1]; prev=r[-2]
    near=abs(prev["h"]-vw)/vw<=0.005; blw=last["c"]<vw; bear=last["c"]<last["o"]; was_below=any(c["c"]<vw for c in r[-6:-3])
    if not(near and blw and bear and was_below): return False,{}
    a=av(c5); vol=last["v"]>(a or 0)*1.1
    sc=65+(10 if vol else 0)+(10 if bear else 0)
    return True,{"setup":"VWAP_REJECT_SHORT","dir":"🔴 SHORT","trigger":f"Break local pivot below VWAP ${round(vw,2)}","inval":f"Acceptance above VWAP ${round(vw*1.003,2)}","level":f"VWAP resistance: ${round(vw,2)}","vol":"Expanding ✅" if vol else "Light","score":min(sc,100),"action":"Actionable","notes":"Rejected at VWAP — rolling over"}

def ema9_pb_long(c5,p,vw):
    r=rh(c5)
    if len(r)<12: return False,{}
    cls=[c["c"] for c in r]; es=ema_series(cls); en=es[-1]
    if not en: return False,{}
    ev=[e for e in es[-5:] if e]; rising=len(ev)>1 and ev[-1]>ev[0]
    abv=p>(vw or 0); last=r[-1]; prev=r[-2]
    touched=last["l"]<=en*1.003 or prev["l"]<=en*1.003
    bouncing=last["c"]>prev["h"] or last["c"]>en
    a=av(c5); lt=last["v"]<(a or float("inf"))*0.85
    if not(rising and abv and touched and bouncing): return False,{}
    sc=65+(10 if lt else 0)+(10 if abv else 0)+(5 if rising else 0)
    return True,{"setup":"EMA9_5M_PULLBACK_LONG","dir":"🟢 LONG","trigger":f"Bounce above ${round(prev['h'],2)} after 9 EMA touch","inval":f"Clean loss of 9 EMA ${round(en,2)}","level":f"9 EMA: ${round(en,2)} | VWAP: ${round(vw,2) if vw else 'N/A'}","vol":"Pullback light ✅" if lt else "Watch volume","score":min(sc,100),"action":"Actionable","notes":"Rising EMA, controlled pullback, bouncing"}

def ema9_pb_short(c5,p,vw):
    r=rh(c5)
    if len(r)<12: return False,{}
    cls=[c["c"] for c in r]; es=ema_series(cls); en=es[-1]
    if not en: return False,{}
    ev=[e for e in es[-5:] if e]; falling=len(ev)>1 and ev[-1]<ev[0]
    blw=p<(vw or float("inf")); last=r[-1]; prev=r[-2]
    touched=last["h"]>=en*0.997 or prev["h"]>=en*0.997
    rejecting=last["c"]<last["o"] and last["c"]<en
    a=av(c5); lt=last["v"]<(a or float("inf"))*0.85
    if not(falling and blw and touched and rejecting): return False,{}
    sc=65+(10 if lt else 0)+(10 if blw else 0)+(5 if falling else 0)
    return True,{"setup":"EMA9_5M_PULLBACK_SHORT","dir":"🔴 SHORT","trigger":f"Break below ${round(prev['l'],2)} after EMA rejection","inval":f"Reclaim through 9 EMA ${round(en,2)}","level":f"9 EMA resistance: ${round(en,2)}","vol":"Bounce light ✅" if lt else "Watch","score":min(sc,100),"action":"Actionable","notes":"Falling EMA, weak bounce, rejecting"}

def flag_long(c5,p,vw):
    r=rh(c5)
    if len(r)<8: return False,{}
    a=av(c5); imp=None
    for c in r[-10:-3]:
        if (c["c"]-c["o"])>0 and c["v"]>(a or 0)*1.5: imp=c; break
    if not imp: return False,{}
    cons=r[-5:]; fh=max(c["h"] for c in cons); fl=min(c["l"] for c in cons)
    fr=fh-fl; is_=imp["c"]-imp["o"]; tight=fr<is_*0.5; abv=p>(vw or 0)
    last=r[-1]; brk=last["c"]>fh and last["c"]>r[-2]["h"]; vol=last["v"]>(a or 0)*1.2
    cdv=all(c["v"]<(a or float("inf"))*0.8 for c in cons[:-1])
    if not(tight and brk and abv): return False,{}
    sc=65+(10 if cdv else 0)+(15 if vol else 0)+(5 if tight else 0)
    return True,{"setup":"FLAG_BREAKOUT_LONG","dir":"🟢 LONG","trigger":f"Break above flag high ${round(fh,2)}","inval":f"Loss of flag low ${round(fl,2)}","level":f"Flag: ${round(fl,2)}–${round(fh,2)}","vol":("Dry-up + expansion ✅" if (cdv and vol) else "Watch volume"),"score":min(sc,100),"action":"Actionable" if(vol and tight) else "Watch","notes":f"Flag range ${round(fr,2)} vs impulse ${round(is_,2)}"}

def pdh_retest(c5,p,vw,pdh):
    if not pdh: return False,{}
    r=rh(c5)
    if len(r)<3: return False,{}
    if not any(c["h"]>pdh for c in r[:-2]): return False,{}
    near=abs(p-pdh)/pdh<=0.005; above=p>=pdh*0.998; abv=p>(vw or 0)
    a=av(c5); lt=all(c["v"]<(a or float("inf"))*0.9 for c in r[-2:])
    if not(above and abv): return False,{}
    sc=70+(10 if near else 0)+(10 if lt else 0)+(5 if abv else 0)
    return True,{"setup":"PDH_BREAK_RETEST_LONG","dir":"🟢 LONG","trigger":f"Reclaim above PDH ${round(pdh,2)} + push","inval":f"Loss of ${round(pdh*0.997,2)}","level":f"Prior Day High: ${round(pdh,2)}","vol":"Pullback light ✅" if lt else "Watch","score":min(sc,100),"action":"Actionable" if near and abv else "Actionable on retest","notes":"Daily breakout — institutional level"}

def pdl_retest(c5,p,vw,pdl):
    if not pdl: return False,{}
    r=rh(c5)
    if len(r)<3: return False,{}
    if not any(c["l"]<pdl for c in r[:-2]): return False,{}
    near=abs(p-pdl)/pdl<=0.005; below=p<=pdl*1.002; blw=p<(vw or float("inf"))
    a=av(c5); lt=all(c["v"]<(a or float("inf"))*0.9 for c in r[-2:])
    if not(below and blw): return False,{}
    sc=70+(10 if near else 0)+(10 if lt else 0)
    return True,{"setup":"PDL_BREAK_RETEST_SHORT","dir":"🔴 SHORT","trigger":f"Reject under PDL ${round(pdl,2)} + break low","inval":f"Reclaim ${round(pdl*1.003,2)}","level":f"Prior Day Low: ${round(pdl,2)}","vol":"Bounce light ✅" if lt else "Watch","score":min(sc,100),"action":"Actionable" if near and blw else "Actionable on retest","notes":"Prior day low broke — institutional breakdown"}

def fmt(ticker,d):
    sc=d.get("score",0)
    em="🔥" if sc>=85 else "✅" if sc>=70 else "⚠️"
    return "\n".join([
        f"{em} <b>{ticker} — {d.get('setup')}</b>  {d.get('dir')}",
        f"Confidence: <b>{sc}/100</b>  |  {d.get('action')}",
        "━"*30,
        f"📍 <b>Trigger:</b> {d.get('trigger')}",
        f"🛑 <b>Stop:</b> {d.get('inval')}",
        f"🔑 <b>Level:</b> {d.get('level')}",
        f"📊 <b>Volume:</b> {d.get('vol')}",
        f"📝 {d.get('notes','')}",
        "━"*30,
        f"⏰ {datetime.now(ET).strftime('%I:%M %p ET')}",
        f"👉 {d.get('action')} — review before entry"
    ])

class Scanner:
    def __init__(self):
        self.wl=list(DEFAULT_WATCHLIST); self.pmh={}; self.pml={}; self.pr={}
        self.pm_dt=None; self.pr_dt=None; self.last=defaultdict(lambda:None)
        self.earnings=set(); self.obs={}; self.opt={}; self.opt_h={}
    def is_mkt(self):
        n=datetime.now(ET)
        if n.weekday()>=5: return False
        return n.replace(hour=9,minute=25,second=0,microsecond=0)<=n<=n.replace(hour=16,minute=5,second=0,microsecond=0)
    def refresh(self):
        today=datetime.now(ET).date()
        if self.pm_dt!=today:
            print("[SCAN] Refreshing PM data...")
            for t in self.wl: self.pmh[t]=pm_high(t); self.pml[t]=pm_low(t); time.sleep(0.5)
            self.pm_dt=today
        if self.pr_dt!=today:
            print("[SCAN] Refreshing prior day...")
            for t in self.wl: self.pr[t]=prior_day(t); time.sleep(0.5)
            self.pr_dt=today
    def can_alert(self,t,s):
        k=f"{t}:{s}"; last=self.last[k]
        return last is None or (datetime.now(ET)-last).total_seconds()/60>=COOLDOWN
    def scan(self,ticker):
        alerts=[]
        try:
            c5=candles(ticker,5); c1=candles(ticker,1)
            if not c5 or not c1: return alerts
            p=price(ticker)
            if not p: return alerts
            vw=vwap(c5); pmh_v=self.pmh.get(ticker); pml_v=self.pml.get(ticker)
            pd=self.pr.get(ticker,{}); pdh=pd.get("h"); pdl=pd.get("l")
            setups=[
                ("ORB_5M_LONG",      lambda: orb_long(c5,c1,p,vw,pmh_v)),
                ("ORB_5M_SHORT",     lambda: orb_short(c5,c1,p,vw,pml_v)),
                ("PMH_RETEST_LONG",  lambda: pmh_retest(c5,p,vw,pmh_v)),
                ("PML_RETEST_SHORT", lambda: pml_retest(c5,p,vw,pml_v)),
                ("VWAP_RECLAIM",     lambda: vwap_reclaim(c5,p,vw)),
                ("VWAP_REJECT",      lambda: vwap_reject(c5,p,vw)),
                ("EMA9_PB_LONG",     lambda: ema9_pb_long(c5,p,vw)),
                ("EMA9_PB_SHORT",    lambda: ema9_pb_short(c5,p,vw)),
                ("FLAG_LONG",        lambda: flag_long(c5,p,vw)),
                ("PDH_RETEST_LONG",  lambda: pdh_retest(c5,p,vw,pdh)),
                ("PDL_RETEST_SHORT", lambda: pdl_retest(c5,p,vw,pdl)),
            ]
            for name,fn in setups:
                try:
                    ok,d=fn()
                    if ok and d.get("score",0)>=MIN_SCORE and self.can_alert(ticker,name):
                        alerts.append((name,d))
                except Exception as e: print(f"[SETUP ERR]{name}:{e}")
        except Exception as e: print(f"[SCAN ERR]{ticker}:{e}")
        return alerts
    def cmd(self,command):
        global MIN_SCORE
        pts=command.strip().split(); c=pts[0].lower() if pts else ""
        if c=="/watch" and len(pts)==2:
            t=pts[1].upper()
            if t not in self.wl: self.wl.append(t)
            send_telegram(f"✅ Added {t} | Watching {len(self.wl)} stocks")
        elif c=="/remove" and len(pts)==2:
            t=pts[1].upper(); self.wl=[x for x in self.wl if x!=t]
            send_telegram(f"🗑️ Removed {t}")
        elif c=="/list":
            send_telegram(f"📋 Watching ({len(self.wl)}):\n{', '.join(self.wl)}")
        elif c=="/status":
            send_telegram(f"📊 <b>Scanner v3.0</b>\nStocks: {len(self.wl)} | Min score: {MIN_SCORE}/100 | Cooldown: {COOLDOWN}min\nEarnings: {', '.join(self.earnings) or 'none'}\nOrder blocks: {len(self.obs)}")
        elif c=="/setups":
            send_telegram("📊 <b>Active Setups</b>\n1. ORB Long\n2. ORB Short\n3. PM High Retest Long\n4. PM Low Retest Short\n5. VWAP Reclaim Long\n6. VWAP Reject Short\n7. 9 EMA Pullback Long\n8. 9 EMA Pullback Short\n9. Flag Breakout Long\n10. PDH Retest Long\n11. PDL Retest Short")
        elif c=="/threshold" and len(pts)==2:
            MIN_SCORE=int(pts[1]); send_telegram(f"⚙️ Min score: {MIN_SCORE}/100")
        elif c=="/ob" and len(pts)==4:
            t=pts[1].upper(); self.obs[t]=(float(pts[2]),float(pts[3])); send_telegram(f"🧱 OB set {t}: ${pts[2]}–${pts[3]}")
        elif c=="/earnings" and len(pts)==2:
            t=pts[1].upper(); self.earnings.add(t); send_telegram(f"📋 {t} flagged earnings")
        elif c=="/auth" and len(pts)>=2:
            full_url=" ".join(pts[1:])
            _complete_auth(full_url)
        elif c=="/reauth":
            _save({})  # clear tokens
            send_telegram("🔄 Tokens cleared. Starting fresh auth...")
            threading.Thread(target=_login,daemon=True).start()
        else:
            send_telegram("Commands:\n/watch TICKER\n/remove TICKER\n/list\n/status\n/setups\n/threshold 65\n/ob TICKER LOW HIGH\n/earnings TICKER\n/reauth")
    def run(self):
        print("[SCANNER] v3.0 starting — 11 setups")
        send_telegram(f"🤖 <b>Scanner v3.0 Online</b>\n{'━'*28}\nWatching <b>{len(self.wl)} stocks</b> | 11 setups | Score ≥{MIN_SCORE}/100\n{'━'*28}\nORB L/S · PM High/Low Retest · VWAP Reclaim/Reject · 9 EMA PB L/S · Flag · PDH/PDL Retest\n\nCommands: /status /setups /watch /remove /list /threshold /ob /earnings")
        while True:
            if not self.is_mkt(): print("[SCAN] Outside hours. Sleep 5m."); time.sleep(300); continue
            self.refresh()
            for t in list(self.wl):
                try:
                    print(f"[SCAN] {t}...")
                    for name,d in self.scan(t):
                        send_telegram(fmt(t,d))
                        self.last[f"{t}:{name}"]=datetime.now(ET)
                        time.sleep(1)
                except Exception as e: print(f"[ERR]{t}:{e}")
                time.sleep(0.5)
            print("[SCAN] Cycle done. Sleep 60s."); time.sleep(60)

def listen(sc):
    offset=None
    while True:
        try:
            r=requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",params={"timeout":30,"offset":offset},timeout=35).json()
            for u in r.get("result",[]):
                offset=u["update_id"]+1; txt=u.get("message",{}).get("text","")
                if txt.startswith("/"): print(f"[CMD]{txt}"); sc.cmd(txt)
        except Exception as e: print(f"[CMD ERR]{e}"); time.sleep(5)

if __name__=="__main__":
    print(f"[MAIN] v3.0 | Schwab:{'OK' if SCHWAB_CLIENT_ID else 'MISSING'} | Telegram:{'OK' if TELEGRAM_TOKEN else 'MISSING'}")
    sc=Scanner()
    threading.Thread(target=listen,args=(sc,),daemon=True).start()
    sc.run()
