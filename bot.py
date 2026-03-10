"""
MTF Scalping Bot - Railway Cloud Deployment
Pairs    : BTCUSDT, ETHUSDT, SOLUSDT
Strategy : 15M EMA9 trend + 5M EMA21 pullback + RSI + ATR chop filter
TP System: 3-tier partial take profit
             TP1 = ATR×3.5  → close 50%
             TP2 = ATR×7.0  → close 30%  (2× TP1)
             TP3 = ATR×14.0 → close 20%  (4× TP1)
Breakeven: SL moves to entry once price moves 1:1 in favour
Backtest : 91 trades | 49.5% WR | PF 6.41 | MaxDD 3.0% | +94.6%
"""

import os, time, json, logging, requests
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("MTFBot")

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

class Cfg:
    TG_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    TF_EMA     = 9
    M5_EMA     = 21
    RSI_P      = 14
    ATR_P      = 14
    PULL_PCT   = 0.007
    SL_MULT    = 1.5
    TP1_MULT   = 3.5          # 1st TP
    TP2_MULT   = 3.5 * 2      # 2nd TP = 2× first
    TP3_MULT   = 3.5 * 4      # 3rd TP = 4× first
    TP1_FRAC   = 0.50         # close 50% at TP1
    TP2_FRAC   = 0.30         # close 30% at TP2
    # remaining 20% closes at TP3
    MAX_HOLD   = 48
    RSI_LO     = 40.0
    RSI_HI     = 60.0
    RSI_FLOOR  = 25.0
    RSI_CEIL   = 75.0
    ATR_REL    = 0.90
    ATR_AVG_N  = 100
    IC         = 10_000.0
    RISK_PCT   = 0.0075
    INTERVAL   = 300
    STATE_FILE = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")) / "bot_state.json"

def binance_url(symbol, interval, limit):
    return f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"

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
            if "open_trades" not in s:  s["open_trades"] = {}
            if "pair_stats"  not in s:  s["pair_stats"]  = {p: {"trades":0,"wins":0,"pnl":0.0} for p in PAIRS}
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

# ── Indicators ─────────────────────────────────────────────────────────────
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
    tr_l = [None]*len(highs)
    out  = [None]*len(highs)
    for i in range(1, len(highs)):
        tr_l[i] = max(highs[i]-lows[i],
                      abs(highs[i]-closes[i-1]),
                      abs(lows[i] -closes[i-1]))
    s = n = 0
    for i in range(1, len(tr_l)):
        if tr_l[i] is None: continue
        if n < period:
            s += tr_l[i]; n += 1
            if n == period: out[i] = s/period
        else:
            out[i] = (out[i-1]*(period-1) + tr_l[i]) / period
    return out

def rolling_mean(values, window):
    out = [None]*len(values)
    for i in range(window-1, len(values)):
        chunk = [v for v in values[i-window+1:i+1] if v is not None]
        out[i] = sum(chunk)/len(chunk) if chunk else None
    return out

# ── Data fetch & build ─────────────────────────────────────────────────────
def fetch_klines(url):
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()

def build(raw5m, raw15m):
    def parse(raw):
        o,h,l,c,ts = [],[],[],[],[]
        for k in raw:
            ts.append(int(k[0])/1000)
            o.append(float(k[1])); h.append(float(k[2]))
            l.append(float(k[3])); c.append(float(k[4]))
        return {"o":o,"h":h,"l":l,"c":c,"ts":ts}

    d5  = parse(raw5m)
    d15 = parse(raw15m)

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

    def ff(vals):
        result,j,last = [],0,None
        for t in d5["ts"]:
            while j<len(d15["ts"]) and d15["ts"][j]<=t:
                last=vals[j]; j+=1
            result.append(last)
        return result

    d5["tf_up"] = ff(d15["up"])
    d5["tf_dn"] = ff(d15["dn"])
    d5["e21"]   = calc_ema(d5["c"], Cfg.M5_EMA)
    d5["rsi"]   = calc_rsi(d5["c"], Cfg.RSI_P)
    d5["atr"]   = calc_atr(d5["h"], d5["l"], d5["c"], Cfg.ATR_P)
    d5["atr_a"] = rolling_mean(d5["atr"], Cfg.ATR_AVG_N)
    d5["dist"]  = [abs(d5["c"][i]-d5["e21"][i])/d5["e21"][i]
                   if d5["e21"][i] else None
                   for i in range(len(d5["c"]))]
    return d5

