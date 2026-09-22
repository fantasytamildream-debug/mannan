#!/usr/bin/env python3
"""KRT Options Terminal - server.  Standard library only (Python 3.9+).

  python server.py            live (broker keys in config.env or environment)
  python server.py --demo     simulated market to try every screen
Open http://localhost:8765
"""
import argparse, base64, csv, hmac, io, json, os, sys, threading, time, urllib.error, urllib.request, zipfile
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import strategy as S, brokers as B
from engine import Engine

ROOT = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))
log = B.log

KEYS = ("BROKER", "ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_MPIN", "ANGEL_TOTP_SECRET", "DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_TOPIC_THREAD_ID", "APP_PASSWORD", "PORT", "DATA_DIR", "DEMO", "DEMO_VOL",
        "QUOTE_INTERVAL_SECONDS", "RISK_PER_TRADE", "MAX_LOTS", "MIN_SCORE", "TOP_PER_SIDE", "TARGET_DELTA", "MAX_SPREAD_PCT",
        "MIN_OPTION_VOLUME_LOTS", "VOLUME_PACE", "MIN_DTE", "INDEX_FILTER", "MAX_ACTIVE", "BAR_SECONDS", "IGNORE_MARKET_HOURS",
        "ENTRY_START", "NO_NEW_ENTRY", "SQUARE_OFF")

def load_cfg():
    cfg = {"PORT": "8765", "QUOTE_INTERVAL_SECONDS": "5"}
    f = ROOT / "config.env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); v = v.split(" #")[0].strip().strip('"').strip("'")
                if v: cfg[k.strip()] = v
    for k in KEYS:
        if os.environ.get(k): cfg[k] = os.environ[k]
    return cfg

# ------------------------------------------------------------------ NSE end-of-day files (keeps levels current)
def fetch_eod(as_of, symbols, cache):
    rows, d = {}, datetime.strptime(as_of, "%Y-%m-%d").date() + timedelta(days=1); now = datetime.now(IST)
    while d <= now.date():
        if d.weekday() < 5 and not (d == now.date() and now.hour < 19):
            f = cache / f"eod_{d:%Y%m%d}.json"; day = None
            if f.exists(): day = json.loads(f.read_text())
            else:
                url = f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
                try:
                    z = zipfile.ZipFile(io.BytesIO(B.http(url, headers=dict(B.UA, Referer="https://www.nseindia.com/"), timeout=20)))
                    txt = z.read([n for n in z.namelist() if n.endswith(".csv")][0]).decode(); day = {}
                    for r in csv.DictReader(io.StringIO(txt)):
                        s = r.get("TckrSymb", "").strip()
                        if s in symbols and r.get("SctySrs", "").strip() == "EQ":
                            day[s] = [d.isoformat(), float(r["OpnPric"]), float(r["HghPric"]), float(r["LwPric"]), float(r["ClsPric"]), int(float(r.get("TtlTradgVol") or 0))]
                    f.write_text(json.dumps(day)); log(f"NSE bhavcopy {d}: {len(day)} stocks")
                except Exception as e: log(f"NSE bhavcopy {d}: not loaded ({getattr(e, 'code', e)})")
            for s, r in (day or {}).items(): rows.setdefault(s, []).append(r)
        d += timedelta(days=1)
    return rows

# ------------------------------------------------------------------ Telegram
class Telegram:
    def __init__(self, cfg):
        self.tok, self.chat, self.thread = cfg.get("TELEGRAM_BOT_TOKEN", ""), cfg.get("TELEGRAM_CHAT_ID", ""), cfg.get("TELEGRAM_TOPIC_THREAD_ID", "")
        self.on = bool(self.tok and self.chat and "YOUR" not in self.tok); self.sent = set()
    def send(self, key, text):
        if key in self.sent: return
        self.sent.add(key)
        if not self.on: log("[alert]", text.splitlines()[0]); return
        body = {"chat_id": self.chat, "text": text, "disable_web_page_preview": True}
        if self.thread: body["message_thread_id"] = int(self.thread)
        def go():
            try: B.http(f"https://api.telegram.org/bot{self.tok}/sendMessage", body, {"Content-Type": "application/json"})
            except Exception as e: log("Telegram failed:", e)
        threading.Thread(target=go, daemon=True).start()

