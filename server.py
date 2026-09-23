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
import replay as RP

ROOT = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))
log = B.log

KEYS = ("BROKER", "ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_MPIN", "ANGEL_TOTP_SECRET", "DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_TOPIC_THREAD_ID", "APP_PASSWORD", "PORT", "DATA_DIR", "DEMO", "DEMO_VOL",
        "QUOTE_INTERVAL_SECONDS", "RISK_PER_TRADE", "MAX_LOTS", "MIN_SCORE", "TOP_PER_SIDE", "TARGET_DELTA", "MAX_SPREAD_PCT",
        "MIN_OPTION_VOLUME_LOTS", "VOLUME_PACE", "MIN_DTE", "INDEX_FILTER", "MAX_ACTIVE", "BAR_SECONDS", "IGNORE_MARKET_HOURS",
        "ENTRY_START", "NO_NEW_ENTRY", "SQUARE_OFF", "REPLAY", "MIN_DELTA", "ANGEL_RATE_GAP",
        "GITHUB_REPO", "GITHUB_TOKEN", "GITHUB_BRANCH", "GITHUB_DIR")

def load_cfg():
    cfg = {"PORT": "8765", "QUOTE_INTERVAL_SECONDS": "6"}
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


# ------------------------------------------------------------------ free storage: keep call records in the GitHub repo
class GitStore:
    """Render's free plan has no disk, so today's calls would die on every restart.
    With a GitHub token the server saves every call file back into the repo and reloads it on start."""
    def __init__(self, cfg, cache):
        self.repo, self.tok = cfg.get("GITHUB_REPO", ""), cfg.get("GITHUB_TOKEN", "")
        self.branch = cfg.get("GITHUB_BRANCH", "main"); self.dir = cfg.get("GITHUB_DIR", "records")
        self.cache, self.on, self.sha, self.q, self.last = Path(cache), bool(self.repo and self.tok), {}, {}, {}
        self.lock = threading.Lock()
    def _h(self): return {"Authorization": "Bearer " + self.tok, "Accept": "application/vnd.github+json",
                          "User-Agent": "krt-terminal", "Content-Type": "application/json"}
    def _url(self, name=""): return f"https://api.github.com/repos/{self.repo}/contents/{self.dir}" + (f"/{name}" if name else "")
    def restore(self):
        if not self.on: return "off"
        try: items = json.loads(B.http(self._url() + f"?ref={self.branch}", headers=self._h(), timeout=20))
        except urllib.error.HTTPError as e:
            if e.code == 404: return "empty (nothing saved yet)"
            return f"restore failed: HTTP {e.code}"
        except Exception as e: return f"restore failed: {e}"
        n = 0
        for it in items if isinstance(items, list) else []:
            if not it["name"].endswith(".json"): continue
            self.sha[it["name"]] = it["sha"]
            try:
                raw = B.http(it["download_url"], headers={"User-Agent": "krt-terminal"}, timeout=20)
                (self.cache / it["name"]).write_bytes(raw); n += 1
            except Exception as e: log("restore", it["name"], e)
        return f"{n} files restored from GitHub"
    def queue(self, path):
        if not self.on: return
        with self.lock: self.q[Path(path).name] = time.time()
    def _push(self, name):
        p = self.cache / name
        if not p.exists(): return
        body = {"message": f"calls {name}", "content": base64.b64encode(p.read_bytes()).decode(), "branch": self.branch}
        if self.sha.get(name): body["sha"] = self.sha[name]
        try:
            r = json.loads(B.http(self._url(name), body, self._h(), timeout=25, method="PUT"))
            self.sha[name] = r["content"]["sha"]
        except urllib.error.HTTPError as e:
            if e.code == 409 or e.code == 422:                     # stale sha: look it up and retry once
                try:
                    cur = json.loads(B.http(self._url(name) + f"?ref={self.branch}", headers=self._h(), timeout=20))
                    self.sha[name] = cur["sha"]; body["sha"] = cur["sha"]
                    r = json.loads(B.http(self._url(name), body, self._h(), timeout=25, method="PUT")); self.sha[name] = r["content"]["sha"]
                except Exception as x: log("GitHub save failed", name, x)
            else: log("GitHub save failed", name, e.code, e.read()[:200])
        except Exception as e: log("GitHub save failed", name, e)
    def loop(self):
        while True:
            time.sleep(20)
            with self.lock: due = [n for n, t in self.q.items() if time.time() - self.last.get(n, 0) > 60]
            for n in due:
                self.last[n] = time.time()
                with self.lock: self.q.pop(n, None)
                self._push(n)

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
BOOT = {"status": "starting", "eng": None, "statics": {}, "storage": "temporary"}