# ── Signal ─────────────────────────────────────────────────────────────────
def get_signal(d5, capital):
    n = len(d5["ts"])
    i,p = n-2, n-3
    if i<60 or d5["atr"][i] is None or d5["rsi"][i] is None:
        return {"sig":"WATCH","reason":"warming up"}

    c = d5["c"][i]; o = d5["o"][i]
    h = d5["h"][i]; l = d5["l"][i]
    rc   = d5["rsi"][i]
    rp   = d5["rsi"][p] if d5["rsi"][p] else rc
    atr  = d5["atr"][i]
    ar   = atr/d5["atr_a"][i] if d5["atr_a"][i] else None
    dist = d5["dist"][i] if d5["dist"][i] else 999

    near = dist < Cfg.PULL_PCT
    rng  = h - l
    body = abs(c-o)/rng if rng>0 else 0
    bull = c>o and body>0.45
    bear = c<o and body>0.45
    tr_d = "UP" if d5["tf_up"][i] else ("DOWN" if d5["tf_dn"][i] else "FLAT")

    m = {"px":c,"e21":d5["e21"][i],"rsi":rc,"atr":atr,"ar":ar,"tr":tr_d,"near":near,"dist":dist}

    if ar is not None and ar < Cfg.ATR_REL:
        return {"sig":"CHOP","reason":f"ATR {ar:.2f}x","m":m}

    if d5["tf_up"][i] and near and rp<Cfg.RSI_LO and rc>rp and rc>Cfg.RSI_FLOOR and bull:
        sl  = c - atr*Cfg.SL_MULT
        tp1 = c + atr*Cfg.TP1_MULT
        tp2 = c + atr*Cfg.TP2_MULT
        tp3 = c + atr*Cfg.TP3_MULT
        be  = c + atr*Cfg.SL_MULT      # 1:1 trigger
        sz  = (capital*Cfg.RISK_PCT)/(atr*Cfg.SL_MULT)
        return {"sig":"LONG","reason":f"15M-UP EMA+RSI {rp:.0f}→{rc:.0f}",
                "m":m,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"be":be,"sz":sz}

    if d5["tf_dn"][i] and near and rp>Cfg.RSI_HI and rc<rp and rc<Cfg.RSI_CEIL and bear:
        sl  = c + atr*Cfg.SL_MULT
        tp1 = c - atr*Cfg.TP1_MULT
        tp2 = c - atr*Cfg.TP2_MULT
        tp3 = c - atr*Cfg.TP3_MULT
        be  = c - atr*Cfg.SL_MULT
        sz  = (capital*Cfg.RISK_PCT)/(atr*Cfg.SL_MULT)
        return {"sig":"SHORT","reason":f"15M-DOWN EMA+RSI {rp:.0f}→{rc:.0f}",
                "m":m,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"be":be,"sz":sz}

    reason = f"15M:{tr_d}"
    if not near: reason += f" | dist {dist*100:.3f}%"
    else:        reason += f" | RSI:{rc:.0f}"
    return {"sig":"WATCH","reason":reason,"m":m}

