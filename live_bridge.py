#!/usr/bin/env python3
"""
Liquidity Vacuum Terminal - live bridge (standard library only, Python 3.9+)

Runs on your own computer and does three jobs for the terminal:
  1. Streams live NSE quotes for all F&O stocks (Angel One SmartAPI or Dhan)  ->  /api/quotes
  2. Fetches real option chains (LTP, IV, OI) on request      ->  /api/chain
  3. Downloads missing NSE bhavcopy days so levels stay fresh  ->  /api/eod
  4. Sends Telegram alerts when the terminal reports a trigger ->  /api/alert

Start:   python live_bridge.py            (live, needs broker keys in config.env)
Demo:    python live_bridge.py --demo     (random quotes, no broker needed)
Then open http://localhost:8765 in your browser.
"""
import argparse, base64, csv, hmac, io, json, math, os, random, struct, sys, threading, time, urllib.request, urllib.error, zipfile
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
HTML = ROOT / "liquidity_vacuum_terminal.html"
CFG = ROOT / "config.env"
CACHE = ROOT / "cache"; CACHE.mkdir(exist_ok=True)
IST = timezone(timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
      "Accept": "*/*", "Referer": "https://www.nseindia.com/"}

def log(*a): print(datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)

def load_cfg():
    cfg = {"PORT": "8765", "QUOTE_INTERVAL_SECONDS": "3"}
    if CFG.exists():
        for line in CFG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("BROKER", "ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_MPIN", "ANGEL_TOTP_SECRET", "DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_TOPIC_THREAD_ID",
              "QUOTE_INTERVAL_SECONDS", "PORT", "APP_PASSWORD", "SERVER_ALERTS", "MIN_SCORE", "TOP_PER_SIDE", "TARGET_DELTA", "DEMO"):
        if os.environ.get(k): cfg[k] = os.environ[k]   # cloud (Render) env vars win over config.env
    return cfg

def embedded_data():
    t = HTML.read_text(encoding="utf-8")
    a = t.index('id="d">') + 7; b = t.index("</script>", a)
    return json.loads(t[a:b].replace("<\\/", "</"))

