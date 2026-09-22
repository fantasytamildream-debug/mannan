# KRT Options Terminal

Live F&O call terminal for the Liquidity Vacuum strategy. The server makes every call with one
rulebook; the website, Telegram and every viewer see the same calls, with entry time,
T1/T2/T3/SL hit times, live option prices and net P&L.

## Files
| File | What it does |
|---|---|
| `server.py` | Starts everything, web server, NSE end-of-day refresh, Telegram |
| `engine.py` | Call lifecycle: watching → trigger → checks → entry → targets / stop |
| `strategy.py` | The maths: levels, trigger, stop, targets, Black-Scholes, costs |
| `brokers.py` | Angel One SmartAPI, Dhan, and a Demo market |
| `static/index.html` | The website |
| `data/market_data.json` | Daily candles, lot sizes, old backtest rows |

## Try it (no broker)
Windows: double-click `START_DEMO.bat`. Mac/Linux:
`IGNORE_MARKET_HOURS=1 BAR_SECONDS=20 python3 server.py --demo --no-eod`
Prices are simulated and candles are 20 seconds long, so you see entries and targets within minutes.

## Run live on your PC
1. Copy `config.env.example` to `config.env` and fill in Angel One keys (see below).
2. Double-click `START_LIVE.bat` → http://localhost:8765

### Angel One keys
- API key: smartapi.angelbroking.com → My Profile → Create an App (Market Feeds).
- TOTP secret: smartapi.angelbroking.com/enable-totp → the text key under the QR code.
- Client code (e.g. A123456) and your 4-digit MPIN.
The server logs in by itself every morning with TOTP.

## Run 24×7 on GitHub + Render
1. GitHub → New **private** repository → upload every file and folder here except `config.env`
   (keep the `static` and `data` folders).
2. Render → New → **Blueprint** → pick the repo. Fill APP_PASSWORD, Angel keys, Telegram.
3. `render.yaml` uses the Starter plan with a 1 GB disk so today's calls and the performance
   history survive restarts. On the free plan the server sleeps and calls are lost on restart.

## What the checks mean
Entry only at a completed 5-minute candle close beyond the trigger, 09:25–14:30, and only if:
entry not more than 0.5R late, stock volume at normal pace or better, NIFTY not against the trade,
no 3-day high/low before T1, fewer than 4 open calls, and a live option contract with spread ≤ 3%,
≥ 20 lots traded, a fresh quote, and risk ≤ ₹2,500 per trade including costs.
Targets are spot ±1R/2R/3R. Premiums marked EST are estimates; LIVE is a broker price.

Not investment advice. Judge the strategy on the Performance tab after 30+ calls.
