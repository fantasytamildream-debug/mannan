"""KRT call engine: turns live quotes into calls with a timestamped lifecycle.

Status flow for every watchlist stock:
  WATCHING -> NEAR (close to trigger) -> BREAKOUT (crossed, waiting for the 5-min close)
           -> ACTIVE (all checks passed at a candle close, entry recorded)
           -> T1 / T2 / T3 / SL / SQUARED_OFF (closed)
  or BLOCKED (candle closed beyond trigger but a check failed; re-checked on the next candle)
  or INVALID (stop level broke before entry) / EXPIRED (no entry before the cut-off)
"""
import json, math, threading, time
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
import strategy as S

IST = timezone(timedelta(hours=5, minutes=30))
PRE = ("WATCHING", "NEAR", "BREAKOUT", "BLOCKED")
CLOSED = ("CLOSED",)

def hm(s):
    h, m = s.split(":"); return int(h) * 60 + int(m)

def f2(v): return "–" if v is None else f"{v:,.2f}"

class Engine:
    def __init__(self, cfg, broker, candles, lots, data_dir, notify):
        self.cfg, self.broker, self.candles, self.lots, self.dir, self.notify = cfg, broker, candles, lots, Path(data_dir), notify
        self.dir.mkdir(parents=True, exist_ok=True)
        g = lambda k, d: type(d)(cfg.get(k, d))
        self.risk_limit = g("RISK_PER_TRADE", 2500.0); self.max_lots = g("MAX_LOTS", 1); self.min_score = g("MIN_SCORE", 60.0)
        self.top = g("TOP_PER_SIDE", 10); self.tdelta = g("TARGET_DELTA", 0.45); self.max_spread = g("MAX_SPREAD_PCT", 3.0)
        self.min_delta = g("MIN_DELTA", 0.22); self.min_opt_lots = g("MIN_OPTION_VOLUME_LOTS", 20.0); self.vol_pace = g("VOLUME_PACE", 1.0); self.min_dte = g("MIN_DTE", 3)
        self.index_filter = cfg.get("INDEX_FILTER", "1") == "1"; self.max_active = g("MAX_ACTIVE", 4)
        self.bar_s = g("BAR_SECONDS", 300); self.ignore_hours = cfg.get("IGNORE_MARKET_HOURS", "0") == "1"
        self.t_entry, self.t_cut, self.t_sq = hm(cfg.get("ENTRY_START", "09:25")), hm(cfg.get("NO_NEW_ENTRY", "14:30")), hm(cfg.get("SQUARE_OFF", "15:15"))
        self.lock = threading.RLock(); self.day = None; self.calls = {}; self.bars = {}; self.q = {}; self.optq = {}
        self.feed = dict(status="starting", last_poll=None, exch_age=None, error="", broker=getattr(broker, "name", ""))
        self._dirty = 0.0; self.source = cfg.get("_SOURCE", "live"); self._plan_at = 0.0; self.autosave = self.source == "live"


    # ------------------------------------------------------------------ planned contract for every watchlist stock (before entry)
    def plan_contract(self, c, now):
        """Strike we expect to buy if the trigger confirms. The final pick at entry can differ if this one is illiquid."""
        cons = [x for x in self.broker.contracts(c["sym"]) if x["type"] == c["side"]]
        if not cons: return None
        dd = lambda x: x["expiry"] if isinstance(x["expiry"], date) else date.fromisoformat(str(x["expiry"]))
        exps = sorted({dd(x) for x in cons}); exps = [e for e in exps if (e - now.date()).days >= self.min_dte] or exps
        ex = exps[0]; pool = [x for x in cons if dd(x) == ex]; T = S.years_to(ex, now); iv = max(.15, c["f"]["hv"] * 1.2)
        best = min(pool, key=lambda x: abs(abs(S.bs(c["trig"], x["strike"], T, .065, iv, c["side"])[1]) - self.tdelta))
        tg, R = S.targets(c["trig"], c["sl"], c["side"]); Tl = max(T - 2 / (365 * 24), 1e-4)
        pr = lambda sp: round(S.bs(sp, best["strike"], Tl, .065, iv, c["side"])[0], 2)
        lot = best.get("lot") or c["lot"]; e = round(S.bs(c["trig"], best["strike"], T, .065, iv, c["side"])[0], 2); sl = pr(c["sl"])
        return dict(name=f"{c['sym']} {best['strike']:g} {c['side']} {ex:%d %b}", token=best["token"], strike=best["strike"], expiry=ex.isoformat(), lot=lot,
                    iv=iv, est_entry=e, est_sl=sl, est_t=[pr(t) for t in tg], spot_t=tg, risk_lot=(e - sl) * lot + S.friction(e * lot, sl * lot),
                    ltp=None, bid=None, ask=None, at=None)

    def refresh_plans(self, now):
        """Runs outside the quote loop: pick planned strikes once, then refresh their live option prices."""
        with self.lock: todo = [c for c in self.calls.values() if c["status"] in PRE]
        for c in todo:
            if not c.get("plan"):
                try: p = self.plan_contract(c, now)
                except Exception as e: print("plan error", c["sym"], e, flush=True); p = None
                if p:
                    with self.lock: c["plan"] = p
        toks = [c["plan"]["token"] for c in todo if c.get("plan")]
        if not toks: return
        try: oq = self.broker.option_quotes(toks)
        except Exception as e: print("plan quote error:", e, flush=True); return
        with self.lock:
            for c in todo:
                p = c.get("plan"); v = oq.get(p["token"]) if p else None
                if not v: continue
                p.update(ltp=v.get("ltp"), bid=v.get("bid"), ask=v.get("ask"), at=now.strftime("%H:%M:%S"))
                # re-price the plan with the volatility the market is actually charging right now
                spot = (self.q.get(c["sym"]) or {}).get("ltp")
                mid = (v["bid"] + v["ask"]) / 2 if v.get("bid") and v.get("ask") else v.get("ltp")
                if not (spot and mid): continue
                T = S.years_to(date.fromisoformat(p["expiry"]), now)
                iv = S.implied_vol(mid, spot, p["strike"], T, .065, c["side"])
                if not iv: continue
                pr = lambda sp, tt=max(T - 2 / (365 * 24), 1e-4): round(S.bs(sp, p["strike"], tt, .065, iv, c["side"])[0], 2)
                tg = S.targets(c["trig"], c["sl"], c["side"])[0]
                e = round(S.bs(c["trig"], p["strike"], T, .065, iv, c["side"])[0], 2); sl = pr(c["sl"])
                p.update(iv=iv, live_iv=True, est_entry=e, est_sl=sl, est_t=[pr(t) for t in tg],
                         risk_lot=(e - sl) * p["lot"] + S.friction(e * p["lot"], sl * p["lot"]))
            self._dirty = time.time()

    # ------------------------------------------------------------------ helpers
    def mins(self, now): return 10 * 60 if self.ignore_hours else now.hour * 60 + now.minute
    def market_open(self, now): return self.ignore_hours or (now.weekday() < 5 and 9 * 60 + 15 <= now.hour * 60 + now.minute <= 15 * 60 + 30)

    def ev(self, c, now, kind, text, **extra):
        e = dict(t=now.strftime("%H:%M:%S"), kind=kind, text=text); e.update(extra); c["events"].append(e); self._dirty = time.time()
        return e

    # ------------------------------------------------------------------ day setup
    def new_day(self, now):
        today = now.date(); self.day = today
        hist = {s: [b for b in c if b[0] < today.isoformat()] for s, c in self.candles.items()}
        f = self.dir / f"calls_{today}.json"
        if f.exists():
            saved = json.loads(f.read_text()); self.calls = saved["calls"]; self.bars = {}
            print("Engine: restored", len(self.calls), "calls for", today, flush=True); return
        wl = S.build_watchlist(hist, self.lots, self.min_score, self.top)
        self.calls, self.bars = {}, {}
        for w in wl:
            cid = f"{today:%y%m%d}-{w['sym']}-{w['side']}"
            self.calls[cid] = dict(id=cid, day=today.isoformat(), sym=w["sym"], side=w["side"], score=w["score"], parts=w["parts"],
                                   trig=w["trig"], sl=w["sl"], planR=w["planR"], lot=w["lot"], f={k: w["f"][k] for k in ("vah", "val", "aw", "atr", "cl", "pdh", "pdl", "h3", "l3", "hv", "vavg", "comp", "date")},
                                   src=self.source, plan=None, status="WATCHING", reason="Waiting for the market", checks=[], events=[], strong=False, contract=None, fills=[],
                                   entry=None, levels=None, stop=None, lots=0, open_lots=0, hits=[], spot=None, opt=None, pnl=None, spark=[], ospark=[])
        print(f"Engine: {today} watchlist {len(self.calls)} ({sum(c['side']=='CE' for c in self.calls.values())} CE / {sum(c['side']=='PE' for c in self.calls.values())} PE) from {max(b[0] for c in hist.values() for b in c[-1:])} close", flush=True)
        if hasattr(self.broker, "set_bias"):
            self.broker.set_bias({c["sym"]: (1 if c["side"] == "CE" else -1) for c in self.calls.values()})
        self.save(force=True)

    def save(self, force=False):
        if not force and (not self.autosave or time.time() - self._dirty > 2): return
        with self.lock:
            (self.dir / f"calls_{self.day}.json").write_text(json.dumps({"calls": self.calls}, default=str))

    def archive(self, c):
        f = self.dir / "history.json"; h = json.loads(f.read_text()) if f.exists() else []
        h = [x for x in h if x["id"] != c["id"]] + [self.summary(c)]; f.write_text(json.dumps(h))

    def summary(self, c):
        e = c["entry"] or {}
        return dict(id=c["id"], day=c["day"], sym=c["sym"], side=c["side"], src=c.get("src", "live"), contract=(c["contract"] or {}).get("name"), score=c["score"], strong=c["strong"],
                    entry_time=e.get("t"), entry=e.get("prem"), exit_time=c["events"][-1]["t"] if c["events"] else None, result=c["reason"],
                    hits=[h["name"] for h in c["hits"]], pnl=round(c["pnl"]["net"], 2) if c.get("pnl") else 0, lots=c["lots"], lot=c["lot"])

    # ------------------------------------------------------------------ main tick (called by the quote loop)
    def tick(self, now, q):
        with self.lock:
            if self.day != now.date(): self.new_day(now)
            toks = [c["contract"]["token"] for c in self.calls.values() if c["status"] == "ACTIVE"]
        oq = {}
        if toks:                                   # network call outside the lock so the website never waits on it
            try: oq = self.broker.option_quotes(toks)
            except Exception as e: print("option quote error:", e, flush=True)
        with self.lock:
            self.optq.update(oq)
            self.q = q; self.feed.update(status="ok", last_poll=now.isoformat(), error="")
            ts = [v["ts"] for v in q.values() if v.get("ts")]
            self.feed["exch_age"] = round((now - max(ts)).total_seconds()) if ts else None
            for c in list(self.calls.values()):
                sq = q.get(c["sym"])
                if sq: self.track(c, sq, now)
            self.save()

    def bar_update(self, sym, sq, now):
        k = int(now.timestamp() // self.bar_s); b = self.bars.get(sym); p = sq["ltp"]; closed = None
        if b and b["k"] != k:
            closed = b; b = None
        if not b:
            b = self.bars[sym] = dict(k=k, start=datetime.fromtimestamp(k * self.bar_s, IST).strftime("%H:%M"), o=p, h=p, l=p, c=p, dh=sq.get("high"), dl=sq.get("low"), n=0)
        # a new day high/low between polls still counts inside this bar
        if sq.get("high") and b["dh"] and sq["high"] > b["dh"]: b["h"] = max(b["h"], sq["high"])
        if sq.get("low") and b["dl"] and sq["low"] < b["dl"]: b["l"] = min(b["l"], sq["low"])
        b["dh"], b["dl"] = sq.get("high"), sq.get("low"); b["h"], b["l"], b["c"] = max(b["h"], p), min(b["l"], p), p; b["n"] += 1
        return closed, b

    def track(self, c, sq, now):
        p = sq["ltp"]; ce = c["side"] == "CE"; g = 1 if ce else -1; c["spot"] = p
        closed, bar = self.bar_update(c["sym"], sq, now)
        if closed:
            c["spark"] = (c["spark"] + [[closed["start"], round(closed["c"], 2)]])[-80:]
            if c.get("orb") is None and (closed["start"] == "09:15" or self.ignore_hours): c["orb"] = [closed["h"], closed["l"]]
        m = self.mins(now)
        if c["status"] in PRE:
            hi, lo = bar["h"], bar["l"]
            if (lo <= c["sl"]) if ce else (hi >= c["sl"]):
                c["status"], c["reason"] = "INVALID", f"Stop level {f2(c['sl'])} broke before any entry"; self.ev(c, now, "invalid", c["reason"]); return
            if m >= self.t_cut and not self.ignore_hours:
                c["status"], c["reason"] = "EXPIRED", f"No confirmed entry by {self.cfg.get('NO_NEW_ENTRY','14:30')}"; self.ev(c, now, "expired", c["reason"]); return
            beyond_close = bool(closed) and ((closed["c"] > c["trig"]) if ce else (closed["c"] < c["trig"]))
            if beyond_close:
                self.on_close(c, closed, sq, now)
                if c["status"] == "ACTIVE": return
            crossed = (p > c["trig"]) if ce else (p < c["trig"])
            if c["status"] == "BLOCKED" and (beyond_close or crossed):
                pass                                   # keep showing why it was not taken until price comes back
            else:
                dist = g * (c["trig"] - p)
                if crossed: c["status"], c["reason"] = "BREAKOUT", f"Spot crossed {f2(c['trig'])}. Waiting for the {self.bar_s // 60}-min candle to close beyond it"
                elif dist <= 0.35 * c["planR"]: c["status"], c["reason"] = "NEAR", f"{f2(abs(dist))} away from trigger {f2(c['trig'])}"
                else: c["status"], c["reason"] = "WATCHING", f"Trigger {f2(c['trig'])} ({abs(dist) / p * 100:.2f}% away)"
        elif c["status"] == "ACTIVE":
            self.manage(c, sq, bar, now)
        if c["status"] == "ACTIVE" and closed:
            oq = self.optq.get(c["contract"]["token"])
            if oq: c["ospark"] = (c["ospark"] + [[closed["start"], oq["ltp"]]])[-80:]

    # ------------------------------------------------------------------ entry decision at a completed candle
    def on_close(self, c, bar, sq, now):
        m = self.mins(now); ce = c["side"] == "CE"; g = 1 if ce else -1; checks = []
        def chk(group, name, ok, detail, fail=None): checks.append(dict(group=group, name=name, ok=bool(ok), detail=detail, fail=fail or name))
        if m < self.t_entry:
            c["status"], c["reason"] = "BLOCKED", f"Candle closed beyond trigger before {self.cfg.get('ENTRY_START','09:25')}; waiting for the next one"; return
        chk("stock", "Candle close beyond trigger", True, f"{bar['start']} candle closed {f2(bar['c'])} vs trigger {f2(c['trig'])}")
        late = g * (bar["c"] - c["trig"]) > 0.5 * c["planR"]
        chk("stock", "Entry not late", not late, f"{abs(bar['c'] - c['trig']) / c['planR']:.2f}R past trigger (limit 0.5R)", "Too late, move already done")
        el = max(15, (m - (9 * 60 + 15))) / 375 if not self.ignore_hours else 0.5
        pace = (sq.get("volume") or 0) / max(1, c["f"]["vavg"] * el)
        chk("stock", "Volume above normal pace", pace >= self.vol_pace, f"{pace:.2f}x the 20-day pace (need {self.vol_pace:g}x)", "Volume too low")
        ni = self.q.get("NIFTY"); nchg = ((ni["ltp"] / ni["prev_close"] - 1) * 100) if ni and ni.get("prev_close") else None
        if self.index_filter:
            ok = nchg is None or (nchg >= -0.15 if ce else nchg <= 0.15)
            chk("stock", "NIFTY not against the trade", ok, "NIFTY data unavailable" if nchg is None else f"NIFTY {nchg:+.2f}% today", "NIFTY against the trade")
        entry_spot = bar["c"]; tg, R = S.targets(entry_spot, c["sl"], c["side"])
        wall = c["f"]["h3"] if ce else c["f"]["l3"]
        room = not ((entry_spot < wall < tg[0]) if ce else (tg[0] < wall < entry_spot))
        chk("stock", "Room to T1", room, f"3-day {'high' if ce else 'low'} {f2(wall)} {'is in the way' if not room else 'not in the way'}", "Resistance before T1")
        active = sum(1 for x in self.calls.values() if x["status"] == "ACTIVE")
        chk("stock", "Open-call limit", active < self.max_active, f"{active} active (max {self.max_active})", "Too many open calls")
        pick, why = self.select_option(c, entry_spot, now)
        if pick:
            chk("option", "Live quote fresh", pick["fresh"], pick["age_txt"])
            chk("option", "Bid–ask spread", True, f"{pick['spread']:.2f}% (max {self.max_spread:g}%)")
            chk("option", "Traded volume", True, f"{pick['vol_lots']:.0f} lots today (min {self.min_opt_lots:g})")
            chk("option", f"Risk ≤ ₹{self.risk_limit:,.0f}", True, f"₹{pick['risk_lot']:,.0f} per lot incl. costs")
        else:
            chk("option", "Tradable option contract", False, why, "No tradable option")
        c["checks"] = checks
        c["orb_break"] = bool(c.get("orb") and ((bar["c"] > c["orb"][0]) if ce else (bar["c"] < c["orb"][1])))
        c["strong"] = bool(c["parts"]["coil"] >= 7.5 and pace >= 1.5 and c.get("orb") and ((bar["c"] > c["orb"][0]) if ce else (bar["c"] < c["orb"][1])) and (nchg is None or g * nchg > 0))
        failed = [x for x in checks if not x["ok"]]
        if failed:
            c["status"] = "BLOCKED"; c["reason"] = "Not taken: " + "; ".join(f"{x['fail']} ({x['detail']})" for x in failed)
            key = "|".join(x["name"] for x in failed)
            if c.get("_block") != key: c["_block"] = key; self.ev(c, now, "blocked", "Candle closed beyond trigger but not taken: " + ", ".join(x["fail"].lower() for x in failed))
            return
        # ---- ENTRY
        lots = max(1, min(self.max_lots, int(self.risk_limit // pick["risk_lot"])))
        c.update(status="ACTIVE", contract=pick["contract"], lots=lots, open_lots=lots, stop=c["sl"], exits=S.exit_plan(lots),
                 entry=dict(t=now.strftime("%H:%M:%S"), bar=bar["start"], spot=entry_spot, prem=pick["ask"], ltp=pick["ltp"], iv=pick["iv"], delta=pick["delta"],
                            chase=round(pick["ask"] * 1.05, 2), risk_lot=pick["risk_lot"]),
                 levels=dict(sl=c["sl"], t=tg, R=R, est=dict(sl=pick["est_sl"], t=pick["est_t"])), reason="Entry taken")
        self.optq[pick["contract"]["token"]] = dict(ltp=pick["ltp"], bid=pick["bid"], ask=pick["ask"])
        e = self.ev(c, now, "entry", f"ENTRY {pick['contract']['name']} at ₹{f2(pick['ask'])} (spot {f2(entry_spot)})", spot=entry_spot, prem=pick["ask"])
        self.alert(c, "entry", e)

    def select_option(self, c, spot, now):
        ce = c["side"] == "CE"
        try: cons = [x for x in self.broker.contracts(c["sym"]) if x["type"] == c["side"]]
        except Exception as e: return None, f"contract list error: {e}"
        exps = sorted({x["expiry"] if isinstance(x["expiry"], date) else date.fromisoformat(str(x["expiry"])) for x in cons})
        exps = [e for e in exps if (e - now.date()).days >= self.min_dte] or exps
        if not exps: return None, "no option contracts listed"
        ex = exps[0]; pool = [x for x in cons if (x["expiry"] if isinstance(x["expiry"], date) else date.fromisoformat(str(x["expiry"]))) == ex]
        near = sorted(pool, key=lambda x: abs(x["strike"] - spot))[:5]
        g = 1 if c["side"] == "CE" else -1                      # two cheaper strikes further out, in case risk does not fit
        far = sorted([x for x in pool if g * (x["strike"] - spot) > 0 and x not in near], key=lambda x: abs(x["strike"] - spot))[:2]
        pool = near + far
        try: oq = self.broker.option_quotes([x["token"] for x in pool])
        except Exception as e: return None, f"option quote error: {e}"
        T = S.years_to(ex, now); rej = []; cands = []
        for x in pool:
            v = oq.get(x["token"]); lot = x.get("lot") or c["lot"]; name = f"{c['sym']} {x['strike']:g} {c['side']} {ex:%d %b}"
            if not v or not v.get("bid") or not v.get("ask"): rej.append(f"{x['strike']:g}: no bid/ask"); continue
            mid = (v["bid"] + v["ask"]) / 2; spread = (v["ask"] - v["bid"]) / mid * 100
            age = (now - v["ts"]).total_seconds() if v.get("ts") else None
            fresh = age is None or age <= 90
            vol_lots = (v.get("volume") or 0) / max(1, lot)
            if spread > self.max_spread: rej.append(f"{x['strike']:g}: spread {spread:.1f}%"); continue
            if vol_lots < self.min_opt_lots: rej.append(f"{x['strike']:g}: only {vol_lots:.0f} lots traded"); continue
            if not fresh: rej.append(f"{x['strike']:g}: quote {age:.0f}s old"); continue
            iv = S.implied_vol(mid, spot, x["strike"], T, .065, c["side"]) or max(.15, c["f"]["hv"] * 1.2)
            delta = S.bs(spot, x["strike"], T, .065, iv, c["side"])[1]
            if abs(delta) < self.min_delta: rej.append(f"{x['strike']:g}: delta {delta:.2f} too far OTM"); continue
            Tl = max(T - 2 / (365 * 24), 1e-4)
            est_sl = S.bs(c["sl"], x["strike"], Tl, .065, iv, c["side"])[0]
            est_t = [S.bs(t, x["strike"], Tl, .065, iv, c["side"])[0] for t in S.targets(spot, c["sl"], c["side"])[0]]
            risk = (v["ask"] - est_sl) * lot + S.friction(v["ask"] * lot, est_sl * lot)
            cands.append(dict(contract=dict(name=name, token=x["token"], strike=x["strike"], expiry=ex.isoformat(), lot=lot), ltp=v["ltp"], bid=v["bid"], ask=v["ask"],
                              spread=spread, vol_lots=vol_lots, fresh=fresh, age_txt="exchange time not sent" if age is None else f"{age:.0f}s old",
                              iv=iv, delta=delta, est_sl=est_sl, est_t=est_t, risk_lot=risk))
        if not cands: return None, "no liquid strike near the money (" + ", ".join(rej[:4]) + ")"
        cands.sort(key=lambda z: abs(abs(z["delta"]) - self.tdelta))
        for z in cands:
            if z["risk_lot"] <= self.risk_limit: c["lot"] = z["contract"]["lot"]; return z, ""
        z = min(cands, key=lambda z: z["risk_lot"])
        return None, f"lowest risk is ₹{z['risk_lot']:,.0f}/lot on {z['contract']['name']}, above the ₹{self.risk_limit:,.0f} limit (stop not tightened to fit)"

    # ------------------------------------------------------------------ managing an open call
    def manage(self, c, sq, bar, now):
        ce = c["side"] == "CE"; L = c["levels"]; oq = self.optq.get(c["contract"]["token"]) or {}
        opt_ltp = oq.get("ltp") or c["entry"]["prem"]; sell_px = oq.get("bid") or opt_ltp; c["opt"] = dict(ltp=opt_ltp, bid=oq.get("bid"), ask=oq.get("ask"))
        hi = lo = sq["ltp"]                      # only prices seen since the previous poll count
        if sq.get("high") and c.get("_dh") and sq["high"] > c["_dh"]: hi = max(hi, sq["high"])
        if sq.get("low") and c.get("_dl") and sq["low"] < c["_dl"]: lo = min(lo, sq["low"])
        c["_dh"], c["_dl"] = sq.get("high"), sq.get("low")
        stop_hit = (lo <= c["stop"]) if ce else (hi >= c["stop"])
        if stop_hit:
            kind = "sl" if c["stop"] == L["sl"] else "trail"
            self.exit(c, now, c["open_lots"], sell_px, "SL" if kind == "sl" else "Trailing stop", sq["ltp"])
        else:
            done = {h["name"] for h in c["hits"]}
            for i, t in enumerate(L["t"]):
                nm = f"T{i+1}"
                if nm in done: continue
                if (hi >= t) if ce else (lo <= t):
                    h = dict(name=nm, t=now.strftime("%H:%M:%S"), spot=sq["ltp"], level=t, prem=sell_px); c["hits"].append(h)
                    n_out = c["exits"][i] if c["open_lots"] > 0 else 0
                    if i == 2: n_out = c["open_lots"]
                    if n_out: self.fill(c, now, n_out, sell_px, nm)
                    if i == 0: c["stop"] = c["entry"]["spot"]; msg = "stop moved to cost"
                    elif i == 1: c["stop"] = L["t"][0]; msg = "stop moved to T1"
                    else: msg = "all lots closed"
                    e = self.ev(c, now, nm.lower(), f"{nm} hit at spot {f2(sq['ltp'])}, option ₹{f2(sell_px)} ({msg})", prem=sell_px, spot=sq["ltp"])
                    self.alert(c, nm.lower(), e)
                    if c["open_lots"] == 0: self.close(c, now, nm); return
        if c["status"] == "ACTIVE" and self.mins(now) >= self.t_sq and not self.ignore_hours:
            self.exit(c, now, c["open_lots"], sell_px, "Square-off", sq["ltp"])
        self.mark(c)

    def fill(self, c, now, lots, px, why):
        c["fills"].append(dict(t=now.strftime("%H:%M:%S"), lots=lots, px=px, why=why)); c["open_lots"] -= lots

    def exit(self, c, now, lots, px, why, spot):
        if lots: self.fill(c, now, lots, px, why)
        e = self.ev(c, now, "exit" if why != "SL" else "sl", f"{why} at spot {f2(spot)}, option ₹{f2(px)}", prem=px, spot=spot)
        self.alert(c, "exit", e); self.close(c, now, why)

    def close(self, c, now, why):
        c["status"] = "CLOSED"; best = [h["name"] for h in c["hits"]]
        c["reason"] = (f"{best[-1]} hit" if best else why) + (f", rest closed by {why.lower()}" if best and why not in best else "")
        self.mark(c); self.archive(c); self.save(force=True)

    def mark(self, c):
        lot = c["contract"]["lot"]; e = c["entry"]["prem"]; ltp = (c.get("opt") or {}).get("bid") or (c.get("opt") or {}).get("ltp") or e
        real = sum((f["px"] - e) * f["lots"] * lot for f in c["fills"]); unreal = (ltp - e) * c["open_lots"] * lot
        buy = e * c["lots"] * lot; sell = sum(f["px"] * f["lots"] * lot for f in c["fills"]) + ltp * c["open_lots"] * lot
        cost = S.friction(buy, sell); c["pnl"] = dict(real=real, unreal=unreal, gross=real + unreal, cost=cost, net=real + unreal - cost,
                                                      pct=(ltp / e - 1) * 100 if c["open_lots"] else ((sell / buy - 1) * 100 if buy else 0))

    # ------------------------------------------------------------------ alerts + snapshot
    def alert(self, c, kind, e):
        try: self.notify(c, kind, e)
        except Exception as x: print("alert error:", x, flush=True)

    def snapshot(self, now):
        with self.lock:
            lp = datetime.fromisoformat(self.feed["last_poll"]) if self.feed.get("last_poll") else None
            poll_age = (now - lp).total_seconds() if lp else None
            st = "disconnected" if poll_age is None or poll_age > 90 or self.feed["status"] == "error" else \
                 "delayed" if poll_age > 30 or (self.market_open(now) and (self.feed.get("exch_age") or 0) > 60) else "live"
            ni = self.q.get("NIFTY")
            return dict(now=now.isoformat(), day=str(self.day), feed=dict(self.feed, health=st, poll_age=poll_age), market_open=self.market_open(now),
                        nifty=dict(ltp=ni["ltp"], chg=(ni["ltp"] / ni["prev_close"] - 1) * 100 if ni.get("prev_close") else None) if ni else None,
                        cfg=dict(risk=self.risk_limit, max_lots=self.max_lots, min_score=self.min_score, spread=self.max_spread, bar=self.bar_s // 60,
                                 entry=self.cfg.get("ENTRY_START", "09:25"), cut=self.cfg.get("NO_NEW_ENTRY", "14:30"), sq=self.cfg.get("SQUARE_OFF", "15:15"),
                                 vol_pace=self.vol_pace, min_opt_lots=self.min_opt_lots, delta=self.tdelta),
                        calls=list(self.calls.values()))

    def days(self):
        """Dates that have a saved live call file (newest first)."""
        return sorted((p.stem[6:] for p in self.dir.glob("calls_*.json")), reverse=True)

    def day_calls(self, d):
        if d == str(self.day):
            with self.lock: return list(self.calls.values())
        f = self.dir / f"calls_{d}.json"
        return list(json.loads(f.read_text())["calls"].values()) if f.exists() else None

    def history(self):
        f = self.dir / "history.json"; return json.loads(f.read_text()) if f.exists() else []
