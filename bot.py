"""
MTF Scalping Bot — Railway Cloud Deployment
Strategy : 30M EMA9 trend filter + 5M EMA21 pullback entry
           RSI(14) bounce/reject + ATR chop filter
Data     : Binance public API (no key needed)
Alerts   : Telegram
"""

import os, time, json, logging, requests
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("MTFBot")

class Cfg:
    TG_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    TF_EMA = 9
    M5_EMA = 21
    RSI_P = 14
    ATR_P = 14
    PULL_PCT = 0.007
    SL_MULT = 1.5
    TP_MULT = 3.5
    MAX_HOLD = 48
    RSI_LO = 40.0
    RSI_HI = 60.0
    RSI_FLOOR = 25.0
    RSI_CEIL = 75.0
    ATR_REL = 0.90
    ATR_AVG_N = 100
    IC = 10_000.0
    RISK_PCT = 0.0075
    INTERVAL = 300
    URL_5M = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=600"
    URL_30M = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=30m&limit=500"
    STATE_FILE = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")) / "bot_state.json"

def load_state():
    try:
        if Cfg.STATE_FILE.exists():
            return json.loads(Cfg.STATE_FILE.read_text())
    except Exception as e:
        log.warning(f"Could not load state: {e}")
    return {"capital": Cfg.IC, "peak": Cfg.IC, "open_trade": None, "trades": [], "checks": 0, "signals": 0, "chops": 0, "start_px": None, "started_at": datetime.now(timezone.utc).isoformat()}

def save_state(S):
    try:
        Cfg.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        Cfg.STATE_FILE.write_text(json.dumps(S, indent=2))
    except Exception as e:
        log.warning(f"Could not save state: {e}")

def calc_ema(values, period):
    k = 2 / (period + 1)
    out = [None] * len(values)
    v = None
    for i, x in enumerate(values):
        if x is None: continue
        v = x if v is None else x * k + v * (1 - k)
        out[i] = v
    return out

def calc_rsi(closes, period=14):
    out = [None] * len(closes)
    if len(closes) < period + 2: return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0: gains += d
        else: losses -= d
    gains /= period; losses /= period
    out[period] = 100 if losses == 0 else 100 - 100 / (1 + gains / losses)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains = (gains * (period - 1) + (d if d > 0 else 0)) / period
        losses = (losses * (period - 1) + (-d if d < 0 else 0)) / period
        out[i] = 100 if losses == 0 else 100 - 100 / (1 + gains / losses)
    return out