def make_handler(cfg, _eng=None, _statics=None):
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
            u = urlparse(self.path); now = datetime.now(IST); eng, statics = BOOT["eng"], BOOT["statics"]
            if u.path == "/ping": return self.send(200, {"ok": True, "status": BOOT["status"]})
            if u.path in ("/", "/index.html"): return self.send(200, (ROOT / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
            if eng is None:
                if u.path == "/api/state": return self.send(200, {"starting": True, "status": BOOT["status"], "now": now.isoformat()})
                return self.send(503, {"error": "starting", "status": BOOT["status"]})
            if u.path == "/api/state":
                st = eng.snapshot(now); st["storage"] = BOOT.get("storage"); st["storage_path"] = BOOT.get("storage_path"); return self.send(200, st)
            if u.path == "/api/history": return self.send(200, eng.history())
            if u.path == "/api/levels": return self.send(200, statics["levels"])
            if u.path == "/api/backtest": return self.send(200, statics["backtest"])
            if u.path == "/api/days":
                return self.send(200, {"live": eng.days(), "backtest": statics["bt_days"], "today": now.date().isoformat(), "replay": statics.get("replay")})
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
    port = int(os.environ.get("PORT") or cfg["PORT"]); host = a.host or ("0.0.0.0" if os.environ.get("RENDER") or os.environ.get("PORT") else "127.0.0.1")
    if host != "127.0.0.1" and not cfg.get("APP_PASSWORD"): log("WARNING: public address without APP_PASSWORD")
    srv = ThreadingHTTPServer((host, port), make_handler(cfg)); srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"KRT Options Terminal at http://localhost:{port} (starting up)")
    BOOT["status"] = "loading market data"
    data = json.loads((ROOT / "data" / "market_data.json").read_text()); symbols = set(data["lots"]) - set(data["indices"])
    cache = Path(cfg.get("DATA_DIR") or (ROOT / "cache")); cache.mkdir(parents=True, exist_ok=True)
    # Is this folder a real disk that survives restarts, or throwaway container space?
    BOOT["storage"] = "persistent" if cfg.get("DATA_DIR") and not str(cache).startswith(str(ROOT)) else "temporary"
    BOOT["storage_path"] = str(cache)
    store = GitStore(cfg, cache)
    if store.on:
        BOOT["status"] = "restoring saved calls from GitHub"; msg = store.restore(); log("GitHub storage:", msg)
        BOOT["storage"] = "github"; BOOT["storage_path"] = f"{store.repo}/{store.dir}"
        threading.Thread(target=store.loop, daemon=True).start()
    if BOOT["storage"] == "temporary":
        log("WARNING: calls and history are stored in", cache, "- they will be LOST on every restart.")
        log("         Add a Render disk mounted at /var/data and set DATA_DIR=/var/data")
    else: log("Storage:", cache, "(survives restarts)")
    candles = {s: list(data["candles"][s]) for s in symbols}
    def merge(rows):
        for s, rs in rows.items():
            have = {r[0] for r in candles.get(s, [])}; candles.setdefault(s, []).extend(r for r in rs if r[0] not in have); candles[s].sort(key=lambda r: r[0])
    if not a.no_eod: BOOT["status"] = "downloading NSE end-of-day files"; log(f"Checking NSE bhavcopy after {data['asOf']}..."); merge(fetch_eod(data["asOf"], symbols, cache))
    BOOT["status"] = "logging in to broker and loading contract list"
    try:
        broker = B.make_broker(cfg, symbols, cache, candles); log(f"{broker.name} connected. {broker.connect()}")
    except B.BrokerError as e:
        BOOT["status"] = f"broker login failed: {e}"; log(BOOT["status"])
        while True: time.sleep(3600)            # keep the site up so the error is visible
    except urllib.error.HTTPError as e:
        BOOT["status"] = f"broker login failed: HTTP {e.code}"; log(BOOT["status"], e.read()[:300])
        while True: time.sleep(3600)
    tg = Telegram(cfg)
    def notify(c, kind, e):
        if kind == "entry": tg.send(f"{c['id']}|entry", call_text(c))
        else:
            p = c.get("pnl") or {}; tg.send(f"{c['id']}|{kind}|{e['t']}" if kind == "exit" else f"{c['id']}|{kind}",
                    f"{'✅' if kind.startswith('t') else '⛔' if kind in ('sl',) else '🔚'} {c['contract']['name']}\n{e['text']} at {e['t']} IST\nNet so far ≈ ₹{p.get('net', 0):,.0f}")
    eng = Engine(cfg, broker, candles, {s: data["lots"][s] for s in symbols}, cache, notify)
    eng.on_file = store.queue
    last_lots = {s: data["lots"][s] for s in symbols if s in data["lots"]}
    if hasattr(broker, "lot_of"):                       # real lot sizes from the broker's contract list
        for s in symbols:
            l = broker.lot_of(s)
            if l: last_lots[s] = l
    eng.lots = last_lots
    T = data["trades"]; nw = sum(t[9] for t in T if t[9] > 0); nl = -sum(t[9] for t in T if t[9] < 0)
    bt_by_day = {}
    for t in T: bt_by_day.setdefault(t[0], []).append(t)
    statics_extra = {"bt_by_day": bt_by_day, "bt_days": sorted(bt_by_day, reverse=True)}
    statics = {"levels": S.level_table(candles, last_lots),
               "backtest": dict(trades=len(T), win=sum(t[12] for t in T) / len(T) * 100, pf_net=nw / nl, net=sum(t[9] for t in T),
                                note="Package backtest, NOT verified: synthetic option prices, same-day close used for entry, targets checked before stops.",
                                rows=T)}
    statics.update(statics_extra)
    BOOT["statics"] = statics; BOOT["eng"] = eng; BOOT["status"] = "ready"; log("Ready.")
    interval = float(cfg["QUOTE_INTERVAL_SECONDS"])
    def loop():
        while True:
            t0 = time.time(); now = datetime.now(IST)
            try:
                if eng.market_open(now) or now.hour * 60 + now.minute in range(9 * 60, 15 * 60 + 40) or eng.day is None:
                    eng.tick(now, broker.quotes(eng.quote_symbols()))
                else:
                    with eng.lock:
                        if eng.day != now.date(): eng.new_day(now)
                        eng.feed.update(status="ok", last_poll=now.isoformat())
            except Exception as e:
                eng.feed.update(status="error", error=f"{broker.name}: {getattr(e, 'code', '') or e} ({broker.hint})"); log("Feed error:", e); time.sleep(10)
            busy = eng.market_open(now)
            time.sleep(max(0.5, interval - (time.time() - t0)) if busy else 30)
    threading.Thread(target=loop, daemon=True).start()
    def plans():
        while True:
            now = datetime.now(IST)
            try:
                if eng.day: eng.refresh_plans(now)
            except Exception as e: log("plan refresh error:", e)
            time.sleep(30 if eng.market_open(now) else 900)
    threading.Thread(target=plans, daemon=True).start()
    statics["replay"] = "not started"
    def replays():
        if not hasattr(broker, "history") or cfg.get("REPLAY", "1") != "1":
            statics["replay"] = "not available for this broker"; return
        time.sleep(20); now = datetime.now(IST)
        first = datetime.strptime(max(statics["bt_days"]), "%Y-%m-%d").date() + timedelta(days=1)
        last = now.date() - timedelta(days=1); first = max(first, last - timedelta(days=45))
        todo = RP.missing_days(first, last, cache)
        for i, d in enumerate(todo):
            statics["replay"] = f"replaying {d} ({i + 1}/{len(todo)})"; log("Replay", d, "...")
            try: log("Replay", d, "→", RP.replay_day(cfg, broker, candles, last_lots, cache, d, log))
            except Exception as e: log("Replay", d, "failed:", e)
            time.sleep(3)
        statics["replay"] = f"done ({len(todo)} day{'s' if len(todo) != 1 else ''})"
    threading.Thread(target=replays, daemon=True).start()
    def eod_refresh():
        while True:
            time.sleep(3600)
            if not a.no_eod and datetime.now(IST).hour >= 19:
                merge(fetch_eod(data["asOf"], symbols, cache)); statics["levels"] = S.level_table(candles, last_lots)
    threading.Thread(target=eod_refresh, daemon=True).start()
    try:
        while True: time.sleep(3600)
    except KeyboardInterrupt: log("Stopped.")

if __name__ == "__main__": main()
