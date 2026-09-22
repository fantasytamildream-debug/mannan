"""Replay past sessions with real 5-minute history, through the SAME engine rules.

Used to fill days the live server did not record (e.g. after the zip ends and before the site
started). Spot and option prices come from Angel One historical 5-minute candles. Only candles
that had already closed at each moment are used, so nothing from later in the day leaks in.
What history cannot give: bid/ask spread (a 0.4% spread is assumed) and tick-by-tick order
inside a candle (open → low → high → close for green candles, open → high → low → close for red).
"""
import json, math, time
from datetime import datetime, timedelta, date
from pathlib import Path
import strategy as S
from engine import Engine, IST

class ReplayBroker:
    name = "Replay"; hint = "replay"
    def __init__(self, real, day):
        self.real, self.day, self.now, self.cache, self.notes = real, day, None, {}, set()
    def contracts(self, sym): return self.real.contracts(sym)
    def _opt_candles(self, token):
        if token not in self.cache:
            try: self.cache[token] = self.real.history("NFO", token, self.day)
            except Exception as e: self.cache[token] = []; self.notes.add(f"option history error: {e}")
        return self.cache[token]
    def option_quotes(self, tokens):
        out = {}
        for t in tokens:
            rows = [r for r in self._opt_candles(t) if r[0] + timedelta(minutes=5) <= self.now]   # completed candles only
            if not rows: continue
            last = rows[-1]; px = last[4]; vol = sum(r[5] for r in rows)
            out[str(t)] = dict(ltp=px, bid=round(px * 0.998, 2), ask=round(px * 1.002, 2), volume=vol, oi=None, ts=self.now)
        return out

def ticks_for(c5):
    """Four price points inside a 5-minute candle, in a plausible order."""
    t, o, h, l, cl, v = c5
    path = [o, l, h, cl] if cl >= o else [o, h, l, cl]
    return [(t + timedelta(seconds=s), p) for s, p in zip((5, 90, 180, 290), path)]

def replay_day(cfg, real, candles, lots, data_dir, day, log=print):
    if not hasattr(real, "history"): return "broker has no history API"
    f = Path(data_dir) / f"calls_{day}.json"
    if f.exists(): return "already recorded"
    rb = ReplayBroker(real, day.isoformat()); c2 = dict(cfg); c2["_SOURCE"] = "replay"; c2["IGNORE_MARKET_HOURS"] = "0"; c2["BAR_SECONDS"] = "300"
    eng = Engine(c2, rb, candles, lots, data_dir, lambda *a: None)
    eng.new_day(datetime(day.year, day.month, day.day, 9, 0, tzinfo=IST))
    syms = sorted({c["sym"] for c in eng.calls.values()})
    if not syms: eng.save(force=True); return "no watchlist"
    eq = getattr(real, "eq", {}); series = {}
    for s in syms + ["NIFTY"]:
        tok = real.NIFTY_TOKEN if s == "NIFTY" else eq.get(s)
        if not tok: continue
        try: series[s] = real.history("NSE", tok, day.isoformat())
        except Exception as e: log(f"replay {day} {s}: {e}")
    if not any(series.get(s) for s in syms):
        f.unlink(missing_ok=True); return "no intraday history returned"
    prev = {s: next((b[4] for b in reversed(candles.get(s, [])) if b[0] < day.isoformat()), None) for s in syms}
    if series.get("NIFTY"):                      # NIFTY previous close from the last earlier session with data
        pd = day
        for _ in range(5):
            pd -= timedelta(days=1)
            if pd.weekday() > 4: continue
            try: rows = real.history("NSE", real.NIFTY_TOKEN, pd.isoformat())
            except Exception: rows = []
            if rows: prev["NIFTY"] = rows[-1][4]; break
    events = []
    for s, rows in series.items():
        for r in rows:
            for t, p in ticks_for(r): events.append((t, s, p, r))
    events.sort(key=lambda x: x[0])
    state = {}
    i = 0
    while i < len(events):
        t = events[i][0]; batch = []
        while i < len(events) and events[i][0] == t: batch.append(events[i]); i += 1
        for _, s, p, r in batch:
            st = state.setdefault(s, dict(open=p, high=p, low=p, vol=0.0, bars=set()))
            st["high"], st["low"], st["ltp"] = max(st["high"], p), min(st["low"], p), p
            if r[0] not in st["bars"]: st["bars"].add(r[0]); st["vol"] += r[5]
        q = {s: dict(ltp=st["ltp"], open=st["open"], high=st["high"], low=st["low"], prev_close=prev.get(s) or st["open"], volume=st["vol"], ts=t) for s, st in state.items()}
        rb.now = t; eng.tick(t, q)
        time.sleep(0.002)                          # let the website threads run
    with eng.lock:
        for c in eng.calls.values():
            c["src"] = "replay"
            if c["status"] == "ACTIVE": c["status"] = "STALE"
        eng.save(force=True)
    n = sum(1 for c in eng.calls.values() if c.get("entry"))
    return f"{len(eng.calls)} watched, {n} calls" + (f" ({'; '.join(rb.notes)})" if rb.notes else "")

def missing_days(first, last, data_dir):
    d, out = first, []
    while d <= last:
        if d.weekday() < 5 and not (Path(data_dir) / f"calls_{d}.json").exists(): out.append(d)
        d += timedelta(days=1)
    return out
