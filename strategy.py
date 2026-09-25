"""KRT Options Terminal - strategy maths (pure functions, no network).

One rulebook, used by the live engine and by the tests:
  Watchlist  : previous-day close outside the 3-day value area on the monthly-AVWAP side,
               trend aligned (close > SMA20 > SMA50 for CE, reverse for PE), quality score >= MIN_SCORE.
  Trigger    : spot level = value-area edge +/-0.5%, beyond the previous day's high/low.
  Entry      : a COMPLETED 5-minute candle closes beyond the trigger (no intrabar entries),
               and every stock + option confirmation passes at that moment.
  Stop       : fixed spot level set the night before (back inside the value area, 0.4-0.8 ATR).
  Targets    : T1 / T2 / T3 = entry spot +/- 1R / 2R / 3R, where R = |entry spot - stop|.
  Exits      : 1 lot  -> T1 moves stop to cost, T2 moves stop to T1, T3 exits.
               2 lots -> 1 lot out at T1 (stop to cost), last lot trails to T3.
               3+ lots -> a third out at each target.
               Anything open at SQUARE_OFF (15:15) exits at market.
"""
import math
from datetime import date, datetime, timedelta

# ------------------------------------------------------------------ Black-Scholes
def ncdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def npdf(x): return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def bs(S, K, T, r, v, ty):
    """price, delta"""
    if T <= 0 or v <= 0 or S <= 0 or K <= 0:
        intr = max(0.0, S - K) if ty == "CE" else max(0.0, K - S)
        return intr, (1.0 if S > K else 0.0) if ty == "CE" else (-1.0 if S < K else 0.0)
    sq = math.sqrt(T); d1 = (math.log(S / K) + (r + v * v / 2) * T) / (v * sq); d2 = d1 - v * sq
    if ty == "CE": return S * ncdf(d1) - K * math.exp(-r * T) * ncdf(d2), ncdf(d1)
    return K * math.exp(-r * T) * ncdf(-d2) - S * ncdf(-d1), ncdf(d1) - 1

def implied_vol(price, S, K, T, r, ty):
    """Bisection; returns None if the price is below intrinsic or unsolvable."""
    lo, hi = 0.01, 4.0
    if price <= 0 or T <= 0: return None
    if price < bs(S, K, T, r, lo, ty)[0] - 1e-6: return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs(S, K, T, r, mid, ty)[0] > price: hi = mid
        else: lo = mid
    return (lo + hi) / 2

def years_to(expiry_d, now):
    """Time to 15:30 IST on expiry, in years (min ~1 hour)."""
    end = datetime(expiry_d.year, expiry_d.month, expiry_d.day, 15, 30, tzinfo=now.tzinfo)
    return max(3600.0, (end - now).total_seconds()) / (365 * 86400)

def step_for(s):
    return 1 if s < 100 else 2.5 if s < 250 else 5 if s < 500 else 10 if s < 1000 else 20 if s < 2500 else 50 if s < 5000 else 100 if s < 10000 else 250

# ------------------------------------------------------------------ costs (same formula as the original package)
def friction(buy_value, sell_value, slippage_pct=0.9):
    br = 40.0; stt = sell_value * 0.001; to = buy_value + sell_value; ex = to * 0.0005
    gst = (br + ex) * 0.18; sebi = to * 1e-6; stamp = buy_value * 3e-5; slip = to * slippage_pct / 100
    return br + stt + ex + gst + sebi + stamp + slip

# ------------------------------------------------------------------ daily features
def daily_features(c):
    """c = list of [date, o, h, l, c, v] ending with the last COMPLETED session."""
    i = len(c) - 1
    if i < 50: return None
    p3 = c[i-2:i+1]; h = max(b[2] for b in p3); l = min(b[3] for b in p3); r = h - l
    vah, val = h - .15 * r, l + .15 * r
    m = c[i][0][:7]; mc = [b for b in c if b[0][:7] == m]; vv = sum(b[5] for b in mc)
    aw = (sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in mc) / vv if vv          # volume-weighted for stocks
          else sum((b[2] + b[3] + b[4]) / 3 for b in mc) / len(mc))            # plain average for indices (no volume)
    atr = sum(max(c[k][2] - c[k][3], abs(c[k][2] - c[k-1][4]), abs(c[k][3] - c[k-1][4])) for k in range(i-13, i+1)) / 14
    cl = c[i][4]; s20 = sum(b[4] for b in c[i-19:i+1]) / 20; s50 = sum(b[4] for b in c[i-49:i+1]) / 50
    vavg = sum(b[5] for b in c[i-20:i]) / 20; rg = c[i][2] - c[i][3]
    has_vol = vavg > 0
    rets = [math.log(c[k][4] / c[k-1][4]) for k in range(i-19, i+1)]; mu = sum(rets) / 20
    hv = math.sqrt(sum((x - mu) ** 2 for x in rets) / 20 * 252)
    return dict(date=c[i][0], vah=vah, val=val, aw=aw, atr=atr, cl=cl, s20=s20, s50=s50, vavg=vavg, has_vol=has_vol,
                vr=c[i][5] / vavg if vavg else 1, clv=(cl - c[i][3]) / rg if rg > 0 else .5, hv=hv,
                comp=r / atr if atr else 9, pdh=c[i][2], pdl=c[i][3], h3=h, l3=l)