def call_text(c):
    e, L, k = c["entry"], c["levels"], c["contract"]; f = lambda v: f"{v:,.2f}"; dirn = "BUY CALL" if c["side"] == "CE" else "BUY PUT"
    rng = f"{f(e['prem'])} – {f(e['chase'])}"
    return (f"{'🔥 ' if c['strong'] else ''}KRT CALL · {dirn}\n{k['name']}  (lot {k['lot']} × {c['lots']})\n"
            f"Entry ₹{rng}  at {e['t']} IST  (live ask, don't buy above {f(e['chase'])})\n"
            f"SL spot {f(L['sl'])}  ≈ ₹{f(L['est']['sl'])}\n"
            + "".join(f"T{i+1} spot {f(t)}  ≈ ₹{f(p)}\n" for i, (t, p) in enumerate(zip(L["t"], L["est"]["t"])))
            + f"Exit rule: {'T1 → SL to cost, T2 → SL to T1, T3 exit' if c['lots'] == 1 else 'part-book at targets, rest trails'} · square-off 15:15\n"
            f"Risk ≈ ₹{e['risk_lot'] * c['lots']:,.0f} incl. costs · score {c['score']:.0f}/100 (quality, not win chance)\nNot investment advice.")

# ------------------------------------------------------------------ HTTP
def make_handler(cfg, eng, statics):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def send(self, code, obj, ctype="application/json"):
            b = obj if isinstance(obj, bytes) else json.dumps(obj, default=str).encode()
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def authed(self):
            pw = cfg.get("APP_PASSWORD", "")
            if not pw or urlparse(self.path).path == "/ping": return True
            h = self.headers.get("Authorization", "")
            try: ok = h.startswith("Basic ") and hmac.compare_digest(base64.b64decode(h[6:]).decode().split(":", 1)[1], pw)
            except Exception: ok = False
            if not ok:
                self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="KRT Options Terminal"'); self.send_header("Content-Length", "0"); self.end_headers()
            return ok
        def do_GET(self):
            if not self.authed(): return
            u = urlparse(self.path); now = datetime.now(IST)
            if u.path == "/ping": return self.send(200, {"ok": True})
            if u.path in ("/", "/index.html"): return self.send(200, (ROOT / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/state": return self.send(200, eng.snapshot(now))
            if u.path == "/api/history": return self.send(200, eng.history())
            if u.path == "/api/levels": return self.send(200, statics["levels"])
            if u.path == "/api/backtest": return self.send(200, statics["backtest"])
            if u.path == "/api/days":
                return self.send(200, {"live": eng.days(), "backtest": statics["bt_days"], "today": now.date().isoformat()})
            if u.path == "/api/day":
                d = (parse_qs(u.query).get("d") or [""])[0][:10]
                calls = eng.day_calls(d)
                if calls is not None: return self.send(200, {"date": d, "source": "live", "calls": calls})
                rows = statics["bt_by_day"].get(d)
                if rows: return self.send(200, {"date": d, "source": "backtest", "trades": rows})
                return self.send(200, {"date": d, "source": "none"})
            self.send(404, {"error": "not found"})
    return H

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--demo", action="store_true"); ap.add_argument("--no-eod", action="store_true"); ap.add_argument("--host")
    a = ap.parse_args(); cfg = load_cfg()
    if a.demo: cfg["DEMO"] = "1"
    data = json.loads((ROOT / "data" / "market_data.json").read_text()); symbols = set(data["lots"]) - set(data["indices"])
    cache = Path(cfg.get("DATA_DIR") or (ROOT / "cache")); cache.mkdir(parents=True, exist_ok=True)
    candles = {s: list(data["candles"][s]) for s in symbols}
    def merge(rows):
        for s, rs in rows.items():
            have = {r[0] for r in candles.get(s, [])}; candles.setdefault(s, []).extend(r for r in rs if r[0] not in have); candles[s].sort(key=lambda r: r[0])
    if not a.no_eod: log(f"Checking NSE bhavcopy after {data['asOf']}..."); merge(fetch_eod(data["asOf"], symbols, cache))
    try:
        broker = B.make_broker(cfg, symbols, cache, candles); log(f"{broker.name} connected. {broker.connect()}")
    except B.BrokerError as e: sys.exit(str(e))
    except urllib.error.HTTPError as e: sys.exit(f"Broker login failed: HTTP {e.code} {e.read()[:300]!r}")
    tg = Telegram(cfg)
    def notify(c, kind, e):
        if kind == "entry": tg.send(f"{c['id']}|entry", call_text(c))
        else:
            p = c.get("pnl") or {}; tg.send(f"{c['id']}|{kind}|{e['t']}" if kind == "exit" else f"{c['id']}|{kind}",
                    f"{'✅' if kind.startswith('t') else '⛔' if kind in ('sl',) else '🔚'} {c['contract']['name']}\n{e['text']} at {e['t']} IST\nNet so far ≈ ₹{p.get('net', 0):,.0f}")
    eng = Engine(cfg, broker, candles, {s: data["lots"][s] for s in symbols}, cache, notify)
    last_lots = {}
    for s in symbols:
        try: last_lots[s] = data["lots"][s]
        except KeyError: pass
    T = data["trades"]; nw = sum(t[9] for t in T if t[9] > 0); nl = -sum(t[9] for t in T if t[9] < 0)
    bt_by_day = {}
    for t in T: bt_by_day.setdefault(t[0], []).append(t)
    statics_extra = {"bt_by_day": bt_by_day, "bt_days": sorted(bt_by_day, reverse=True)}
    statics = {"levels": S.level_table(candles, last_lots),
               "backtest": dict(trades=len(T), win=sum(t[12] for t in T) / len(T) * 100, pf_net=nw / nl, net=sum(t[9] for t in T),
                                note="Package backtest, NOT verified: synthetic option prices, same-day close used for entry, targets checked before stops.",
                                rows=T)}
    statics.update(statics_extra)
    interval = float(cfg["QUOTE_INTERVAL_SECONDS"])
    def loop():
        while True:
            t0 = time.time(); now = datetime.now(IST)
            try:
                if eng.market_open(now) or now.hour * 60 + now.minute in range(9 * 60, 15 * 60 + 40) or eng.day is None:
                    eng.tick(now, broker.quotes())
                else:
                    with eng.lock:
                        if eng.day != now.date(): eng.new_day(now)
                        eng.feed.update(status="ok", last_poll=now.isoformat())
            except Exception as e:
                eng.feed.update(status="error", error=f"{broker.name}: {getattr(e, 'code', '') or e} ({broker.hint})"); log("Feed error:", e); time.sleep(10)
            busy = eng.market_open(now)
            time.sleep(max(0.5, interval - (time.time() - t0)) if busy else 30)
    threading.Thread(target=loop, daemon=True).start()
    def eod_refresh():
        while True:
            time.sleep(3600)
            if not a.no_eod and datetime.now(IST).hour >= 19:
                merge(fetch_eod(data["asOf"], symbols, cache)); statics["levels"] = S.level_table(candles, last_lots)
    threading.Thread(target=eod_refresh, daemon=True).start()
    port = int(os.environ.get("PORT") or cfg["PORT"]); host = a.host or ("0.0.0.0" if os.environ.get("RENDER") or os.environ.get("PORT") else "127.0.0.1")
    if host != "127.0.0.1" and not cfg.get("APP_PASSWORD"): log("WARNING: public address without APP_PASSWORD")
    log(f"KRT Options Terminal at http://localhost:{port}")
    ThreadingHTTPServer((host, port), make_handler(cfg, eng, statics)).serve_forever()

if __name__ == "__main__": main()