def calc_atr(highs, lows, closes, period=14):
    tr_l = [None] * len(highs)
    out = [None] * len(highs)
    for i in range(1, len(highs)):
        tr_l[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    s, n = 0.0, 0
    for i in range(1, len(tr_l)):
        if tr_l[i] is None: continue
        if n < period:
            s += tr_l[i]; n += 1
            if n == period: out[i] = s / period
        else: out[i] = (out[i - 1] * (period - 1) + tr_l[i]) / period
    return out

def rolling_mean(values, window):
    out = [None] * len(values)
    for i in range(window - 1, len(values)):
        chunk = [v for v in values[i - window + 1: i + 1] if v is not None]
        out[i] = sum(chunk) / len(chunk) if chunk else None
    return out

def fetch_klines(url):
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()

def build(raw5m, raw30m):
    def parse(raw):
        o,h.l,c,ts = [],[],[],[],[]
        for k in raw:
            ts.append(int(k[0]) / 1000)
            o.append(float(k[1])); h.append(float(k[2]))
            l.append(float(k[3])); c.append(float(k[4]))
        return {"o":o,"h":h,"l":l,"c":c,"ts":ts}
    d5 = parse(raw5m); d30 = parse(raw30m)
    d30["e9"] = calc_ema(d30["c"], Cfg.TF_EMA)
    e9p = [None] + d30["e9"][:-1]
    d30["up"] = [i > 0 and d30["c"][i] > d30["e9"][i] and d30["e9"][i] is not None and e9p[i] is not None and d30["e9"][i] > e9p[i] for i in range(len(d30["ts"]))]
    d30["dn"] = [i > 0 and d30["c"][i] < d30["e9"][i] and d30["e9"][i] is not None and e9p[i] is not None and d30["e9"][i] < e9p[i] for i in range(len(d30["ts"]))]
    def ff(vals):
        result, j, last = [], 0, None
        for t in d5["ts"]:
            while j < len(d30["ts"]) and d30["ts"][j] <= t: last = vals[j]; j += 1
            result.append(last)
        return result
    d5["tf_up"] = ff(d30["up"]); d5["tf_dn"] = ff(d30["dn"]); d5["tf_e9"] = ff(d30["e9"])
    d5["e21"] = calc_ema(d5["c"], Cfg.M5_EMA)
    d5["rsi"] = calc_rsi(d5["c"], Cfg.RSI_P)
    d5["atr"] = calc_atr(d5["h"], d5["l"], d5["c"], Cfg.ATR_P)
    d5["atr_a"] = rolling_mean(d5["atr"], Cfg.ATR_AVG_N)
    d5["dist"] = [abs(d5["c"][i] - d5["e21"][i]) / d5["e21"][i] if d5["e21"][i] else None for i in range(len(d5["c"]))]
    return d5

def get_signal(d5, capital):
    n = len(d5["ts"])
    i, p = n - 2, n - 3
    if i < 60 or d5["atr"][i] is None or d5["rsi"][i] is None: return {"sig": "WATCH", "reason": "warming up"}
    c = d5["c"][i]; o = d5["o"][i]; h = d5["h"][i]; l = d5["l"][i]
    rc = d5["rsi"][i]; rp = d5["rsi"][p] if d5["rsi"][p] else rc
    atr = d5["atr"][i]
    ar = d5["atr"][i] / d5["atr_a"][i] if d5["atr_a"][i] else None
    dist = d5["dist"][i] if d5["dist"][i] else 999
    near = dist < Cfg.PULL_PCT
    rng = h - l
    body = abs(c - o) / rng if rng > 0 else 0
    bull = c > o and body > 0.45
    bear = c < o and body > 0.45
    tr_d = "UP" if d5["tf_up"][i] else ("DOWN" if d5["tf_dn"][i] else "FLAT")
    m = {"px":c, "e21":d5["e21"][i], "tf_e9":d5["tf_e9"][i], "rsi":rc, "atr":atr, "ar":ar, "tr":tr_d, "near":near, "dist":dist}
    if ar is not None and ar < Cfg.ATR_REL: return {"sig":"CHOP", "reason":f"ATR {ar:.2f}x < {Cfg.ATR_REL}", "m":m}
    if d5["tf_up"][i] and near and rp < Cfg.RSI_LO and rc > rp and rc > Cfg.RSI_FLOOR and bull:
        sl = c - atr * Cfg.SL_MULT; tp = c + atr * Cfg.TP_MULT; sz = (capital * Cfg.RISK_PCT) / (atr * Cfg.SL_MULT)
        return {"sig":"LONG", "reason":f"30M-UP EMA9 + EMA21 + RSI {rp:.0f}->{rc:.0f}", "m":m, "sl":sl, "tp":tp, "sz":sz}
    if d5["tf_dn"][i] and near and rp > Cfg.RSI_HI and rc < rp and rc < Cfg.RSI_CEIL and bear:
        sl = c + atr * Cfg.SL_MULT; tp = c - atr * Cfg.TP_MULT; sz = (capital * Cfg.RISK_PCT) / (atr * Cfg.SL_MULT)
        return {"sig":"SHORT", "reason":f"30M-DOWN EMA9 + EMA21 + RSI {rp:.0f}->{rc:.0f}", "m":m, "sl":sl, "tp":tp, "sz":sz}
    reason = f"30M:{tr_d}"
    if not near: reason += f" | dist {dist*100:.3f}% (need <0.7%)"
    else: reason += f" | RSI:{rc:.0f} bull:{bull} bear:{bear}"
    return {"sig":"WATCH", "reason":reason, "m":m}

def check_exit(ot, d5):
    i = len(d5["ts"]) - 1
    h = d5["h"][i]; l = d5["l"][i]; c = d5["c"][i]
    ot["bars"] = ot.get("bars", 0) + 1
    hit = None
    if ot["dir"] == "LONG":
        if h >= ot["tp"]: hit = ("TP", ot["tp"])
        elif l <= ot["sl"]: hit = ("SL", ot["sl"])
        elif ot["bars"] >= Cfg.MAX_HOLD: hit = ("TIME", c)
    else:
        if l <= ot["tp"]: hit = ("TP", ot["tp"])
        elif h >= ot["sl"]: hit = ("SL", ot["sl"])
        elif ot["bars"] >= Cfg.MAX_HOLD: hit = ("TIME", c)
    if not hit: return None
    pnl = (hit[1] - ot["entry"]) * ot["size"] if ot["dir"] == "LONG" else (ot["entry"] - hit[1]) * ot["size"]
    return {**ot, "exit": hit[1], "reason": hit[0], "pnl": pnl, "close_time": datetime.now(timezone.utc).isoformat()}

def tg_send(text):
    if not Cfg.TG_TOKEN or not Cfg.TG_CHAT_ID:
        log.info(f"[TG skipped] {text[:80]}"); return
    try:
        requests.post(f"https://api.telegram.org/bot{Cfg.TG_TOKEN}/sendMessage", json={"chat_id":Cfg.TG_CHAT_ID,"text":text,"parse_mode":"HTML"}, timeout=10)
    except Exception as e: log.warning(f"Telegram error: {e}")

def tg_trade_opened(ot):
    emoji = "LONG" if ot["dir"] == "LONG" else "SHORT"
    tg_send(f"<b>{emoji} OPENED</b>\nEntry: ${ot['entry']:,.2f}\nTP: ${ot['tp']:,.2f}\nSL: ${ot['sl']:,.2f}\nSize: {ot['size']:.4f} BTC\nReason: {ot['reason']}")

def tg_trade_closed(t, capital):
    emoji = "WIJ" if t["pnl"] > 0 else "LOSS"
    tg_send(f"<b>{emoji} {t['reason']} {t['dir']} CLOSED</b>\nEntry: ${t['entry']:,.2f}\nExit: ${t['exit']:,.2f}\nP&L: {+'if t['pnl']>=0 else ''}${t['pnl']:,.2f}\nCapital: ${capital:,.2f}")

def tg_heartbeat(S, m):
    ret = (S["capital"] - Cfg.IC) / Cfg.IC * 100
    mdd = (S["peak"] - S["capital"]) / S["peak"] * 100 if S["peak"] > 0 else 0
    tg_send(f"<b>Heartbeat #{S['checks']}</b>\nBTC: ${m.get('px',0):,.0f}\n30M: {m.get('tr','?')}\nCapital: ${S['capital']:,.2f} ({ret:+.2f}%)\nMaxDD: {mdd:.1f}%\nTrades: {len(S['trades'])} | Signals: {S['signals']} | Chops: {S['chops']}")

def main():
    log.info("=== MTF SCALPING BOT - Railway Cloud ===")
    S = load_state()
    log.info(f"State loaded: checks={S['checks']} capital=${S['capital']:.2f} trades={len(S['trades'])}")
    tg_send(f"<b>MTF Bot started</b>\nCapital: ${S['capital']:,.2f}\nChecks so far: {S['checks']}\nStrategy: 30M EMA9 + 5M EMA21")
    while True:
        loop_start = time.time()
        try:
            log.info(f"=== Check #{S['checks']+1} ===")
            raw5m = fetch_klines(Cfg.URL_5M); raw30m = fetch_klines(Cfg.URL_30M)
            d5 = build(raw5m, raw30m)
            S["checks"] += 1; px = d5["c"][-1]
            if S["start_px"] is None: S["start_px"] = px
            if S["open_trade"]:
                closed = check_exit(S["open_trade"], d5)
                if closed:
                    S["capital"] += closed["pnl"]; S["peak"] = max(S["peak"], S["capital"])
                    S["trades"].append(closed); S["open_trade"] = None
                    log.info(f"{closed['reason']} {closed['dir']} P&L ${closed['pnl']:+.2f} | cap=${S['capital']:.2f}")
                    tg_trade_closed(closed, S["capital"])
                else:
                    ot = S["open_trade"]; ep = (px - ot["entry"]) * ot["size"] if ot["dir"]=="LONG" else (ot["entry"] - px) * ot["size"]
                    log.info(f"HOLDING {ot['dir']} {ot['bars']}bars | est P&L ${ep:+.2f}")
            result = get_signal(d5, S["capital"])
            m = result.get("m", {})
            sig = result["sig"]
            log.info(f"BTC ${px:,
.0f} | 30M:{m.get('tr','?')} | RSI:{m.get('rsi',0):.1f} | ATR:{m.get('ar',0):.2f}x | Dist:{m.get('dist',0)*100:.3f}% | {sig}")
            if not S["open_trade"]:
                if sig == "CHOP": S["chops"] += 1
                elif sig in ("LONG", "SHORT"):
                    ot = {"dir": sig, "entry": result["m"]["px"], "sl": result["sl"], "tp": result["tp"], "size": result["sz"], "bars": 0, "reason": result["reason"], "open_time": datetime.now(timezone.utc).isoformat()}
                    S["open_trade"] = ot; S["signals"] += 1
                    log.info(f"{sig} entry=${ot['entry']:.0f} TP=${ot['tp']:.0f} SL=${ot['sl']:.0f}")
                    tg_trade_opened(ot)
            save_state(S)
            if S["checks"] % 12 == 0: tg_heartbeat(S, m)
        except requests.exceptions.RequestException as e:
            log.error(f"Network error: {e}"); tg_send(f"Network error: {e}\nRetrying in 5 min...")
        except Exception as e:
            log.exception(f"Unexpected error: {e}"); tg_send(f"Bot error: {e}\nRetrying in 5 min...")
        elapsed = time.time() - loop_start
        time.sleep(max(5, Cfg.INTERVAL - elapsed))

if __name__ == "__main__":
    main()