# ── Exit check (partial TPs + breakeven) ─────────────────────────────────
def check_exits(ot, d5):
    """
    Returns list of events (partial closes) and whether trade is fully closed.
    Modifies ot in-place (sl, tp hits, rem, pnl).
    Returns: (events_list, fully_closed, close_reason, close_px)
    """
    i    = len(d5["ts"]) - 1
    h    = d5["h"][i]; l = d5["l"][i]; c = d5["c"][i]
    ot["bars"] = ot.get("bars", 0) + 1
    dirn = ot["dir"]
    events = []

    # ── Breakeven trigger ──────────────────────────────────────────────────
    if not ot.get("be_hit", False):
        if (dirn=="LONG" and h >= ot["be"]) or (dirn=="SHORT" and l <= ot["be"]):
            ot["sl"]     = ot["entry"]
            ot["be_hit"] = True
            events.append("BE_TRIGGERED")

    sl = ot["sl"]

    # ── TP partial hits ────────────────────────────────────────────────────
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

    # ── Full close conditions ──────────────────────────────────────────────
    if ot["tp3_hit"] or ot["rem"] <= 0:
        return events, True, "TP3", ot["tp3"]

    sl_hit = (dirn=="LONG" and l<=sl) or (dirn=="SHORT" and h>=sl)
    if sl_hit:
        sl_pnl = ((sl - ot["entry"]) if dirn=="LONG" else (ot["entry"] - sl)) * ot["size"] * ot["rem"]
        ot["pnl"] += sl_pnl; ot["rem"] = 0
        reason = "BE" if ot.get("be_hit") else "SL"
        return events, True, reason, sl

    if ot["bars"] >= Cfg.MAX_HOLD:
        t_pnl = ((c - ot["entry"]) if dirn=="LONG" else (ot["entry"] - c)) * ot["size"] * ot["rem"]
        ot["pnl"] += t_pnl; ot["rem"] = 0
        return events, True, "TIME", c

    return events, False, None, None

# ── Telegram ───────────────────────────────────────────────────────────────
def tg(text):
    if not Cfg.TG_TOKEN or not Cfg.TG_CHAT_ID:
        log.info(f"[TG] {text[:120]}")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{Cfg.TG_TOKEN}/sendMessage",
            json={"chat_id":Cfg.TG_CHAT_ID,"text":text,"parse_mode":"HTML"}, timeout=10)
    except Exception as e:
        log.warning(f"Telegram: {e}")

def tg_opened(sym, ot):
    tg(f"<b>{'📈' if ot['dir']=='LONG' else '📉'} {sym} {ot['dir']} OPENED</b>\n"
       f"Entry : ${ot['entry']:,.4f}\n"
       f"TP1   : ${ot['tp1']:,.4f}  (50% close)\n"
       f"TP2   : ${ot['tp2']:,.4f}  (30% close)\n"
       f"TP3   : ${ot['tp3']:,.4f}  (20% close)\n"
       f"SL    : ${ot['sl']:,.4f}\n"
       f"BE    : ${ot['be']:,.4f}  (SL→entry when hit)\n"
       f"Signal: {ot['reason']}")

def tg_tp_hit(sym, ot, event):
    tg(f"<b>🎯 {sym} {event} HIT</b>\n"
       f"Dir   : {ot['dir']}\n"
       f"Rem   : {ot['rem']*100:.0f}% still open\n"
       f"SL now: ${ot['sl']:,.4f}")

def tg_closed(sym, ot, capital):
    sign  = "+" if ot["pnl"] >= 0 else ""
    emoji = "✅ WIN" if ot["pnl"]>0 else ("⚖️ BREAK" if ot["pnl"]==0 else "❌ LOSS")
    tp_lvl = ('TP3' if ot['tp3_hit'] else 'TP2' if ot['tp2_hit'] else 'TP1' if ot['tp1_hit'] else 'NONE')
    tg(f"<b>{emoji} — {sym} {ot['dir']} closed ({ot.get('close_reason','?')})</b>\n"
       f"Entry   : ${ot['entry']:,.4f}\n"
       f"TP level: {tp_lvl}\n"
       f"P&L     : {sign}${ot['pnl']:,.2f}\n"
       f"Capital : ${capital:,.2f}")

