# Liquidity Vacuum Terminal – Live setup

## What you need
- Windows, Mac or Linux with Python 3.9+ (no extra packages)
- An Angel One account with SmartAPI (free), or a DhanHQ account
- Optional: a Telegram bot for alerts

## 1. Try it without a broker (2 minutes)
Double-click `START_DEMO.bat` (Mac/Linux: `python3 live_bridge.py --demo`).
The browser opens http://localhost:8765 with random test prices, so you can see
how the live badge, triggers and alerts behave.

## 2. Go live with Angel One (recommended)
One-time setup, then it logs in by itself every morning (no daily token paste):
1. **API key**: smartapi.angelbroking.com → sign up with your Angel client ID →
   My Profile → **Create an App** → type *Market Feeds* → copy the **API Key**.
2. **TOTP secret**: smartapi.angelbroking.com/enable-totp → log in with client ID + MPIN +
   OTP → it shows a QR code and a text key. Copy the **text key** (that is ANGEL_TOTP_SECRET).
   You can also scan the QR in Google Authenticator.
3. In `config.env`: BROKER=angel, ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_MPIN, ANGEL_TOTP_SECRET.
4. Run `START_LIVE.bat`. The window should print `Angel One login OK` and `211/211 F&O stocks mapped`.

With Angel, **Load live option chain** shows real LTP, OI and bid/ask per strike (IV column stays blank).

## 2b. Go live with Dhan
1. Log in to web.dhan.co → Profile → Access DhanHQ APIs → generate an access token.
2. Double-click `START_LIVE.bat`. The first time it opens `config.env` in Notepad:
   paste your Client ID and Access Token, save, and run `START_LIVE.bat` again.
3. Keep the black window open during market hours. Browser: http://localhost:8765

On start the bridge:
- downloads missing NSE bhavcopy days, so VAH / VAL / AVWAP are current
- maps all F&O stocks to Dhan security IDs (once a day)
- refreshes live quotes every 3 seconds

In the terminal the top badge shows `LIVE · Dhan · hh:mm:ss`. Best setups cards
change to Triggered / Not triggered / Stop crossed on their own.
In Plan trade, **Load live option chain** fills real LTP, IV and OI for each strike.

## 3. Telegram alerts (optional)
Fill `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `config.env`, restart, then tick
**Telegram alert on trigger** in Best setups. One alert per stock per day,
only while the browser tab stays open.

## Daily routine
- 09:00 start `START_LIVE.bat` (new Dhan token if it expired)
- 09:25 onward watch Best setups; check the real premium in Plan trade before buying
- 15:15 exit remaining positions; after 19:00 the next day's bhavcopy loads on restart

## Troubleshooting
- "Dhan login failed": token expired → generate a new one, paste into config.env.
- Badge says "Bridge not reachable": the black window was closed → start it again.
- A stock shows no live price: Dhan ID not mapped → delete the `cache` folder and restart.
- Holidays: NSE files don't exist on holidays; the bridge skips them.

Not investment advice. Paper-trade first.

---

# Run it 24×7 on GitHub + Render (no PC needed)

Render runs `live_bridge.py` on a cloud server. You open a web link from phone or
laptop, and Telegram alerts keep coming even when every browser is closed.

## 1. Put the folder on GitHub
1. github.com → **New repository** → name `liquidity-vacuum` → choose **Private** → Create.
2. **Add file → Upload files** → drag everything in this folder **except `config.env`**
   (it holds your tokens; `.gitignore` already skips it) → Commit.

## 2. Deploy on Render
1. render.com → sign in with GitHub → **New → Blueprint** → pick the repo.
   Render reads `render.yaml` (Singapore region, Starter plan).
2. It asks for the secret values. Fill in:
   - `APP_PASSWORD` – any strong password; the site asks for it (user name can be anything)
   - `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_MPIN`, `ANGEL_TOTP_SECRET`
     (for Dhan instead: set `BROKER` = `dhan` and add `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`)
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (for alerts)
3. **Apply**. After the build, open the `https://liquidity-vacuum-terminal.onrender.com` link.

## 3. Every day
Angel One: nothing to do, the bridge logs in again each morning with TOTP.
Dhan only: access tokens expire. When the badge shows *Feed error* or Telegram goes quiet:
Render dashboard → service → **Environment** → paste the new `DHAN_ACCESS_TOKEN` → **Save**
(Render restarts it in about a minute).

## Good to know
- **Free plan**: the server sleeps after 15 minutes without visitors and quotes stop. Use
  Starter (about US$7/month), or keep free and ping `https://<your-app>.onrender.com/ping`
  every 10 minutes from cron-job.org during 09:00–15:30.
- **NSE bhavcopy** downloads are sometimes blocked from cloud servers. If the stale-data
  banner stays, upload the files in the Data tab (saved in that browser), or add them locally.
- **Server alerts** (`SERVER_ALERTS=1`) use trend filter on, `MIN_SCORE`, `TOP_PER_SIDE`.
  One alert per stock per day, 09:25–15:15 IST, skipped if stop is already crossed or price
  is past T1. Settings you change in the browser don't change the server's alerts.
- The bridge only **reads** market data. It never places orders.
- Keep the repo private and never commit `config.env`.