def side_of(f):
    if f["cl"] > f["aw"] and f["cl"] > f["vah"] * .99: return "CE"
    if f["cl"] < f["aw"] and f["cl"] < f["val"] * 1.01: return "PE"
    return None

def trend_ok(f, sd):
    return f["cl"] > f["s20"] > f["s50"] if sd == "CE" else f["cl"] < f["s20"] < f["s50"]

def score(f, sd):
    g = 1 if sd == "CE" else -1
    parts = {"trend": 20 * (g * (f["cl"] - f["s20"]) > 0) + 10 * (g * (f["s20"] - f["s50"]) > 0),
             "volume": min(20, max(0, (f["vr"] - 1) * 20)) if f.get("has_vol", True) else 10,
             "close": 20 * (f["clv"] if sd == "CE" else 1 - f["clv"]),
             "coil": 15 * max(0, min(1, (2.2 - f["comp"]) / 1.2)),
             "avwap": 15 * (g * (f["cl"] - f["aw"]) / f["aw"] * 100 > .5)}
    return sum(parts.values()), parts

def spot_plan(f, sd):
    """Trigger and structural stop, fixed before the session."""
    cl = lambda x, a, b: max(a, min(b, x))
    if sd == "CE":
        trig = max(f["vah"] * 1.005, f["pdh"] * 1.001); sl = trig - cl(trig - f["vah"], .4 * f["atr"], .8 * f["atr"])
    else:
        trig = min(f["val"] * .995, f["pdl"] * .999); sl = trig + cl(f["val"] - trig, .4 * f["atr"], .8 * f["atr"])
    return trig, sl

def targets(entry_spot, sl, sd):
    R = abs(entry_spot - sl); g = 1 if sd == "CE" else -1
    return [entry_spot + g * k * R for k in (1, 2, 3)], R

def build_watchlist(candles, lots, min_score=60, top_per_side=10, trend_filter=True, always=()):
    """`always` (the indices) keep their place even if stocks score higher."""
    out = []
    for s, c in candles.items():
        if s not in lots: continue
        f = daily_features(c)
        if not f or f["cl"] < 50: continue
        sd = side_of(f)
        if not sd or (trend_filter and not trend_ok(f, sd)): continue
        sc, parts = score(f, sd)
        if sc < min_score: continue
        trig, sl = spot_plan(f, sd)
        out.append(dict(sym=s, side=sd, score=round(sc, 1), parts={k: round(v, 1) for k, v in parts.items()}, trig=trig, sl=sl,
                        planR=abs(trig - sl), f=f, lot=lots[s]))
    out.sort(key=lambda x: -x["score"])
    picked = [x for x in out if x["side"] == "CE"][:top_per_side] + [x for x in out if x["side"] == "PE"][:top_per_side]
    ids = {id(x) for x in picked}
    picked += [x for x in out if x["sym"] in always and id(x) not in ids]
    return picked

def exit_plan(lots):
    """How many lots leave at T1, T2, T3."""
    if lots <= 1: return [0, 0, 1]
    if lots == 2: return [1, 0, 1]
    a = lots // 3; return [a, a, lots - 2 * a]

def level_table(candles, lots):
    rows = []
    for s, c in candles.items():
        f = daily_features(c)
        if not f: continue
        sd = side_of(f); r = dict(sym=s, last=f["cl"], vah=f["vah"], val=f["val"], aw=f["aw"], atr=f["atr"], side=sd or "", date=f["date"],
                                  trend=bool(sd and trend_ok(f, sd)), score=round(score(f, sd)[0], 1) if sd else None, lot=lots.get(s))
        if sd:
            trig, sl = spot_plan(f, sd); tg, R = targets(trig, sl, sd); r.update(trig=trig, sl=sl, t=tg)
        rows.append(r)
    return rows