def tg_heartbeat(S):
    ret = (S["capital"]-Cfg.IC)/Cfg.IC*100
    mdd = (S["peak"]-S["capital"])/S["peak"]*100 if S["peak"]>0 else 0
    lines = [
        f"<b>💓 Heartbeat #{S['checks']}</b>",
        f"Capital : ${S['capital']:,.2f} ({ret:+.2f}%)",
        f"MaxDD   : {mdd:.1f}%",
        f"Trades  : {len(S['trades'])} | Signals: {S['signals']} | Chops: {S['chops']}",
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

# ── Main loop ──────────────────────────────────────────────────────────────
def main():
    log.info("=== MTF BOT — BTC/ETH/SOL — Partial TP + Breakeven ===")
    S = load_state()
    log.info(f"State: checks={S['checks']} capital=${S['capital']:.2f} trades={len(S['trades'])}")
    tg(f"<b>🤖 MTF Bot started (3 pairs — Partial TP)</b>\n"
       f"Pairs   : {', '.join(PAIRS)}\n"
       f"TP1/2/3 : 50% @ 3.5R | 30% @ 7R | 20% @ 14R\n"
       f"Breakeven: SL→entry at 1:1\n"
       f"Capital : ${S['capital']:,.2f}\n"
       f"Backtest: PF 6.41 | MaxDD 3.0% | +94.6%")

    while True:
        loop_start = time.time()
        try:
            S["checks"] += 1
            log.info(f"=== Check #{S['checks']} ===")

            for sym in PAIRS:
                try:
                    raw5m  = fetch_klines(binance_url(sym, "5m",  600))
                    raw15m = fetch_klines(binance_url(sym, "15m", 500))
                    d5 = build(raw5m, raw15m)
                    px = d5["c"][-1]

                    if sym not in S["start_px"]:
                        S["start_px"][sym] = px

                    ps = S["pair_stats"].setdefault(sym, {"trades":0,"wins":0,"pnl":0.0})

                    # ── Exit check ─────────────────────────────────────────
                    ot = S["open_trades"].get(sym)
                    if ot:
                        events, closed, c_reason, c_px = check_exits(ot, d5)

                        # Notify partial TP hits
                        for ev in events:
                            if ev.startswith("TP"):
                                tg_tp_hit(sym, ot, ev.split(":")[0])
                            elif ev == "BE_TRIGGERED":
                                tg(f"<b>🔒 {sym} — Breakeven triggered!</b>\n"
                                   f"SL moved to entry: ${ot['entry']:,.4f}")

                        if closed:
                            ot["close_reason"] = c_reason
                            S["capital"] += ot["pnl"]
                            S["peak"]     = max(S["peak"], S["capital"])
                            S["trades"].append({**ot, "symbol":sym,
                                                "close_time":datetime.now(timezone.utc).isoformat()})
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

                    # ── Entry check ────────────────────────────────────────
                    if sym not in S["open_trades"]:
                        result = get_signal(d5, S["capital"])
                        sig    = result["sig"]
                        m      = result.get("m", {})
                        log.info(f"{sym} ${px:,.2f} | 15M:{m.get('tr','?')} | "
                                 f"RSI:{m.get('rsi',0):.1f} | ATR:{m.get('ar',0):.2f}x | "
                                 f"Dist:{m.get('dist',0)*100:.3f}% | {sig}")
                        if sig == "CHOP":
                            S["chops"] += 1
                        elif sig in ("LONG","SHORT"):
                            new_ot = {
                                "symbol":   sym,
                                "dir":      sig,
                                "entry":    result["m"]["px"],
                                "sl":       result["sl"],
                                "tp1":      result["tp1"],
                                "tp2":      result["tp2"],
                                "tp3":      result["tp3"],
                                "be":       result["be"],
                                "size":     result["sz"],
                                "be_hit":   False,
                                "tp1_hit":  False,
                                "tp2_hit":  False,
                                "tp3_hit":  False,
                                "rem":      1.0,
                                "pnl":      0.0,
                                "bars":     0,
                                "reason":   result["reason"],
                                "open_time":datetime.now(timezone.utc).isoformat()
                            }
                            S["open_trades"][sym] = new_ot
                            S["signals"] += 1
                            log.info(f"{sym} {sig} entry=${new_ot['entry']:.2f} "
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
            tg(f"⚠️ Bot error: {e}\nRetrying in 5 min...")

        elapsed = time.time() - loop_start
        time.sleep(max(5, Cfg.INTERVAL - elapsed))

if __name__ == "__main__":
    main()
