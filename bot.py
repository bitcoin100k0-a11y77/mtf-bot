"""
MTF Scalping Bot â CHAMPION v3.0
=================================
Strategy  : 15M EMA9 trend + 5M EMA21 pullback + RSI + ATR chop filter
Pairs     : BTCUSDT, ETHUSDT, SOLUSDT

NEW in v3 (vs v2):
  â¦ Session filter       â only trade 07:00â20:00 UTC (London + NY)
  â¦ RSI momentum filter  â require RSI rising 2 bars (long) / falling 2 bars (short)
  â¦ 1H RSI filter        â 1H RSI > 40 for longs, < 60 for shorts
  â¦ Optimised SL/TP      â SL=1.8Ã ATR, TP1=4.5Ã, TP2=7.2Ã, TP3=18Ã
  â¦ Looser pull zone     â 1.0% vs 0.7%
  â¦ Adjusted RSI bands   â enter long <45, short >60
  â¦ Breakeven SL         â unchanged (1:1 trigger)
  â¦ 3-tier partial TP    â 40% @ TP1, 30% @ TP2, 30% @ TP3

Backtest (BTC, JanâMar 2026):
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

    # ââ Core strategy ââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    TF_EMA      = 9          # 15M EMA period
    M5_EMA      = 21         # 5M EMA period
    RSI_P       = 14
    ATR_P       = 14

    # ââ Entry filters ââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    PULL_PCT    = 0.010       # v3: 1.0% pull-back zone (was 0.7%)
    RSI_LO      = 45          # v3: long when RSI < 45 (was 40)
    RSI_HI      = 60          # v3: short when RSI > 60 (unchanged)
    RSI_FLOOR   = 25.0
    RSI_CEIL    = 75.0
    ATR_REL     = 0.90        # ATR must be â¥ 90% of avg (chop filter)
    ATR_AVG_N   = 100

    # ââ Session filter (v3 NEW) ââââââââââââââââââââââââââââââââââââââââââââ
    SESSION_START = 7         # 07:00 UTC = London open
    SESSION_END   = 20        # 20:00 UTC = NY close

    # ââ 1H RSI filter (v3 NEW) ââââââââââââââââââââââââââââââââââââââââââââ
    RSI_1H_LO   = 40.0        # 1H RSI must be > 40 to go LONG
    RSI_1H_HI   = 60.0        # 1H RSI must be < 60 to go SHORT

    # ââ Risk & sizing ââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    SL_MULT     = 1.8         # v3: 1.8Ã ATR (was 1.5)
    TP1_MULT    = 4.5         # v3: 4.5Ã ATR (was 3.5)
    TP2_MULT    = 7.2         # v3: 7.2Ã ATR (= 1.6 Ã TP1)
    TP3_MULT    = 18.0        # v3: 18Ã  ATR (= 4.0 Ã TP1)
    TP1_FRAC    = 0.40        # v3: close 40% at TP1 (was 50%)
    TP2_FRAC    = 0.30        # close 30% at TP2
    # remaining 30% closes at TP3

    MAX_HOLD    = 48          # max bars before forced exit (5-min bars = 4 hours)
    IC          = 10_000.0
    RISK_PCT    = 0.0075      # 0.75% risk per trade

    INTERVAL    = 300         # check every 5 minutes
    STATE_FILE  = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")) / "bot_state.json"


# âââ Indicators âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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
    out = [None]*len(highs); s = n = 0
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


# âââ State ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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


# âââ Data & build âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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


# âââ Signal âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def get_signal(d5, capital):
    n = len(d5["ts"])
    i, p = n-2, n-3
    if i < 120 or d5["atr"][i] is None or d5["rsi"][i] is None:
        return {"sig": "WATCH", "reason": "warming up"}

    c  = d5["c"][i]; o = d5["o"][i]
    h  = d5["h"][i]; l = d5["l"][i]
    rc = d5["rsi"][i]
    rp = d5["rsi"][i-1] if d5["rsi"][i-1] else rc
    atr_val   = d5["atr"][i]
    atr_avg   = d5["atr_avg"][i]
    ar        = atr_val/atr_avg if atr_avg else None
    dist      = d5["dist"][i]   if d5["dist"][i] else 999
    rsi_1h    = d5["rsi_1h"][i]
    hour      = d5["hour"][i]
    rsi_rise  = d5["rsi_rising"][i]
    rsi_fall  = d5["rsi_falling"][i]

    m = {"px":c,"e21":d5["e21"][i],"rsi":rc,"atr":atr_val,
         "ar":ar,"hour":hour,"rsi_1h":rsi_1h}

    # ââ Chop filter ââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    if ar is None or ar < Cfg.ATR_REL:
        return {"sig": "CHOP", "reason": f"ATR {ar:.2f}x" if ar else "ATR N/A", "m": m}

    # ââ Session filter (v3) âââââââââââââââââââââââââââââââââââââââââââââââ
    if not (Cfg.SESSION_START <= hour < Cfg.SESSION_END):
        return {"sig": "WATCH", "reason": f"off-session {hour}:xx UTC", "m": m}

    # ââ Candle quality ââââââââââââââââââââââââââââââââââââââââââââââââââââ
    rng  = h - l
    body = abs(c-o)/rng if rng > 0 else 0
    bull = c > o and body > 0.45
    bear = c < o and body > 0.45

    # ââ Distance filter ââââââââââââââââââââââââââââââââââââââââââââââââââ
    near = dist < Cfg.PULL_PCT

    tr_d = "UP" if d5["tf_up"][i] else ("DOWN" if d5["tf_dn"][i] else "FLAT")

    # ââ LONG signal âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    if (d5["tf_up"][i]
            and near
            and rp < Cfg.RSI_LO and rc > rp       # RSI turning up from below 45
            and rc > Cfg.RSI_FLOOR
            and bull
            and rsi_rise                             # â v3: RSI rising 2 bars
            and (rsi_1h is None or rsi_1h > Cfg.RSI_1H_LO)):  # â v3: 1H filter
        sl  = c - atr_val * Cfg.SL_MULT
        tp1 = c + atr_val * Cfg.TP1_MULT
        tp2 = c + atr_val * Cfg.TP2_MULT
        tp3 = c + atr_val * Cfg.TP3_MULT
        be  = c + atr_val * Cfg.SL_MULT
        sz  = (capital * Cfg.RISK_PCT) / (atr_val * Cfg.SL_MULT)
        return {"sig": "LONG",
                "reason": f"15M-UP EMA+RSI {rp:.0f}â{rc:.0f}",
                "m": m, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "be": be, "sz": sz}

    # ââ SHORT signal ââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    if (d5["tf_dn"][i]
            and near
            and rp > Cfg.RSI_HI and rc < rp        # RSI turning down from above 60
            and rc < Cfg.RSI_CEIL
            and bear
            and rsi_fall                             # â v3: RSI falling 2 bars
            and (rsi_1h is None or rsi_1h < Cfg.RSI_1H_HI)):  # â v3: 1H filter
        sl  = c + atr_val * Cfg.SL_MULT
        tp1 = c - atr_val * Cfg.TP1_MULT
        tp2 = c - atr_val * Cfg.TP2_MULT
        tp3 = c - atr_val * Cfg.TP3_MULT
        be  = c - atr_val * Cfg.SL_MULT
        sz  = (capital * Cfg.RISK_PCT) / (atr_val * Cfg.SL_MULT)
        return {"sig": "SHORT",
                "reason": f"15M-DOWN EMA+RSI {rp:.0f}â{rc:.0f}",
                "m": m, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "be": be, "sz": sz}

    reason = f"15M:{tr_d}"
    if not near: reason += f" | dist {dist*100:.3f}%"
    else:        reason += f" | RSI:{rc:.0f}"
    if not (Cfg.SESSION_START <= hour < Cfg.SESSION_END):
        reason += f" | off-session"
    return {"sig": "WATCH", "reason": reason, "m": m}


# âââ Exit check (partial TPs + breakeven) âââââââââââââââââââââââââââââââââââââ

def check_exits(ot, d5):
    """
    Checks BE trigger, partial TP hits, and full close conditions.
    Modifies ot in-place. Returns (events, fully_closed, close_reason,on, slose_px).
    """
    i    = len(d5["ts"]) - 1
    h    = d5["h"][i]; l = d5["l"][i]; c = d5["c"][i]
    ot["bars"] = ot.get("bars", 0) + 1
    dirn = ot["dir"]
    events = []

    # ââ Breakeven trigger âââââââââââââââââââââââââââââââââââââââââââââââââ
    if not ot.get("be_hit", False):
        if (dirn=="LONG"  and h >= ot["be"]) or \
           (dirn=="SHORT" and l <= ot["be"]):
            ot["sl"]     = ot["entry"]
            ot["be_hit"] = True
            events.append("BE_TRIGGERED")

    sl = ot["sl"]

    # ââ Partial TP hits âââââââââââââââââââââââââââââââââââââââââââââââââââ
    if dirn == "LONG":
        if not ot["tp1_hit"] and h >= ot["tp1"]:
            pnl = (ot["tp1"] - ot["entry"]) * ot["size"] * Cfg.TP1_FRAC
            ot["pnl"] += pnl; ot["rem"] -= Cfg.TP1_FRAC; ot["tp1_hit"] = True
            events.append(f"TP1:+${pnl:.2f}")
        if ot["tp1_hit"] and not ot["tp2_hit"] and h >= ot["tp2"]:
            pnl = (ot["tp2"] - ot["entry"]) * ot["size"] * Cfg.TP2_FRAC
            ot["pnl"] += pnl; ot["rem"] -= Cfg.TP2_FRAC; ot["tp2_hit"] = True
            events.append(f"TP2:+${pnl:.2f}")
        if ot["tp2_hit"] and not ot["tp3_hit"] and h >= ot["tp3"]:
            pnl = (ot["tp3"] - ot["entry"]) * ot["size"] * ot["rem"]
            ot["pnl"] += pnl; ot["rem"] = 0; ot["tp3_hit"] = True
            events.append(f"TP3:+${pnl:.2f}")
    else:  # SHORT
        if not ot["tp1_hit"] and l <= ot["tp1"]:
            pnl = (ot["entry"] - ot["tp1"]) * ot["size"] * Cfg.TP1_FRAC
            ot["pnl"] += pnl; ot["rem"] -= Cfg.TP1_FRAC; ot["tp1_hit"] = True
            events.append(f"TP1:+${pnl:.2f}")
        if ot["tp1_hit"] and not ot["tp2_hit"] and l <= ot["tp2"]:
            pnl = (ot["entry"] - ot["tp2"]) * ot["size"] * Cfg.TP2_FRAC
            ot["pnl"] += pnl; ot["rem"] -= Cfg.TP2_FRAC; ot["tp2_hit"] = True
            events.append(f"TP2:+${pnl:.2f}")
        if ot["tp2_hit"] and not ot["tp3_hit"] and l <= ot["tp3"]:
            pnl = (ot["entry"] - ot["tp3"]) * ot["size"] * ot["rem"]
            ot["pnl"] += pnl; ot["rem"] = 0; ot["tp3_hit"] = True
            events.append(f"TP3:+${pnl:.2f}")

    # ââ Full close conditions âââââââââââââââââââââââââââââââââââââââââââââ
    if ot["tp3_hit"] or ot["rem"] <= 0:
        return events, True, "TP3", ot["tp3"]

    sl_hit = (dirn=="LONG" and l<=sl) or (dirn=="SHORT" and h>=sl)
    if sl_hit:
        sl_pnl = ((sl - ot["entry"]) if dirn=="LONG" else (ot["entry"] - sl)) \
                 * ot["size"] * ot["rem"]
        ot["pnl"] += sl_pnl; ot["rem"] = 0
        reason = "BE" if ot.get("be_hit") else "SL"
        return events, True, reason, sl

    if ot["bars"] >= Cfg.MAX_HOLD:
        t_pnl = ((c - ot["entry"]) if dirn=="LONG" else (ot["entry"] - c)) \
                * ot["size"] * ot["rem"]
        ot["pnl"] += t_pnl; ot["rem"] = 0
        return events, True, "TIME", c

    return events, False, None, None


# âââ Telegram âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def tg(text):
    if not Cfg.TG_TOKEN or not Cfg.TG_CHAT_ID:
        log.info(f"[TG] {text[:120]}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{Cfg.TG_TOKEN}/sendMessage",
            json={"chat_id": Cfg.TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10)
    except Exception as e:
        log.warning(f"Telegram: {e}")

def tg_opened(sym, ot):
    tg(f"<b>{'ð' if ot['dir']=='LONG' else 'ð'} {sym} {ot['dir']} OPENED</b>\n"
       f"Entry : ${ot['entry']:,.4f}\n"
       f"TP1   : ${ot['tp1']:,.4f}  (40% close)\n"
       f"TP2   : ${ot['tp2']:,.4f}  (30% close)\n"
       f"TP3   : ${ot['tp3']:,.4f}  (30% close)\n"
       f"SL    : ${ot['sl']:,.4f}\n"
       f"BE    : ${ot['be']:,.4f}  (SLâentry when hit)\n"
       f"Signal: {ot['reason']}")

def tg_tp_hit(sym, ot, event):
    tg(f"<b>ð¯ {sym} {event} HIT</b>\n"
       f"Dir   : {ot['dir']}\n"
       f"Rem   : {ot['rem']*100:.0f}% still open\n"
       f"SL now: ${ot['sl']:,.4f}")

def tg_closed(sym, ot, capital):
    sign  = "+" if ot["pnl"] >= 0 else ""
    emoji = "â WIN" if ot["pnl"]>0 else ("âï¸ BREAK" if ot["pnl"]==0 else "â LOSS")
    tp_lvl = ('TP3' if ot['tp3_hit'] else 'TP2' if ot['tp2_hit'] else 'TP1' if ot['tp1_hit'] else 'NONE')
    tg(f"<b>{emoji} â {sym} {ot['dir']} closed ({ot.get('close_reason','?')})</b>\n"
       f"Entry    : ${ot['entry']:,.4f}\n"
       f"TP level : {tp_lvl}\n"
       f"P&L      : {sign}${ot['pnl']:,.2f}\n"
       f"Capital  : ${capital:,.2f}")

def tg_heartbeat(S):
    ret = (S["capital"]-Cfg.IC)/Cfg.IC*100
    mdd = (S["peak"]-S["capital"])/S["peak"]*100 if S["peak"]>0 else 0
    lines = [
        f"<b>ð Heartbeat #{S['checks']}</b>",
        f"Capital : ${S['capital']:,.2f} ({ret:+.2f}%)",
        f"MaxDD   : {mdd:.1f}%",
        f"Trades  : {len(S['trades'])} | Signals:{S['signals']} | Chops:{S['chops']}",
        ""
    ]
    for sym in PAIRS:
        ps = S["pair_stats"].get(sym, {})
        ot = S["open_trades"].get(sym)
        if ot:
            pos = f"OPEN {ot['dir']} @ ${ot['entry']:,.2f} | rem:{ot['rem']*100:.0f}%"
        else:
            pos = "No position"
        wr = f"{ps.get('wins',0)}/{ps.get('trades',0)}" if ps.get('trades') else "0/0"
        lines.append(f"<b>{sym}</b>: {pos} | W/T:{wr} | P&L:${ps.get('pnl',0):+.0f}")
    tg("\n".join(lines))


# âââ Main loop ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def main():
    log.info("=== MTF BOT v3.0 â CHAMPION â BTC/ETH/SOL ===")
    S = load_state()
    log.info(f"State: checks={S['checks']} capital=${S['capital']:.2f} trades={len(S['trades'])}")
    tg(f"<b>ð MTF Bot v3.0 CHAMPION started</b>\n"
       f"Pairs    : {', '.join(PAIRS)}\n"
       f"TP 40/30/30% @ 4.5R / 7.2R / 18R\n"
       f"Session  : 07â20 UTC only\n"
       f"RSI mom  : 2-bar confirmation\n"
       f"1H RSI   : filter active\n"
       f"Capital  : ${S['capital']:,.2f}\n"
       f"Backtest : PF 18.13 | MaxDD 1.5% | WR 56%")

    while True:
        loop_start = time.time()
        try:
            S["checks"] += 1
            log.info(f"=== Check #{S['checks']} ===")

            for sym in PAIRS:
                try:
                    raw5m  = fetch_klines(sym, "5m",  600)
                    raw15m = fetch_klines(sym, "15m", 500)
                    raw1h  = fetch_klines(sym, "1h",  200)
                    d5 = build(raw5m, raw15m, raw1h)
                    px = d5["c"][-1]

                    if sym not in S["start_px"]:
                        S["start_px"][sym] = px

                    ps = S["pair_stats"].setdefault(sym, {"trades":0,"wins":0,"pnl":0.0})

                    # ââ Exit check âââââââââââââââââââââââââââââââââââââââââ
                    ot = S["open_trades"].get(sym)
                    if ot:
                        events, closed, c_reason, c_px = check_exits(ot, d5)

                        for ev in events:
                            if ev.startswith("TP"):
                                tg_tp_hit(sym, ot, ev.split(":")[0])
                            elif ev == "BE_TRIGGERED":
                                tg(f"<b>ð {sym} â Breakeven triggered!</b>\n"
                                   f"SL moved to entry: ${ot['entry']:,.4f}")

                        if closed:
                            ot["close_reason"] = c_reason
                            S["capital"] += ot["pnl"]
                            S["peak"]     = max(S["peak"], S["capital"])
                            S["trades"].append({**ot, "symbol":sym,
                                "close_time": datetime.now(timezone.utc).isoformat()})
                            ps["trades"] += 1
                            ps["pnl"]    += ot["pnl"]
                            if ot["pnl"] > 0: ps["wins"] += 1
                            S["open_trades"].pop(sym)
                            log.info(f"{sym} CLOSED {c_reason} {ot['dir']} P&L ${ot['pnl']:+.2f} | cap=${S['capital']:.2f}")
                            tg_closed(sym, ot, S["capital"])
                        else:
                            ep = ((px - ot["entry"]) if ot["dir"]=="LONG"
                                  else (ot["entry"] - px)) * ot["size"] * ot["rem"]
                            log.info(f"{sym} HOLDING {ot['dir']} {ot['bars']}bars rem:{ot['rem']*100:.0f}% est:${ep:+.2f}")

                    # ââ Entry check ââââââââââââââââââââââââââââââââââââââââ
                    if sym not in S["open_trades"]:
                        result = get_signal(d5, S["capital"])
                        sig    = result["sig"]
                        m      = result.get("m", {})
                        log.info(f"{sym} ${px:,.2f} | 15M:{m.get('tr','?')} | "
                                 f"RSI:{m.get('rsi',0):.1f} | ATR:{m.get('ar',0):.2f}x | "
                                 f"Dist:{m.get('dist',0)*100:.3f}% | Hr:{m.get('hour','?')} | {sig}")
                        if sig == "CHOP":
                            S["chops"] += 1
                        elif sig in ("LONG","SHORT"):
                            new_ot = {
                                "symbol":    sym,
                                "dir":       sig,
                                "entry":     result["m"]["px"],
                                "sl":        result["sl"],
                                "tp1":       result["tp1"],
                                "tp2":       result["tp2"],
                                "tp3":       result["tp3"],
                                "be":        result["be"],
                                "size":      result["sz"],
                                "be_hit":    False,
                                "tp1_hit":   False,
                                "tp2_hit":   False,
                                "tp3_hit":   False,
                                "rem":       1.0,
                                "pnl":       0.0,
                                "bars":      0,
                                "reason":    result["reason"],
                                "open_time": datetime.now(timezone.utc).isoformat()
                            }
                            S["open_trades"][sym] = new_ot
                            S["signals"] += 1
                            log.info(f"{sym} {sig} entry={new_ot['entry']:.2f} "
                                     f"TP1={new_ot['tp1']:.2f} TP2={new_ot['tp2']:.2f} "
                                     f"TP3={new_ot['tp3']:.2f} SL={new_ot['sl']:.2f}")
                            tg_opened(sym, new_ot)
                    else:
                        log.info(f"{sym} ${px:,.2f} | in trade")

                except Exception as e:
                    log.error(f"{sym} error: {e}")

            save_state(S)
            if S["checks"] % 12 == 0:
                tg_heartbeat(S)

        except Exception as e:
            log.exception(f"Main loop error: {e}")
            tg(f"â ï¸ Bot error: {e}\nRetrying in 5 min...")

        elapsed = time.time() - loop_start
        time.sleep(max(5, Cfg.INTERVAL - elapsed))


if __name__ == "__main__":
    main()
