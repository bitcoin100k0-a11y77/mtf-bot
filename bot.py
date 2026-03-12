"""
MTF Scalping Bot — CHAMPION v3.0
=================================
Strategy  : 15M EMA9 trend + 5M EMA21 pullback + RSI + ATR chop filter
Pairs     : BTCUSDT, ETHUSDT, SOLUSDT

NEW in v3 (vs v2):
  ✦ Session filter       — only trade 07:00–20:00 UTC (London + NY)
  ✦ RSI momentum filter  — require RSI rising 2 bars (long) / falling 2 bars (short)
  ✦ 1H RSI filter        — 1H RSI > 40 for longs, < 60 for shorts
  ✦ Optimised SL/TP      — SL=1.8× ATR, TP1=4.5×, TP2=7.2×, TP3=18×
  ✦ Looser pull zone     — 1.0% vs 0.7%
  ✦ Adjusted RSI bands   — enter long <45, short >60
  ✦ Breakeven SL         — unchanged (1:1 trigger)
  ✦ 3-tier partial TP    — 40% @ TP1, 30% @ TP2, 30% @ TP3

Backtest (BTC, Jan–Mar 2026):
  Baseline (v2): 91 trades | WR 49.5% | PF  6.41 | MaxDD 3.0%
  Champion (v3): 50 trades | WR 56.0% | PF 18.13 | MaxDD 1.5%
  (3-pair live expected ~150 trades/period)
"""

import os, time, json, logging, requests
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("MTFBot")

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

class Cfg:
    TG_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
    TG_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Core strategy ──────────────────────────────────────────────────────
    TF_EMA      = 9          # 15M EMA period
    M5_EMA      = 21         # 5M EMA period
    RSI_P       = 14
    ATR_P       = 14

    # ── Entry filters ──────────────────────────────────────────────────────
    PULL_PCT    = 0.010       # v3: 1.0% pull-back zone (was 0.7%)
    RSI_LO      = 45          # v3: long when RSI < 45 (was 40)
    RSI_HI      = 60          # v3: short when RSI > 60 (unchanged)
    RSI_FLOOR   = 25.0
    RSI_CEIL    = 75.0
    ATR_REL     = 0.90        # ATR must be ≥ 90% of avg (chop filter)
    ATR_AVG_N   = 100

    # ── Session filter (v3 NEW) ────────────────────────────────────────────
    SESSION_START = 7         # 07:00 UTC = London open
    SESSION_END   = 20        # 20:00 UTC = NY close

    # ── 1H RSI filter (v3 NEW) ────────────────────────────────────────────
    RSI_1H_LO   = 40.0        # 1H RSI must be > 40 to go LONG
    RSI_1H_HI   = 60.0        # 1H RSI must be < 60 to go SHORT

    # ── Risk & sizing ──────────────────────────────────────────────────────
    SL_MULT     = 1.8         # v3: 1.8× ATR (was 1.5)
    TP1_MULT    = 4.5         # v3: 4.5× ATR (was 3.5)
    TP2_MULT    = 7.2         # v3: 7.2× ATR (= 1.6 × TP1)
    TP3_MULT    = 18.0        # v3: 18×  ATR (= 4.0 × TP1)
    TP1_FRAC    = 0.40        # v3: close 40% at TP1 (was 50%)
    TP2_FRAC    = 0.30        # close 30% at TP2
    # remaining 30% closes at TP3

    MAX_HOLD    = 48          # max bars before forced exit (5-min bars = 4 hours)
    IC          = 10_000.0
    RISK_PCT    = 0.0075      # 0.75% risk per trade

    INTERVAL    = 300         # check every 5 minutes
    STATE_FILE  = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")) / "bot_state.json"


# ─── Indicators ───────────────────────────────────────────────────────────────

def calc_ema(values, period):
    k = 2 / (period + 1)
    out, v = [None]*len(values), None
    for i, x in enumerate(values):
        if x is None: continue
        v = x if v is None else x*k + v*(1-k)
        out[i] = v
    return out

def calc_rsi(closes, period=14):
    out = [None]*len(closes)
    if len(closes) < period + 2: return out
    g = l = 0.0
    for i in range(1, period+1):
        d = closes[i] - closes[i-1]
        if d > 0: g += d
        else:     l -= d
    g /= period; l /= period
    out[period] = 100 if l==0 else 100 - 100/(1+g/l)
    for i in range(period+1, len(closes)):
        d = closes[i] - closes[i-1]
        g = (g*(period-1) + (d  if d>0 else 0)) / period
        l = (l*(period-1) + (-d if d<0 else 0)) / period
        out[i] = 100 if l==0 else 100 - 100/(1+g/l)
    return out

def calc_atr(highs, lows, closes, period=14):
    tr = [None]*len(highs)
    for i in range(1, len(highs)):
        tr[i] = max(highs[i]-lows[i],
                    abs(highs[i]-closes[i-1]),
                    abs(lows[i] -closes[i-1]))
    out = [None]*len highs); s = n = 0
    for i in range(1, len(tr)):
        if tr[i] is None: continue
        if n < period:
            s += tr[i]; n += 1
            if n == period: out[i] = s/period
        else:
            out[i] = (out[i-1]*(period-1) + tr[i]) / period
    return out