def http(url, body=None, headers=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

# ---------------------------------------------------------------- brokers
# Every broker gives the same three things: connect(), quotes() -> {SYM: {ltp, open, high, low, prev_close, volume}}, chain(sym, expiry)
class BrokerError(Exception): pass

def totp(secret):
    """RFC 6238 six-digit code, same as Google Authenticator."""
    sec = secret.strip().replace(" ", "").upper(); key = base64.b32decode(sec + "=" * (-len(sec) % 8))
    h = hmac.new(key, struct.pack(">Q", int(time.time()) // 30), "sha1").digest(); o = h[-1] & 15
    return f"{(struct.unpack('>I', h[o:o+4])[0] & 0x7fffffff) % 1000000:06d}"

class AngelOne:
    name = "Angel One"
    BASE = "https://apiconnect.angelone.in"
    MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    hint = "check ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_MPIN and ANGEL_TOTP_SECRET"

    def __init__(self, cfg, symbols):
        self.key, self.client = cfg.get("ANGEL_API_KEY", ""), cfg.get("ANGEL_CLIENT_CODE", "")
        self.mpin, self.secret = cfg.get("ANGEL_MPIN", ""), cfg.get("ANGEL_TOTP_SECRET", "")
        if not all([self.key, self.client, self.mpin, self.secret]) or "YOUR" in self.key:
            raise BrokerError("Add ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_MPIN, ANGEL_TOTP_SECRET to config.env (or Render environment)")
        self.symbols, self.jwt, self.lock, self.last = symbols, None, threading.Lock(), 0.0

    def _h(self):
        h = {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB",
             "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1", "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": self.key}
        if self.jwt: h["Authorization"] = "Bearer " + self.jwt
        return h

    def login(self):
        self.jwt = None
        d = json.loads(http(self.BASE + "/rest/auth/angelbroking/user/v1/loginByPassword",
                            {"clientcode": self.client, "password": self.mpin, "totp": totp(self.secret)}, self._h(), timeout=15))
        if not d.get("status") or not (d.get("data") or {}).get("jwtToken"):
            raise BrokerError(f"Angel login failed: {d.get('message')} ({d.get('errorcode')}). " + self.hint)
        self.jwt = d["data"]["jwtToken"]; self.login_day = datetime.now(IST).date(); log("Angel One login OK")

    def _post(self, path, body, retry=True):
        with self.lock:                       # stay well inside Angel's rate limits
            w = 0.4 - (time.time() - self.last)
            if w > 0: time.sleep(w)
            self.last = time.time()
        if not self.jwt or getattr(self, "login_day", None) != datetime.now(IST).date(): self.login()
        try:
            d = json.loads(http(self.BASE + path, body, self._h(), timeout=10))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and retry: self.login(); return self._post(path, body, False)
            raise
        if not d.get("status"):
            if retry and str(d.get("errorcode", "")).startswith("AG80"): self.login(); return self._post(path, body, False)
            raise BrokerError(f"Angel: {d.get('message')} ({d.get('errorcode')})")
        return d.get("data") or {}

    def connect(self):
        self.login(); self._load_master()
        return f"{len(self.eq)}/{len(self.symbols)} F&O stocks mapped"

    def _load_master(self):
        f = CACHE / f"angel_master_{date.today():%Y%m%d}.json"
        if f.exists():
            m = json.loads(f.read_text())
        else:
            log("Downloading Angel One instrument master (once a day, ~40 MB)...")
            rows = json.loads(http(self.MASTER, headers=UA, timeout=120)); m = {"eq": {}, "opt": {}}
            for r in rows:
                n = r.get("name", "")
                if n not in self.symbols: continue
                if r.get("exch_seg") == "NSE" and r.get("symbol") == n + "-EQ": m["eq"][n] = r["token"]
                elif r.get("exch_seg") == "NFO" and r.get("instrumenttype") == "OPTSTK":
                    m["opt"].setdefault(n, []).append([r["expiry"], float(r["strike"]) / 100, r["symbol"][-2:], r["token"], int(float(r.get("lotsize") or 0))])
            f.write_text(json.dumps(m))
        self.eq, self.opt = m["eq"], m["opt"]; self.inv = {v: k for k, v in self.eq.items()}

    def _quote(self, exch, tokens):
        out = []
        for i in range(0, len(tokens), 50):
            out += self._post("/rest/secure/angelbroking/market/v1/quote/", {"mode": "FULL", "exchangeTokens": {exch: tokens[i:i+50]}}).get("fetched") or []
        return out

    def quotes(self):
        q = {}
        for v in self._quote("NSE", list(self.eq.values())):
            s = self.inv.get(str(v.get("symbolToken")))
            if s and v.get("ltp"):
                q[s] = {"ltp": v["ltp"], "open": v.get("open"), "high": v.get("high"), "low": v.get("low"), "prev_close": v.get("close"), "volume": v.get("tradeVolume", 0)}
        return q

    def chain(self, sym, want):
        rows = self.opt.get(sym) or []
        if not rows: raise BrokerError(f"No option contracts for {sym} in the Angel master")
        pd = lambda e: datetime.strptime(e.title(), "%d%b%Y").date()
        exps = sorted({pd(r[0]) for r in rows if pd(r[0]) >= datetime.now(IST).date()})
        try: w = datetime.strptime(want, "%Y-%m-%d").date()
        except Exception: w = exps[0]
        exp = next((e for e in exps if e >= w), exps[0])
        spot = (STATE["quotes"].get(sym) or {}).get("ltp") or 0
        sel = [r for r in rows if pd(r[0]) == exp]
        ks = sorted({r[1] for r in sel}, key=lambda k: abs(k - spot))[:21]
        sel = [r for r in sel if r[1] in ks]; by_tok = {r[3]: r for r in sel}; out = []
        for v in self._quote("NFO", [r[3] for r in sel]):
            r = by_tok.get(str(v.get("symbolToken")))
            if not r: continue
            dp = v.get("depth") or {}; bid = (dp.get("buy") or [{}])[0].get("price"); ask = (dp.get("sell") or [{}])[0].get("price")
            out.append({"k": r[1], "t": r[2], "ltp": v.get("ltp"), "oi": v.get("opnInterest"), "vol": v.get("tradeVolume"), "bid": bid, "ask": ask, "iv": None, "lot": r[4]})
        return {"expiry": exp.isoformat(), "expiries": [e.isoformat() for e in exps], "spot": spot, "rows": out}

class Dhan:
    name = "Dhan"; BASE = "https://api.dhan.co/v2"; SCRIP = "https://images.dhan.co/api-data/api-scrip-master.csv"
    hint = "token expired? generate a new one at web.dhan.co"
    def __init__(self, cfg, symbols):
        cid, tok = cfg.get("DHAN_CLIENT_ID", ""), cfg.get("DHAN_ACCESS_TOKEN", "")
        if not cid or not tok or "YOUR" in cid: raise BrokerError("Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN")
        self.h = {"access-token": tok, "client-id": cid, "Content-Type": "application/json", "Accept": "application/json"}
        self.symbols, self.last, self.lock = symbols, 0.0, threading.Lock()
    def connect(self):
        try: http(self.BASE + "/fundlimit", headers=self.h, timeout=8)
        except urllib.error.HTTPError as e: raise BrokerError(f"Dhan login failed (HTTP {e.code}). {self.hint}")
        f = CACHE / f"dhan_scrip_{date.today():%Y%m%d}.json"
        if f.exists(): self.ids = json.loads(f.read_text())
        else:
            log("Downloading Dhan scrip master..."); raw = http(self.SCRIP, headers=UA, timeout=90).decode("utf-8", "replace"); self.ids = {}
            for r in csv.DictReader(io.StringIO(raw)):
                if r.get("SEM_EXM_EXCH_ID") == "NSE" and r.get("SEM_SEGMENT") == "E" and r.get("SEM_SERIES", "EQ") == "EQ" and r.get("SEM_TRADING_SYMBOL") in self.symbols:
                    self.ids[r["SEM_TRADING_SYMBOL"]] = int(r["SEM_SMST_SECURITY_ID"])
            f.write_text(json.dumps(self.ids))
        self.inv = {str(v): k for k, v in self.ids.items()}
        return f"{len(self.ids)}/{len(self.symbols)} F&O stocks mapped"
    def quotes(self):
        d = json.loads(http(self.BASE + "/marketfeed/quote", {"NSE_EQ": list(self.ids.values())}, self.h)).get("data", {}).get("NSE_EQ", {}); q = {}
        for sid, v in d.items():
            s, o = self.inv.get(str(sid)), v.get("ohlc", {})
            if s and v.get("last_price"): q[s] = {"ltp": v["last_price"], "open": o.get("open"), "high": o.get("high"), "low": o.get("low"), "prev_close": o.get("close"), "volume": v.get("volume", 0)}
        return q
    def _chainpost(self, path, body):
        with self.lock:
            w = 3.1 - (time.time() - self.last)
            if w > 0: time.sleep(w)
            self.last = time.time()
        return json.loads(http(self.BASE + path, body, self.h)).get("data")
    def chain(self, sym, want):
        sid = self.ids.get(sym)
        if not sid: raise BrokerError(f"No Dhan security ID for {sym}")
        exps = self._chainpost("/optionchain/expirylist", {"UnderlyingScrip": sid, "UnderlyingSeg": "NSE_EQ"}) or []
        exp = want if want in exps else (exps[0] if exps else want)
        d = self._chainpost("/optionchain", {"UnderlyingScrip": sid, "UnderlyingSeg": "NSE_EQ", "Expiry": exp}) or {}; rows = []
        for k, v in (d.get("oc") or {}).items():
            for side in ("ce", "pe"):
                o = v.get(side) or {}
                if o: rows.append({"k": float(k), "t": side.upper(), "ltp": o.get("last_price"), "iv": o.get("implied_volatility"), "oi": o.get("oi"),
                                   "vol": o.get("volume"), "bid": o.get("top_bid_price"), "ask": o.get("top_ask_price")})
        return {"expiry": exp, "expiries": exps, "spot": d.get("last_price"), "rows": rows}

def make_broker(cfg, symbols):
    b = (cfg.get("BROKER") or ("angel" if cfg.get("ANGEL_API_KEY") else "dhan")).lower()
    return (AngelOne if b.startswith("angel") else Dhan)(cfg, symbols)

# ---------------------------------------------------------------- NSE EOD
def fetch_eod(as_of, symbols):
    rows, d = {}, datetime.strptime(as_of, "%Y-%m-%d").date() + timedelta(days=1)
    today = datetime.now(IST).date()
    while d <= today:
        if d.weekday() < 5 and not (d == today and datetime.now(IST).hour < 19):
            f = CACHE / f"eod_{d:%Y%m%d}.json"
            if f.exists():
                day = json.loads(f.read_text())
            else:
                url = f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
                try:
                    z = zipfile.ZipFile(io.BytesIO(http(url, headers=UA, timeout=20)))
                    txt = z.read([n for n in z.namelist() if n.endswith(".csv")][0]).decode()
                    day = {}
                    for r in csv.DictReader(io.StringIO(txt)):
                        s = r.get("TckrSymb", "").strip()
                        if s in symbols and r.get("SctySrs", "").strip() == "EQ":
                            day[s] = [d.isoformat(), float(r["OpnPric"]), float(r["HghPric"]), float(r["LwPric"]), float(r["ClsPric"]), int(float(r.get("TtlTradgVol") or 0))]
                    f.write_text(json.dumps(day)); log(f"Bhavcopy {d}: {len(day)} symbols")
                except urllib.error.HTTPError as e:
                    day = None; log(f"Bhavcopy {d}: not available (HTTP {e.code}, holiday or not published yet)")
                except Exception as e:
                    day = None; log(f"Bhavcopy {d}: failed ({e})")
            for s, r in (day or {}).items(): rows.setdefault(s, []).append(r)
        d += timedelta(days=1)
    return rows

# ---------------------------------------------------------------- state
STATE = {"quotes": {}, "updated": None, "status": "starting", "mode": "live", "eod": {}, "alerts": set(), "error": ""}

def quote_loop(broker, interval, demo, seed):
    while True:
        t0 = time.time()
        try:
            if demo:
                q = {}
                for s, (o, h, l, c) in seed.items():
                    p = STATE["quotes"].get(s, {}).get("ltp", o) * (1 + random.gauss(0, 0.0015))
                    old = STATE["quotes"].get(s, {"high": p, "low": p})
                    q[s] = {"ltp": round(p, 2), "open": o, "high": round(max(old["high"], p), 2), "low": round(min(old["low"], p), 2), "volume": 0}
                STATE["quotes"] = q
            else:
                STATE["quotes"] = broker.quotes()
            STATE["updated"] = datetime.now(IST).strftime("%H:%M:%S"); STATE["status"] = "ok"; STATE["error"] = ""
        except (urllib.error.HTTPError, BrokerError) as e:
            code = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(e)
            STATE["status"] = "error"; STATE["error"] = f"{broker.name if broker else 'feed'}: {code} ({broker.hint if broker else ''})"
            log(STATE["error"]); time.sleep(20)
        except Exception as e:
            STATE["status"] = "error"; STATE["error"] = str(e); log("Quote error:", e); time.sleep(5)
        n = datetime.now(IST); live_hours = n.weekday() < 5 and (9 * 60) <= n.hour * 60 + n.minute <= (15 * 60 + 35)
        if STATE["status"] == "ok" and STATE.get("on_tick"): 
            try: STATE["on_tick"]()
            except Exception as e: log("Server alert error:", e)
        time.sleep(max(0.5, interval - (time.time() - t0)) if (live_hours or demo) else 60)

def telegram(cfg, text):
    tok, chat = cfg.get("TELEGRAM_BOT_TOKEN", ""), cfg.get("TELEGRAM_CHAT_ID", "")
    if not tok or "YOUR" in tok or not chat: return False, "Telegram not configured"
    body = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if cfg.get("TELEGRAM_TOPIC_THREAD_ID"): body["message_thread_id"] = int(cfg["TELEGRAM_TOPIC_THREAD_ID"])
    try:
        http(f"https://api.telegram.org/bot{tok}/sendMessage", body, {"Content-Type": "application/json"}); return True, "sent"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------- server-side setups (same rules as the Best setups tab)
def ncdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def bs(S, K, T, r, v, ty):
    if T <= 0 or v <= 0: return max(0.0, S - K if ty == "CE" else K - S), (1.0 if S > K else 0.0) if ty == "CE" else (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r + v * v / 2) * T) / (v * math.sqrt(T)); d2 = d1 - v * math.sqrt(T)
    if ty == "CE": return S * ncdf(d1) - K * math.exp(-r * T) * ncdf(d2), ncdf(d1)
    return K * math.exp(-r * T) * ncdf(-d2) - S * ncdf(-d1), ncdf(d1) - 1
def step_for(s): return 1 if s < 100 else 2.5 if s < 250 else 5 if s < 500 else 10 if s < 1000 else 20 if s < 2500 else 50 if s < 5000 else 100 if s < 10000 else 250
def last_tuesday(y, m):
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    while d.weekday() != 1: d -= timedelta(days=1)
    return d
def expiry(min_dte=5):
    t = datetime.now(IST).date(); e = last_tuesday(t.year, t.month)
    if (e - t).days < min_dte: e = last_tuesday(t.year + (t.month == 12), t.month % 12 + 1)
    return e
def feats(c):
    i = len(c) - 1
    if i < 50: return None
    p3 = c[i-2:i+1]; h = max(b[2] for b in p3); l = min(b[3] for b in p3); r = h - l; vah, val = h - .15 * r, l + .15 * r
    m = c[i][0][:7]; mc = [b for b in c if b[0][:7] == m]; vv = sum(b[5] for b in mc)
    aw = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in mc) / vv if vv else c[i][4]
    atr = sum(max(c[k][2] - c[k][3], abs(c[k][2] - c[k-1][4]), abs(c[k][3] - c[k-1][4])) for k in range(i-13, i+1)) / 14
    cl = c[i][4]; s20 = sum(b[4] for b in c[i-19:i+1]) / 20; s50 = sum(b[4] for b in c[i-49:i+1]) / 50
    va = sum(b[5] for b in c[i-20:i]) / 20; rg = c[i][2] - c[i][3]
    rets = [math.log(c[k][4] / c[k-1][4]) for k in range(i-19, i+1)]; mu = sum(rets) / 20
    hv = math.sqrt(sum((x - mu) ** 2 for x in rets) / 20 * 252)
    return dict(vah=vah, val=val, aw=aw, atr=atr, cl=cl, s20=s20, s50=s50, vr=c[i][5] / va if va else 1,
                clv=(cl - c[i][3]) / rg if rg > 0 else .5, hv=hv, comp=r / atr, hi=c[i][2], lo=c[i][3])
def build_setups(candles, lots, cfg):
    min_score, top, tdelta = float(cfg.get("MIN_SCORE", 60)), int(cfg.get("TOP_PER_SIDE", 10)), float(cfg.get("TARGET_DELTA", .42))
    exp = expiry(); T = max(1, (exp - datetime.now(IST).date()).days + .6) / 365; out = []
    for s, c in candles.items():
        if s not in lots: continue
        f = feats(c)
        if not f or f["cl"] < 50: continue
        sd = "CE" if f["cl"] > f["aw"] and f["cl"] > f["vah"] * .99 else "PE" if f["cl"] < f["aw"] and f["cl"] < f["val"] * 1.01 else None
        if not sd: continue
        g = 1 if sd == "CE" else -1
        if not ((f["cl"] > f["s20"] > f["s50"]) if sd == "CE" else (f["cl"] < f["s20"] < f["s50"])): continue
        sc = 20 * (g * (f["cl"] - f["s20"]) > 0) + 10 * (g * (f["s20"] - f["s50"]) > 0) + min(20, max(0, (f["vr"] - 1) * 20)) \
             + 20 * (f["clv"] if sd == "CE" else 1 - f["clv"]) + 15 * max(0, min(1, (2.2 - f["comp"]) / 1.2)) + 15 * (g * (f["cl"] - f["aw"]) / f["aw"] * 100 > .5)
        if sc < min_score: continue
        w = f["vah"] - f["val"]; cl = lambda x, a, b: max(a, min(b, x))
        if sd == "CE":
            trig = max(f["vah"] * 1.005, f["hi"] * 1.001); sl = trig - cl(trig - f["vah"], .4 * f["atr"], .8 * f["atr"]); R = trig - sl
            t1 = trig + 1.5 * R; t2 = max(t1 + .5 * R, min(trig + 3 * R, f["vah"] + 1.618 * w))
        else:
            trig = min(f["val"] * .995, f["lo"] * .999); sl = trig + cl(f["val"] - trig, .4 * f["atr"], .8 * f["atr"]); R = sl - trig
            t1 = trig - 1.5 * R; t2 = min(t1 - .5 * R, max(trig - 3 * R, f["val"] - 1.618 * w))
        iv = cl(f["hv"] * 1.2, .15, .8); st = step_for(f["cl"]); atm = round(trig / st) * st
        k = min((round(atm + i * st, 2) for i in range(-8, 9) if atm + i * st > 0), key=lambda k: abs(abs(bs(trig, k, T, .065, iv, sd)[1]) - tdelta))
        pr = lambda x: bs(x, k, T - .25 / 365, .065, iv, sd)[0]
        out.append(dict(s=s, sd=sd, sc=sc, k=k, trig=trig, sl=sl, t1=t1, t2=t2, e=bs(trig, k, T, .065, iv, sd)[0], psl=pr(sl), pt1=pr(t1), pt2=pr(t2), lot=lots[s], exp=exp))
    out.sort(key=lambda x: -x["sc"])
    return [x for x in out if x["sd"] == "CE"][:top] + [x for x in out if x["sd"] == "PE"][:top]

def make_alerter(cfg, candles, lots):
    cache = {"day": None, "setups": []}
    def on_tick():
        day = datetime.now(IST).date()
        if cache["day"] != day: cache["day"] = day; cache["setups"] = build_setups(candles, lots, cfg); log(f"Server alerts watching {len(cache['setups'])} setups")
        n = datetime.now(IST)
        if n.weekday() > 4 or not (9 * 60 + 25 <= n.hour * 60 + n.minute <= 15 * 60 + 15): return
        for x in cache["setups"]:
            q = STATE["quotes"].get(x["s"]); key = f'{day}|{x["s"]}|{x["sd"]}'
            if not q or key in STATE["alerts"]: continue
            hi, lo, ltp = q.get("high") or q["ltp"], q.get("low") or q["ltp"], q["ltp"]; ce = x["sd"] == "CE"
            if (lo <= x["sl"]) if ce else (hi >= x["sl"]): continue          # stop already crossed, skip
            if (ltp >= x["t1"]) if ce else (ltp <= x["t1"]): continue        # already past T1, don't chase
            if not ((hi >= x["trig"]) if ce else (lo <= x["trig"])): continue
            f2 = lambda v: f"{v:,.2f}"
            text = (f"LIQUIDITY VACUUM · {'BULLISH' if ce else 'BEARISH'} TRIGGER\n{x['s']} {x['k']:g} {x['sd']} (lot {x['lot']}, exp {x['exp']:%d %b})\n"
                    f"Spot {f2(ltp)} {'crossed above' if ce else 'broke below'} {f2(x['trig'])}\nBuy near {f2(x['e'])}-{f2(x['e']*1.05)} (est., check live premium)\n"
                    f"SL {f2(x['psl'])} · spot {f2(x['sl'])}\nT1 {f2(x['pt1'])} · spot {f2(x['t1'])} (book half, SL to cost)\nT2 {f2(x['pt2'])} · spot {f2(x['t2'])}\n"
                    f"Score {x['sc']:.0f} · not investment advice")
            STATE["alerts"].add(key); ok, why = telegram(cfg, text); log(f"Trigger {x['s']} {x['sd']}: telegram {why}")
    return on_tick

# ---------------------------------------------------------------- web
def make_handler(cfg, broker):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def send(self, code, obj, ctype="application/json"):
            b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def authed(self):
            pw = cfg.get("APP_PASSWORD", "")
            if not pw or urlparse(self.path).path == "/ping": return True
            h = self.headers.get("Authorization", "")
            try: ok = h.startswith("Basic ") and hmac.compare_digest(base64.b64decode(h[6:]).decode().split(":", 1)[1], pw)
            except Exception: ok = False
            if not ok:
                self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="Liquidity Vacuum Terminal"'); self.send_header("Content-Length", "0"); self.end_headers()
            return ok
        def do_GET(self):
            if not self.authed(): return
            u = urlparse(self.path); q = parse_qs(u.query)
            if u.path == "/ping": return self.send(200, {"ok": True})
            if u.path in ("/", "/index.html"):
                return self.send(200, HTML.read_bytes().replace(b"<head>", b'<head><meta name="lv-bridge" content="1">', 1), "text/html; charset=utf-8")
            if u.path == "/api/health": return self.send(200, {"ok": True, "mode": STATE["mode"], "status": STATE["status"], "error": STATE["error"], "telegram": bool(cfg.get("TELEGRAM_CHAT_ID")) and "YOUR" not in cfg.get("TELEGRAM_BOT_TOKEN", "YOUR")})
            if u.path == "/api/quotes": return self.send(200, {"updated": STATE["updated"], "status": STATE["status"], "error": STATE["error"], "mode": STATE["mode"], "broker": STATE.get("broker", ""), "quotes": STATE["quotes"]})
            if u.path == "/api/eod": return self.send(200, STATE["eod"])
            if u.path == "/api/chain":
                if not broker: return self.send(400, {"error": "Option chain needs a broker login (not available in demo mode)."})
                try:
                    d = broker.chain(q.get("sym", [""])[0].upper(), q.get("expiry", [""])[0]); d["sym"] = q.get("sym", [""])[0].upper()
                    return self.send(200, d)
                except urllib.error.HTTPError as e:
                    return self.send(502, {"error": f"{broker.name} HTTP {e.code}: {e.read()[:200].decode('utf-8','replace')}"})
                except Exception as e:
                    return self.send(502, {"error": str(e)})
            self.send(404, {"error": "not found"})
        def do_POST(self):
            if not self.authed(): return
            if urlparse(self.path).path != "/api/alert": return self.send(404, {"error": "not found"})
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception:
                return self.send(400, {"error": "bad json"})
            key = f'{datetime.now(IST).date()}|{d.get("key","")}'  # browser sends "SYM|SIDE"
            if key in STATE["alerts"]: return self.send(200, {"sent": False, "reason": "already sent today"})
            ok, why = telegram(cfg, d.get("text", ""))
            if ok: STATE["alerts"].add(key); log("Telegram alert:", d.get("key"))
            self.send(200, {"sent": ok, "reason": why})
    return H

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--demo", action="store_true", help="random quotes, no broker"); ap.add_argument("--no-eod", action="store_true")
    ap.add_argument("--host", default=None); a = ap.parse_args(); cfg = load_cfg()
    if cfg.get("DEMO") == "1": a.demo = True
    if not HTML.exists(): sys.exit(f"Put liquidity_vacuum_terminal.html next to this script ({HTML})")
    data = embedded_data(); symbols = set(data["lots"]) - set(data["indices"])
    STATE["mode"] = "demo" if a.demo else "live"
    if not a.no_eod:
        log(f"Checking NSE bhavcopy after {data['asOf']}..."); STATE["eod"] = fetch_eod(data["asOf"], symbols)
    broker, seed = None, {}
    if a.demo:
        for s in symbols:
            c = data["candles"][s]; last = STATE["eod"].get(s, [c[-1]])[-1]; o = last[4] * (1 + random.gauss(0, .006)); seed[s] = (round(o, 2), o, o, o)
        log("DEMO mode: quotes are random, for testing only.")
    else:
        try:
            broker = make_broker(cfg, symbols); info = broker.connect()
        except BrokerError as e: sys.exit(str(e))
        except urllib.error.HTTPError as e: sys.exit(f"Broker login failed: HTTP {e.code} {e.read()[:300]!r}")
        STATE["broker"] = broker.name; log(f"{broker.name} connected. {info}")
    candles = {s: list(data["candles"][s]) for s in symbols}
    def merge_eod():
        for s, rows in STATE["eod"].items():
            have = {r[0] for r in candles.get(s, [])}
            candles.setdefault(s, []).extend(r for r in rows if r[0] not in have); candles[s].sort(key=lambda r: r[0])
    merge_eod()
    def eod_refresher():   # long-running servers: pick up tonight's bhavcopy without a restart
        while True:
            time.sleep(3600)
            if not a.no_eod and datetime.now(IST).hour >= 19:
                STATE["eod"] = fetch_eod(data["asOf"], symbols); merge_eod()
    threading.Thread(target=eod_refresher, daemon=True).start()
    if cfg.get("SERVER_ALERTS", "0") == "1":
        STATE["on_tick"] = make_alerter(cfg, candles, {s: data["lots"][s] for s in symbols}); log("Server-side Telegram alerts ON (work even with the browser closed)")
    threading.Thread(target=quote_loop, args=(broker, float(cfg["QUOTE_INTERVAL_SECONDS"]), a.demo, seed), daemon=True).start()
    port = int(cfg["PORT"]); host = a.host or ("0.0.0.0" if os.environ.get("RENDER") or os.environ.get("PORT") else "127.0.0.1")
    if host != "127.0.0.1" and not cfg.get("APP_PASSWORD"): log("WARNING: running on a public address without APP_PASSWORD. Anyone with the URL can use it.")
    srv = ThreadingHTTPServer((host, port), make_handler(cfg, broker))
    log(f"Terminal running at  http://localhost:{port}   (Ctrl+C to stop)")
    try: srv.serve_forever()
    except KeyboardInterrupt: log("Stopped.")

if __name__ == "__main__": main()
