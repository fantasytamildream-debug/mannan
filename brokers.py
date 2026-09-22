"""Broker adapters. Each one gives the engine the same shapes:

  connect()               -> short info string (raises BrokerError on failure)
  quotes()                -> {SYM: {ltp, open, high, low, prev_close, volume, ts}}, plus "NIFTY"
  contracts(sym)          -> [{expiry: date, strike, type, token, lot}]  (real exchange contracts)
  option_quotes(tokens)   -> {token: {ltp, bid, ask, volume, oi, ts}}
"""
import base64, csv, hmac, io, json, math, random, struct, threading, time, urllib.error, urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import strategy as S

IST = timezone(timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36", "Accept": "*/*"}

class BrokerError(Exception): pass

def log(*a): print(datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)

def http(url, body=None, headers=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r: return r.read()

def parse_ts(v):
    """Exchange timestamps come in a few formats; return aware datetime or None."""
    if not v: return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / (1000 if v > 1e11 else 1), IST)
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try: return datetime.strptime(str(v)[:19], fmt).replace(tzinfo=IST)
        except ValueError: pass
    return None

def totp(secret):
    sec = secret.strip().replace(" ", "").upper(); key = base64.b32decode(sec + "=" * (-len(sec) % 8))
    h = hmac.new(key, struct.pack(">Q", int(time.time()) // 30), "sha1").digest(); o = h[-1] & 15
    return f"{(struct.unpack('>I', h[o:o+4])[0] & 0x7fffffff) % 1000000:06d}"

class RateGate:
    def __init__(self, gap): self.gap, self.last, self.lock = gap, 0.0, threading.Lock()
    def wait(self):
        with self.lock:
            w = self.gap - (time.time() - self.last)
            if w > 0: time.sleep(w)
            self.last = time.time()

# ================================================================== Angel One SmartAPI
class AngelOne:
    name = "Angel One"; BASE = "https://apiconnect.angelone.in"
    MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    NIFTY_TOKEN = "99926000"
    hint = "check ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_MPIN, ANGEL_TOTP_SECRET"

    def __init__(self, cfg, symbols, cache):
        self.key, self.client = cfg.get("ANGEL_API_KEY", ""), cfg.get("ANGEL_CLIENT_CODE", "")
        self.mpin, self.secret = cfg.get("ANGEL_MPIN", ""), cfg.get("ANGEL_TOTP_SECRET", "")
        if not all([self.key, self.client, self.mpin, self.secret]) or "YOUR" in self.key:
            raise BrokerError("Add ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_MPIN, ANGEL_TOTP_SECRET")
        self.symbols, self.cache, self.jwt, self.gate, self.login_day = symbols, cache, None, RateGate(0.35), None

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
            raise BrokerError(f"Angel login failed: {d.get('message')} ({d.get('errorcode')}). {self.hint}")
        self.jwt = d["data"]["jwtToken"]; self.login_day = datetime.now(IST).date(); log("Angel One login OK")

    def _post(self, path, body, retry=True):
        self.gate.wait()
        if not self.jwt or self.login_day != datetime.now(IST).date(): self.login()
        try: d = json.loads(http(self.BASE + path, body, self._h(), timeout=10))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and retry: self.login(); return self._post(path, body, False)
            raise
        if not d.get("status"):
            if retry and str(d.get("errorcode", "")).startswith("AG80"): self.login(); return self._post(path, body, False)
            raise BrokerError(f"Angel: {d.get('message')} ({d.get('errorcode')})")
        return d.get("data") or {}

    def connect(self):
        self.login()
        f = self.cache / f"angel_master_{date.today():%Y%m%d}.json"
        if f.exists(): m = json.loads(f.read_text())
        else:
            log("Downloading Angel One instrument master (once a day, ~40 MB)...")
            rows = json.loads(http(self.MASTER, headers=UA, timeout=180)); m = {"eq": {}, "opt": {}}
            for r in rows:
                n = r.get("name", "")
                if n not in self.symbols: continue
                if r.get("exch_seg") == "NSE" and r.get("symbol") == n + "-EQ": m["eq"][n] = r["token"]
                elif r.get("exch_seg") == "NFO" and r.get("instrumenttype") == "OPTSTK":
                    m["opt"].setdefault(n, []).append([r["expiry"], float(r["strike"]) / 100, r["symbol"][-2:], r["token"], int(float(r.get("lotsize") or 0))])
            f.write_text(json.dumps(m))
        self.eq = m["eq"]; self.inv = {v: k for k, v in self.eq.items()}; self.inv[self.NIFTY_TOKEN] = "NIFTY"
        pd = lambda e: datetime.strptime(e.title(), "%d%b%Y").date()
        self.opt = {s: [dict(expiry=pd(r[0]), strike=r[1], type=r[2], token=str(r[3]), lot=r[4]) for r in rows] for s, rows in m["opt"].items()}
        return f"{len(self.eq)}/{len(self.symbols)} stocks, {sum(len(v) for v in self.opt.values())} option contracts"

    def _quote(self, exch, tokens):
        out = []
        for i in range(0, len(tokens), 50):
            out += self._post("/rest/secure/angelbroking/market/v1/quote/", {"mode": "FULL", "exchangeTokens": {exch: tokens[i:i+50]}}).get("fetched") or []
        return out

    def quotes(self):
        q = {}
        for v in self._quote("NSE", list(self.eq.values()) + [self.NIFTY_TOKEN]):
            s = self.inv.get(str(v.get("symbolToken")))
            if s and v.get("ltp"):
                q[s] = dict(ltp=float(v["ltp"]), open=v.get("open"), high=v.get("high"), low=v.get("low"), prev_close=v.get("close"),
                            volume=v.get("tradeVolume", 0), ts=parse_ts(v.get("exchFeedTime") or v.get("exchTradeTime")))
        return q

    def contracts(self, sym): return self.opt.get(sym, [])

    def lot_of(self, sym):
        o = self.opt.get(sym) or []
        return o[0]["lot"] if o and o[0].get("lot") else None

    def history(self, exch, token, day, interval="FIVE_MINUTE"):
        """Intraday candles for one past day: [[datetime, o, h, l, c, v], ...]"""
        d = self._post("/rest/secure/angelbroking/historical/v1/getCandleData",
                       {"exchange": exch, "symboltoken": str(token), "interval": interval, "fromdate": f"{day} 09:15", "todate": f"{day} 15:30"})
        out = []
        for r in (d if isinstance(d, list) else []):
            try: out.append([datetime.fromisoformat(r[0]).astimezone(IST), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])])
            except Exception: pass
        return out

    def option_quotes(self, tokens):
        out = {}
        for v in self._quote("NFO", list(tokens)):
            dp = v.get("depth") or {}; b = (dp.get("buy") or [{}])[0].get("price"); a = (dp.get("sell") or [{}])[0].get("price")
            out[str(v.get("symbolToken"))] = dict(ltp=float(v.get("ltp") or 0), bid=b, ask=a, volume=v.get("tradeVolume", 0), oi=v.get("opnInterest"),
                                                  ts=parse_ts(v.get("exchFeedTime") or v.get("exchTradeTime")))
        return out

# ================================================================== Dhan
class Dhan:
    name = "Dhan"; BASE = "https://api.dhan.co/v2"; SCRIP = "https://images.dhan.co/api-data/api-scrip-master.csv"
    hint = "token expired? generate a new one at web.dhan.co"
    def __init__(self, cfg, symbols, cache):
        cid, tok = cfg.get("DHAN_CLIENT_ID", ""), cfg.get("DHAN_ACCESS_TOKEN", "")
        if not cid or not tok or "YOUR" in cid: raise BrokerError("Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN")
        self.h = {"access-token": tok, "client-id": cid, "Content-Type": "application/json", "Accept": "application/json"}
        self.symbols, self.cache, self.gate, self.chain_gate, self.opt = symbols, cache, RateGate(1.05), RateGate(3.1), {}
    def connect(self):
        try: http(self.BASE + "/fundlimit", headers=self.h, timeout=8)
        except urllib.error.HTTPError as e: raise BrokerError(f"Dhan login failed (HTTP {e.code}). {self.hint}")
        f = self.cache / f"dhan_scrip_{date.today():%Y%m%d}.json"
        if f.exists(): self.ids = json.loads(f.read_text())
        else:
            log("Downloading Dhan scrip master..."); raw = http(self.SCRIP, headers=UA, timeout=120).decode("utf-8", "replace"); self.ids = {}
            for r in csv.DictReader(io.StringIO(raw)):
                if r.get("SEM_EXM_EXCH_ID") == "NSE" and r.get("SEM_SEGMENT") == "E" and r.get("SEM_SERIES", "EQ") == "EQ" and r.get("SEM_TRADING_SYMBOL") in self.symbols:
                    self.ids[r["SEM_TRADING_SYMBOL"]] = int(r["SEM_SMST_SECURITY_ID"])
            f.write_text(json.dumps(self.ids))
        self.inv = {str(v): k for k, v in self.ids.items()}
        return f"{len(self.ids)}/{len(self.symbols)} stocks mapped"
    def _q(self, body):
        self.gate.wait(); return json.loads(http(self.BASE + "/marketfeed/quote", body, self.h)).get("data", {})
    def quotes(self):
        d = self._q({"NSE_EQ": list(self.ids.values()), "IDX_I": [13]}); q = {}
        for seg in ("NSE_EQ", "IDX_I"):
            for sid, v in (d.get(seg) or {}).items():
                s = "NIFTY" if seg == "IDX_I" else self.inv.get(str(sid)); o = v.get("ohlc", {})
                if s and v.get("last_price"):
                    q[s] = dict(ltp=float(v["last_price"]), open=o.get("open"), high=o.get("high"), low=o.get("low"), prev_close=o.get("close"),
                                volume=v.get("volume", 0), ts=parse_ts(v.get("last_trade_time")))
        return q
    def contracts(self, sym):
        if sym in self.opt: return self.opt[sym]
        sid = self.ids.get(sym); out = []
        if not sid: return out
        self.chain_gate.wait()
        exps = json.loads(http(self.BASE + "/optionchain/expirylist", {"UnderlyingScrip": sid, "UnderlyingSeg": "NSE_EQ"}, self.h)).get("data") or []
        for e in exps[:2]:
            self.chain_gate.wait()
            d = json.loads(http(self.BASE + "/optionchain", {"UnderlyingScrip": sid, "UnderlyingSeg": "NSE_EQ", "Expiry": e}, self.h)).get("data") or {}
            for k, v in (d.get("oc") or {}).items():
                for side in ("ce", "pe"):
                    o = v.get(side) or {}
                    if o.get("security_id"): out.append(dict(expiry=datetime.strptime(e, "%Y-%m-%d").date(), strike=float(k), type=side.upper(), token=str(o["security_id"]), lot=0))
        self.opt[sym] = out; return out
    def option_quotes(self, tokens):
        d = self._q({"NSE_FNO": [int(t) for t in tokens]}).get("NSE_FNO") or {}; out = {}
        for sid, v in d.items():
            dp = v.get("depth") or {}; b = (dp.get("buy") or [{}])[0].get("price"); a = (dp.get("sell") or [{}])[0].get("price")
            out[str(sid)] = dict(ltp=float(v.get("last_price") or 0), bid=b, ask=a, volume=v.get("volume", 0), oi=v.get("oi"), ts=parse_ts(v.get("last_trade_time")))
        return out

# ================================================================== Demo (simulated market, for testing the whole flow)
class Demo:
    name = "Demo"; hint = "demo"
    def __init__(self, cfg, symbols, cache, candles=None, watch_bias=None):
        self.symbols, self.candles, self.state, self.bias = symbols, candles or {}, {}, watch_bias or {}
        self.vol = float(cfg.get("DEMO_VOL", "0.0006"))
    def connect(self):
        for s in list(self.symbols) + ["NIFTY"]:
            c = self.candles.get(s); last = c[-1][4] if c else 25000.0
            o = last * (1 + random.gauss(0, .0015)); f = S.daily_features(c) if c else None
            self.state[s] = dict(ltp=o, open=o, high=o, low=o, prev_close=last, volume=int((f["vavg"] if f else 1e6) * random.uniform(.4, .8)), iv=max(.18, (f["hv"] if f else .2) * 1.2), vavg=(f["vavg"] if f else 1e6))
        return f"simulated {len(self.symbols)} stocks"
    def set_bias(self, bias): self.bias = bias      # {sym: +1/-1} nudges watchlist names toward their trigger
    def quotes(self):
        now = datetime.now(IST); out = {}
        for s, st in self.state.items():
            drift = self.bias.get(s, 0) * self.vol * 0.35
            st["ltp"] *= 1 + random.gauss(drift, self.vol); st["high"] = max(st["high"], st["ltp"]); st["low"] = min(st["low"], st["ltp"])
            st["volume"] += int(st["vavg"] / 4500 * random.uniform(.5, 2.2))
            out[s] = dict(ltp=round(st["ltp"], 2), open=round(st["open"], 2), high=round(st["high"], 2), low=round(st["low"], 2),
                          prev_close=st["prev_close"], volume=st["volume"], ts=now)
        return out
    def contracts(self, sym):
        st = self.state.get(sym)
        if not st: return []
        t = datetime.now(IST).date(); exps = []
        for k in range(3):
            y, m = t.year + (t.month + k - 1) // 12, (t.month + k - 1) % 12 + 1
            d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
            while d.weekday() != 1: d -= timedelta(days=1)
            if d >= t: exps.append(d)
        stp = S.step_for(st["prev_close"]); atm = round(st["prev_close"] / stp) * stp; lot = 1000
        return [dict(expiry=e, strike=round(atm + i * stp, 2), type=ty, token=f"{sym}|{e}|{round(atm + i * stp, 2)}|{ty}", lot=lot)
                for e in exps[:2] for i in range(-10, 11) for ty in ("CE", "PE") if atm + i * stp > 0]
    def option_quotes(self, tokens):
        now = datetime.now(IST); out = {}
        for tok in tokens:
            sym, e, k, ty = tok.split("|"); st = self.state[sym]; e = date.fromisoformat(e); k = float(k)
            p = S.bs(st["ltp"], k, S.years_to(e, now), .065, st["iv"], ty)[0]; p = max(.05, round(p * 20) / 20)
            spr = max(.05, round(p * .008 * 20) / 20)
            out[tok] = dict(ltp=p, bid=round(p - spr / 2, 2), ask=round(p + spr / 2, 2), volume=random.randint(5, 80) * 1000, oi=random.randint(50, 900) * 1000, ts=now)
        return out

def make_broker(cfg, symbols, cache, candles=None):
    if cfg.get("DEMO") == "1": return Demo(cfg, symbols, cache, candles)
    b = (cfg.get("BROKER") or ("angel" if cfg.get("ANGEL_API_KEY") else "dhan")).lower()
    return (AngelOne if b.startswith("angel") else Dhan)(cfg, symbols, cache)
