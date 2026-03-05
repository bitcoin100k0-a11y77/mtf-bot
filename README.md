# 🤖 MTF Scalping Bot — Railway Cloud Setup

**Strategy**: 30M EMA9 trend + 5M EMA21 pullback + RSI bounce + ATR chop filter  
**Data**: Binance public API (no account or key needed)  
**Alerts**: Telegram (free)

---

## Step 1 — Get your Telegram Bot Token (5 min)

1. Open Telegram, search for **@BotFather**
2. Send `/newbot`
3. Give it a name (e.g. `MTF Scalping Bot`)
4. Give it a username (e.g. `my_mtf_bot`)
5. BotFather sends you a token like: `7123456789:AAFxxxxxxxxxxxxxxxxxxxxx`  
   → **Copy this token**

6. Now get your **Chat ID**:
   - Search for **@userinfobot** on Telegram
   - Send `/start`
   - It replies with your ID like `Id: 123456789`  
   → **Copy this number**

---

## Step 2 — Push to GitHub (3 min)

1. Create a free account at [github.com](https://github.com)
2. Click **New Repository** → name it `mtf-bot` → **Create**
3. Upload these 3 files:
   - `bot.py`
   - `requirements.txt`
   - `railway.toml`

---

## Step 3 — Deploy on Railway (3 min)

1. Go to [railway.app](https://railway.app) → **Login with GitHub**
2. Click **New Project** → **Deploy from GitHub repo**
3. Select your `mtf-bot` repo
4. Railway auto-detects Python and starts building

---

## Step 4 — Add Environment Variables

In Railway dashboard → your project → **Variables** tab, add:

| Variable | Value |
|---|---|
| `TELEGRAM_TOKEN` | Your BotFather token |
| `TELEGRAM_CHAT_ID` | Your chat ID number |

Click **Save** — bot restarts automatically with the new vars.

---

## Step 5 — Add Persistent Volume (saves trade state)

1. In Railway → your project → **+ Add Service** → **Volume**
2. Mount path: `/data`
3. Done — bot state survives restarts

---

## You're Live! 🚀

The bot will:
- ✅ Check Binance every **5 minutes**
- ✅ Send Telegram alert when a **trade opens**
- ✅ Send Telegram alert when a **trade closes** (TP/SL/TIME)
- ✅ Send **hourly heartbeat** with capital status
- ✅ **Auto-restart** if it crashes

---

## Telegram Alert Examples

**Trade opened:**
```
🟢 LONG OPENED
Entry  : $72,450
TP     : $72,870
SL     : $72,095
Size   : 0.0021 BTC
Reason : 30M↑EMA9 + EMA21 + RSI 38→42
```

**Trade closed:**
```
✅ TP — LONG CLOSED
Entry  : $72,450
Exit   : $72,870
P&L    : +$63.18
Capital: $10,063.18
```

**Hourly heartbeat:**
```
💓 Heartbeat #12
BTC    : $72,650
30M    : UP
Capital: $10,063.18 (+0.63%)
MaxDD  : 0.8%
Trades : 1 | Signals: 1 | Chops: 4
```

---

## Strategy Config (edit in bot.py)

```python
TF_EMA    = 9       # Trend EMA period on 30M
M5_EMA    = 21      # Pullback EMA on 5M
SL_MULT   = 1.5     # Stop = 1.5 × ATR
TP_MULT   = 3.5     # Target = 3.5 × ATR
RISK_PCT  = 0.0075  # 0.75% risk per trade
ATR_REL   = 0.90    # Chop filter threshold
```

---

## Railway Free Tier

- **500 hours/month** free (enough for ~20 days)
- For unlimited: upgrade to Hobby plan ($5/month)
- Or use **Render.com** free tier (always-on with 750 hrs/month)