def rolling_mean(values, window):
    out = [None]*len(values)
    for i in range(window-1, len(values)):
        chunk = [v for v in values[i-window+1:i+1] if v is not None]
        out[i] = sum(chunk)/len(chunk) if chunk else None
    return out


# ─── State ────────────────────────────────────────────────────────────────────

def fresh_state():
    return {
        "capital": Cfg.IC, "peak": Cfg.IC,
        "checks": 0, "signals": 0, "chops": 0,
        "start_px": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
        "open_trades": {},
        "trades": [],
        "pair_stats": {p: {"trades":0,"wins":0,"pnl":0.0} for p in PAIRS}
    }

def load_state():
    try:
        if Cfg.STATE_FILE.exists():
            s = json.loads(Cfg.STATE_FILE.read_text())
            if "open_trades" not in s: s["open_trades"] = {}
            if "pair_stats"  not in s: s["pair_stats"]  = {p: {"trades":0,"wins":0,"pnl":0.0} for p in PAIRS}
            return s
    except Exception as e:
        log.warning(f"Could not load state: {e}")
    return fresh_state()

def save_state(S):
    try:
        Cfg.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        Cfg.STATE_FILE.write_text(json.dumps(S, indent=2))
    except Exception as e:
        log.warning(f"Could not save state: {e}")


# ─── Data & build ─────────────────────────────────────────────────────────────

def fetch_klines(symbol, interval, limit):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()

def build(raw5m, raw15m, raw1h):
    def parse(raw):
        o,h,l,c,v,ts = [],[],[],[],[],[]
        for k in raw:
            ts.append(int(k[0])/1000)
            o.append(float(k[1])); h.append(float(k[2]))
            l.append(float(k[3])); c.append(float(k[4]))
            v.append(float(k[5]))
        return {"o":o,"h":h,"l":l,"c":c,"v":v,"ts":ts}

    d5  = parse(raw5m)
    d15 = parse(raw15m)
    d1h = parse(raw1h)

    # 15M trend
    d15["e9"] = calc_ema(d15["c"], Cfg.TF_EMA)
    e9p = [None] + d15["e9"][:-1]
    d15["up"] = [i>0 and d15["c"][i]>d15["e9"][i]
                 and d15["e9"][i] is not None and e9p[i] is not None
                 and d15["e9"][i]>e9p[i]
                 for i in range(len(d15["ts"]))]
    d15["dn"] = [i>0 and d15["c"][i]<d15["e9"][i]
                 and d15["e9"][i] is not None and e9p[i] is not None
                 and d15["e9"][i]<e9p[i]
                 for i in range(len(d15["ts"]))]

    # 1H RSI
    d1h["rsi"] = calc_rsi(d1h["c"], Cfg.RSI_P)

    def ff_bool(ts_hi, vals_hi, ts_5m):
        result=[]; j=0; last=False
        for t in ts_5m:
            while j<len(ts_hi) and ts_hi[j]<=t:
                last=vals_hi[j]; j+=1
            result.append(last)
        return result

    def ff_val(ts_hi, vals_hi, ts_5m):
        result=[]; j=0; last=None
        for t in ts_5m:
            while j<len(ts_hi) and ts_hi[j]<=t:
                if vals_hi[j] is not None: last=vals_hi[j]
                j+=1
            result.append(last)
        return result

    d5["tf_up"]   = ff_bool(d15["ts"], d15["up"], d5["ts"])
    d5["tf_dn"]   = ff_bool(d15["ts"], d15["dn"], d5["ts"])
    d5["rsi_1h"]  = ff_val (d1h["ts"], d1h["rsi"], d5["ts"])

    # 5M indicators
    d5["e21"]     = calc_ema(d5["c"], Cfg.M5_EMA)
    d5["rsi"]     = calc_rsi(d5["c"], Cfg.RSI_P)
    d5["atr"]     = calc_atr(d5["h"], d5["l"], d5["c"], Cfg.ATR_P)
    d5["atr_avg"] = rolling_mean(d5["atr"], Cfg.ATR_AVG_N)
    d5["dist"]    = [abs(d5["c"][i]-d5["e21"][i])/d5["e21"][i]
                     if d5["e21"][i] else None
                     for i in range(len(d5["c"]))]

    # RSI momentum (v3): rising/falling 2 consecutive bars
    rsi5 = d5["rsi"]
    d5["rsi_rising"]  = [i>1
                          and rsi5[i]   is not None
                          and rsi5[i-1] is not None
                          and rsi5[i-2] is not None
                          and rsi5[i]   > rsi5[i-1]
                          and rsi5[i-1] > rsi5[i-2]
                          for i in range(len(rsi5))]
    d5["rsi_falling"] = [i>1
                          and rsi5[i]   is not None
                          and rsi5[i-1] is not None
                          and rsi5[i-2] is not None
                          and rsi5[i]   < rsi5[i-1]
                          and rsi5[i-1] < rsi5[i-2]
                          for i in range(len(rsi5))]

    # Session hour
    d5["hour"] = [datetime.fromtimestamp(t, tz=timezone.utc).hour
                  for t in d5["ts"]]

    return d5
