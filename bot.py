"""
MTF Scalping Bot — CHAMPION v4.0 + LIVE EXECUTION
===================================================
Strategy  : 15M EMA9 trend + 5M EMA21 pullback + RSI + ATR chop filter
Pairs     : BTCUSDT, ETHUSDT, SOLUSDT
Execution : Binance USD-M Futures via CCXT (executor.py)

NEW in v4.1 (vs v4.0):
  • Real order execution via executor.py
  • Server-side stop-loss orders (survive bot crashes)
  • Partial TP exits as real market orders
  • Breakeven SL moves on exchange
  • Circuit breaker: 3 consecutive losses OR 5% daily DD → halt
  • TRADING_MODE env var: "live" or "paper"
  • Position sizing from actual Futures wallet balance

⚠️ ENV REQUIRED: BINANCE_API_KEY, BINANCE_API_SECRET
⚠️ ENV REQUIRED: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
⚠️ ENV OPTIONAL: TRADING_MODE (default: live), FUTURES_LEVERAGE (default: 1)
🔴 RISK: When TRADING_MODE=live, this bot places REAL orders with REAL money

Backtest (BTC, Jan–Mar 2026):
  Baseline (v2): 91 trades | WR 49.5% | PF  6.41 | MaxDD 3.0%
  Champion (v3): 50 trades | WR 56.0% | PF 18.13 | MaxDD 1.5%
  (3-pair live expected ~150 trades/period)
"""

import os, time, json, logging, requests
from datetime import datetime, timezone
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ── Execution layer import ──
import executor

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

    # ── Core strategy ──────────────────────────────────────────────
    TF_EMA      = 9          # 15M EMA period
    M5_EMA      = 21         # 5M EMA period
    RSI_P       = 14
    ATR_P       = 14

    # ── Entry filters ─────────────────────────────────────────────
    PULL_PCT    = 0.005       # v5: 0.5% pull-back zone (was 0.8%) — tighter = better edge
    RSI_LO      = 45          # v3: long when RSI < 45 (was 40)
    RSI_HI      = 60          # v3: short when RSI > 60 (unchanged)
    RSI_FLOOR   = 25.0
    RSI_CEIL    = 75.0
    ATR_REL     = 0.70        # v4: 0.70x avg (was 0.90x) - more valid setups
    ATR_AVG_N   = 100

    # ── Session filter (v5 EXPANDED) ─────────────────────────────
    SESSION_START = 6         # v5: 06:00 UTC (was 07:00) — capture London pre-market
    SESSION_END   = 22        # v5: 22:00 UTC (was 20:00) — capture full NY session

    # ── 1H RSI filter (v3 NEW) ────────────────────────────────────
    RSI_1H_LO   = 40.0        # 1H RSI must be > 40 to go LONG
    RSI_1H_HI   = 60.0        # 1H RSI must be < 60 to go SHORT

    # ── Risk & sizing ─────────────────────────────────────────────
    SL_MULT     = 1.5         # v5: 1.5× ATR (was 1.8) — backtest #1 of 100 combos
    TP1_MULT    = 4.5         # v3: 4.5× ATR (was 3.5)
    TP2_MULT    = 7.2         # v3: 7.2× ATR (= 1.6 × TP1)
    TP3_MULT    = 30.0        # v4: 30x ATR - catch bigger runners (was 18x)
    TP1_FRAC    = 0.50        # v5: close 50% at TP1 (was 40%) — bank faster
    TP2_FRAC    = 0.20        # v5: 20% at TP2 (was 30%); remaining 30% at TP3
    # TP1+TP2+TP3 fracs = 0.50+0.20+0.30 = 1.0

    MAX_HOLD    = 36          # v5: 36 bars = 3h (was 48/4h) — lower DD, better PF
    IC          = 10_000.0    # initial capital (virtual tracking)
    RISK_PCT    = 0.01        # 🔴 RISK: 1.0% risk per trade

    INTERVAL    = 300         # check every 5 minutes
    STATE_FILE  = Path(os.getenv("BOT_DATA_DIR", os.getenv("RAILWAY_VOLUME_MOUNT_PATH", r"C:\botdata"))) / "bot_state.json"


# ─── Indicators ───────────────────────────────────────────────────────────────

def calc_ema(values, period):
    """Calculate Exponential Moving Average."""
    k = 2 / (period + 1)
    out, v = [None]*len(values), None
    for i, x in enumerate(values):
        if x is None: continue
        v = x if v is None else x*k + v*(1-k)
        out[i] = v
    return out

def calc_rsi(closes, period=14):
    """Calculate Relative Strength Index."""
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
    """Calculate Average True Range."""
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
    """Calculate rolling mean over a window."""
    out = [None]*len(values)
    for i in range(window-1, len(values)):
        chunk = [v for v in values[i-window+1:i+1] if v is not None]
        out[i] = sum(chunk)/len(chunk) if chunk else None
    return out


# ─── State ────────────────────────────────────────────────────────────────────

def reconcile_open_trades(S: dict) -> None:
    """Purge stale open_trades that have no matching Binance position on startup.

    After a crash, restart, or manual intervention, bot_state.json may contain
    trade records for positions that were already closed on Binance (e.g. by a
    server-side SL while the bot was offline). If left in open_trades, these
    phantom entries are evaluated by check_exits() on the first check cycle, and
    their SL prices may already be exceeded — causing up to N consecutive phantom
    losses that trip the circuit breaker before any real trade is placed.

    This function queries Binance for each open_trade symbol and removes entries
    that have no real position. It does NOT record P&L or feed the circuit breaker
    — the trade outcome is unknown (SL may or may not have filled at exactly SL px).

    🔴 RISK: Calling this at startup means any real position opened by the bot
    that is still active on Binance is preserved. Only truly closed positions are
    removed. Call AFTER _get_exchange() has been verified to work.

    📋 TEST THIS: Check Telegram startup message for "reconciled N stale trade(s)"
    when restarting with phantom state entries.
    """
    import time as _time

    if not S.get("open_trades"):
        return

    MAX_RETRIES  = 3   # attempts per symbol before aggressive removal
    RETRY_DELAY  = 2   # seconds between attempts

    stale_symbols    = []
    api_fail_symbols = []

    for sym in list(S["open_trades"].keys()):
        last_exc  = None
        confirmed = False

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                pos = executor.get_open_position(sym)
                if pos is None or pos["qty"] <= 0:
                    log.warning(
                        f"RECONCILE: {sym} in open_trades but NO Binance position — "
                        f"removing phantom trade (no P&L recorded)"
                    )
                    stale_symbols.append(sym)
                else:
                    log.info(
                        f"RECONCILE: {sym} confirmed open on Binance — "
                        f"side={pos['side']} qty={pos['qty']:.6f} "
                        f"entry={pos['entry_price']:.4f}"
                    )
                confirmed = True
                break  # success — stop retrying
            except Exception as e:
                last_exc = e
                log.warning(
                    f"RECONCILE: {sym} attempt {attempt}/{MAX_RETRIES} failed: {e}"
                )
                if attempt < MAX_RETRIES:
                    _time.sleep(RETRY_DELAY)

        if not confirmed:
            # 🔴 FIX: All retries exhausted. Remove the trade anyway.
            # Rationale: a phantom reaching check_exits() causes a fake consecutive
            # loss that trips the CB. Server-side STOP_MARKET (closePosition=True)
            # still guards capital on Binance even when we stop tracking here.
            log.error(
                f"RECONCILE: {sym} — unverifiable after {MAX_RETRIES} attempts "
                f"(last: {last_exc}). Removing from tracking. "
                f"Server-side SL remains active on Binance."
            )
            stale_symbols.append(sym)
            api_fail_symbols.append(sym)

    for sym in stale_symbols:
        S["open_trades"].pop(sym, None)

    if stale_symbols:
        api_note = ""
        if api_fail_symbols:
            api_note = (
                f"\n⚠️ {len(api_fail_symbols)} removed due to API timeout (unverifiable): "
                f"{', '.join(api_fail_symbols)}. Server-side SL still active."
            )
        msg = (
            f"⚠️ Startup reconciliation removed {len(stale_symbols)} stale trade(s): "
            f"{', '.join(stale_symbols)}\n"
            f"P&L NOT recorded. Circuit breaker NOT penalised.{api_note}"
        )
        log.warning(msg)
        tg(msg)
    else:
        log.info("RECONCILE: All open_trades verified against Binance — no stale entries")


def cleanup_orphan_sl_orders(S: dict) -> None:
    """Cancel and re-place a fresh SL for every still-tracked open_trade at startup.

    🔴 FIX (-4130 race on restart): After any crash/restart, server-side SL orders
    may linger on Binance while the bot's internal state has drifted (be_hit flag
    reset, capital re-synced, etc.). On the next BE trigger or TP partial close,
    move_stop_loss() tries to replace the SL — but the old one is still registered
    in Binance's internal -4130 tracker and the placement fails.

    By proactively cancelling and re-placing the SL for every open_trade at startup
    we guarantee a clean slate. executor.move_stop_loss() uses _place_closeposition_sl_with_retry()
    internally, so the helper's cancel → wait → retry defence applies here too.

    Call AFTER reconcile_open_trades() so we only process real (non-phantom) positions.
    """
    if not S.get("open_trades"):
        log.info("Orphan SL cleanup: no open trades to sweep")
        return

    for sym, ot in list(S["open_trades"].items()):
        try:
            direction    = ot["dir"]
            remaining_qty = ot["size"] * ot.get("rem", 1.0)
            sl_price     = ot["sl"]
            log.info(
                f"Orphan SL cleanup: refreshing SL for {sym} "
                f"({direction} qty={remaining_qty:.6f} sl={sl_price})"
            )
            # move_stop_loss → _place_closeposition_sl_with_retry (cancel + wait + retry)
            result = executor.move_stop_loss(
                symbol=sym,
                direction=direction,
                new_sl_price=sl_price,
                remaining_qty=remaining_qty,
            )
            if result["success"]:
                ot["exec_sl_id"] = result["sl_order_id"]
                log.info(f"Orphan SL cleanup: {sym} fresh SL placed ({result['sl_order_id']})")
                tg(f"\U0001f6e1 <b>{sym}</b> startup SL refreshed @ {sl_price}")
            else:
                log.warning(
                    f"Orphan SL cleanup: {sym} failed to refresh SL "
                    f"({result['error']}) — next trigger will retry via helper"
                )
        except Exception as e:
            log.error(f"Orphan SL cleanup: {sym} unexpected error: {e}")

    log.info("Orphan SL cleanup: complete")


def fresh_state():
    """Create a fresh bot state dictionary."""
    return {
        "capital": Cfg.IC, "peak": Cfg.IC,
        "checks": 0, "signals": 0, "chops": 0,
        "start_px": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
        "open_trades": {},
        "trades": [],
        "pair_stats": {p: {"trades":0,"wins":0,"pnl":0.0} for p in PAIRS},
        "circuit_breaker": {},  # v4.1: circuit breaker state
    }

def load_state():
    """Load bot state from disk, with safe defaults for missing keys."""
    try:
        if Cfg.STATE_FILE.exists():
            s = json.loads(Cfg.STATE_FILE.read_text())
            if "open_trades" not in s: s["open_trades"] = {}
            if "pair_stats"  not in s: s["pair_stats"]  = {p: {"trades":0,"wins":0,"pnl":0.0} for p in PAIRS}
            if "circuit_breaker" not in s: s["circuit_breaker"] = {}
            return s
    except Exception as e:
        log.warning(f"Could not load state: {e}")
    return fresh_state()

def save_state(S):
    """Persist bot state to disk atomically.

    🔴 FIX (Bug 4): write to .tmp then atomic rename. The previous
    write_text() truncated the target before writing — a crash mid-write
    left botstate.json corrupt; load_state() then fell back to fresh_state
    and ALL accumulated trades + capital sync were silently lost.
    Path.replace() is POSIX-atomic and on Windows uses MoveFileEx with
    replace flag (also atomic).
    """
    try:
        S["circuit_breaker"] = executor.circuit_breaker.to_dict()
        Cfg.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = Cfg.STATE_FILE.with_suffix(Cfg.STATE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(S, indent=2))
        tmp.replace(Cfg.STATE_FILE)
    except Exception as e:
        log.warning(f"Could not save state: {e}")


# ─── Data & build ────────────────────────────────────────────────────────────

def fetch_klines(symbol, interval, limit):
    """Fetch kline data from Binance public API."""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()

def build(raw5m, raw15m, raw1h):
    """Build indicator dataset from raw kline data across 3 timeframes."""
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


# ─── Signal ───────────────────────────────────────────────────────────────────

def get_signal(d5, capital):
    """Generate entry signal from 5M indicator dataset.

    Returns dict with sig: LONG/SHORT/WATCH/CHOP and all trade parameters.
    """
    n = len(d5["ts"])
    i, p = n-2, n-3
    if i < 120 or d5["atr"][i] is None or d5["rsi"][i] is None:
        return {"sig": "WATCH", "reason": "warming up"}

    c  = d5["c"][i]; o = d5["o"][i]
    h  = d5["h"][i]; l = d5["l"][i]
    rc = d5["rsi"][i]
    rp = d5["rsi"][i-1] if d5["rsi"][i-1] else rc
    atr_val   = d5["atr"][i]
    # 🔴 FIX (Bug 7): guard against atr=0 in flat markets — would div-by-zero
    # in sizing formula `sz = (capital * RISK_PCT) / (atr_val * SL_MULT)`.
    if atr_val < 1e-6:
        return {"sig": "WATCH", "reason": "ATR ~0 (flat market)"}
    atr_avg   = d5["atr_avg"][i]
    ar        = atr_val/atr_avg if atr_avg else None
    dist      = d5["dist"][i]   if d5["dist"][i] else 999
    rsi_1h    = d5["rsi_1h"][i]
    hour      = d5["hour"][i]
    rsi_rise  = d5["rsi_rising"][i]
    rsi_fall  = d5["rsi_falling"][i]

    m = {"px":c,"e21":d5["e21"][i],"rsi":rc,"atr":atr_val,
         "ar":ar,"hour":hour,"rsi_1h":rsi_1h}

    # ── Chop filter ──────────────────────────────────────────────
    if ar is None or ar < Cfg.ATR_REL:
        return {"sig": "CHOP", "reason": f"ATR {ar:.2f}x" if ar else "ATR N/A", "m": m}

    # ── Session filter (v3) ──────────────────────────────────────
    if not (Cfg.SESSION_START <= hour < Cfg.SESSION_END):
        return {"sig": "WATCH", "reason": f"off-session {hour}:xx UTC", "m": m}

    # ── Candle quality ───────────────────────────────────────────
    rng  = h - l
    body = abs(c-o)/rng if rng > 0 else 0
    bull = c > o and body > 0.45
    bear = c < o and body > 0.45

    # ── Distance filter ──────────────────────────────────────────
    near = dist < Cfg.PULL_PCT

    tr_d = "UP" if d5["tf_up"][i] else ("DOWN" if d5["tf_dn"][i] else "FLAT")
    m["tr"] = tr_d  # expose 15M trend to log display (was missing — caused 15M:? in logs)

    # ── LONG signal ──────────────────────────────────────────────
    if (d5["tf_up"][i]
            and near
            and rp < Cfg.RSI_LO and rc > rp       # RSI turning up from below 45
            and rc > Cfg.RSI_FLOOR
            and bull
            and rsi_rise                             # v3: RSI rising 2 bars
            and (rsi_1h is None or rsi_1h > Cfg.RSI_1H_LO)):  # v3: 1H filter
        sl  = c - atr_val * Cfg.SL_MULT
        tp1 = c + atr_val * Cfg.TP1_MULT
        tp2 = c + atr_val * Cfg.TP2_MULT
        tp3 = c + atr_val * Cfg.TP3_MULT
        be  = c + atr_val * Cfg.SL_MULT
        sz  = (capital * Cfg.RISK_PCT) / (atr_val * Cfg.SL_MULT)
        # 🔴 FIX (margin-cap): risk-based sz ignores leverage/wallet. On small
        # accounts during low-ATR regimes, notional can exceed wallet × leverage
        # → pre-flight blocks trade. Cap sz so notional ≤ 88% of capital × leverage
        # (synced with executor's Layer-B 88% — eliminates double-shrink between layers).
        _lev = max(executor.LEVERAGE, 1)
        _max_notional = capital * _lev * 0.88
        _max_sz = _max_notional / c
        if sz > _max_sz:
            log.warning(
                f"Sizing capped by margin: risk-based={sz:.6f} → "
                f"margin-based={_max_sz:.6f} (cap notional ${_max_notional:.2f} "
                f"@ {_lev}x on ${capital:.2f})"
            )
            sz = _max_sz
        return {"sig": "LONG",
                "reason": f"15M-UP EMA+RSI {rp:.0f}→{rc:.0f}",
                "m": m, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "be": be, "sz": sz, "atr": atr_val}

    # ── SHORT signal ─────────────────────────────────────────────
    if (d5["tf_dn"][i]
            and near
            and rp > Cfg.RSI_HI and rc < rp        # RSI turning down from above 60
            and rc < Cfg.RSI_CEIL
            and bear
            and rsi_fall                             # v3: RSI falling 2 bars
            and (rsi_1h is None or rsi_1h < Cfg.RSI_1H_HI)):  # v3: 1H filter
        sl  = c + atr_val * Cfg.SL_MULT
        tp1 = c - atr_val * Cfg.TP1_MULT
        tp2 = c - atr_val * Cfg.TP2_MULT
        tp3 = c - atr_val * Cfg.TP3_MULT
        be  = c - atr_val * Cfg.SL_MULT
        sz  = (capital * Cfg.RISK_PCT) / (atr_val * Cfg.SL_MULT)
        # 🔴 FIX (margin-cap): mirror of LONG path — see comment above.
        _lev = max(executor.LEVERAGE, 1)
        _max_notional = capital * _lev * 0.88
        _max_sz = _max_notional / c
        if sz > _max_sz:
            log.warning(
                f"Sizing capped by margin: risk-based={sz:.6f} → "
                f"margin-based={_max_sz:.6f} (cap notional ${_max_notional:.2f} "
                f"@ {_lev}x on ${capital:.2f})"
            )
            sz = _max_sz
        return {"sig": "SHORT",
                "reason": f"15M-DOWN EMA+RSI {rp:.0f}→{rc:.0f}",
                "m": m, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "be": be, "sz": sz, "atr": atr_val}

    reason = f"15M:{tr_d}"
    if not near: reason += f" | dist {dist*100:.3f}%"
    else:        reason += f" | RSI:{rc:.0f}"
    if not (Cfg.SESSION_START <= hour < Cfg.SESSION_END):
        reason += f" | off-session"
    return {"sig": "WATCH", "reason": reason, "m": m}


# ─── Exit check (partial TPs + breakeven) ────────────────────────────────────

def check_exits(ot, d5):
    """Check BE trigger, partial TP hits, and full close conditions.

    Modifies ot in-place. Returns (events, fully_closed, close_reason, close_px).
    Events are strings like "TP1", "TP2", "BE_TRIGGERED" for executor integration.
    """
    i    = len(d5["ts"]) - 1
    h    = d5["h"][i]; l = d5["l"][i]; c = d5["c"][i]
    ot["bars"] = ot.get("bars", 0) + 1
    dirn = ot["dir"]
    events = []

    # ── Breakeven trigger ────────────────────────────────────────
    if not ot.get("be_hit", False):
        if (dirn=="LONG"  and h >= ot["be"]) or \
           (dirn=="SHORT" and l <= ot["be"]):
            ot["sl"]     = ot["entry"]
            ot["be_hit"] = True
            events.append("BE_TRIGGERED")

    sl = ot["sl"]

    # ── Partial TP hits ──────────────────────────────────────────
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

    # ── Full close conditions ────────────────────────────────────
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


# ─── Telegram ─────────────────────────────────────────────────────────────────

def tg(text):
    """Send a message via Telegram bot API."""
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
    """Send Telegram alert for new trade opened."""
    mode = executor.get_mode_label()
    exec_info = ""
    if ot.get("exec_order_id"):
        exec_info = (f"\nExec    : {mode}\n"
                     f"Fill    : ${ot.get('exec_fill_price', 0):,.4f}\n"
                     f"OrderID : {ot.get('exec_order_id', 'N/A')}")
    else:
        exec_info = f"\nExec    : {mode} (signal only)"

    emoji = "\U0001f7e2" if ot['dir']=='LONG' else "\U0001f534"
    tg(f"<b>{emoji} {sym} {ot['dir']} OPENED</b>\n"
       f"Entry : ${ot['entry']:,.4f}\n"
       f"TP1   : ${ot['tp1']:,.4f}  (50% close)\n"
       f"TP2   : ${ot['tp2']:,.4f}  (20% close)\n"
       f"TP3   : ${ot['tp3']:,.4f}  (30% close)\n"
       f"SL    : ${ot['sl']:,.4f}\n"
       f"BE    : ${ot['be']:,.4f}  (SL→entry when hit)\n"
       f"Signal: {ot['reason']}"
       f"{exec_info}")

def tg_tp_hit(sym, ot, event):
    """Send Telegram alert for partial TP hit."""
    tg(f"<b>\U0001f3af {sym} {event} HIT</b>\n"
       f"Dir   : {ot['dir']}\n"
       f"Rem   : {ot['rem']*100:.0f}% still open\n"
       f"SL now: ${ot['sl']:,.4f}")

def tg_closed(sym, ot, capital):
    """Send Telegram alert for trade closed."""
    sign  = "+" if ot["pnl"] >= 0 else ""
    emoji = "\u2705 WIN" if ot["pnl"]>0 else ("\u2696\ufe0f BREAK" if ot["pnl"]==0 else "\u274c LOSS")
    tp_lvl = ('TP3' if ot['tp3_hit'] else 'TP2' if ot['tp2_hit'] else 'TP1' if ot['tp1_hit'] else 'NONE')
    mode = executor.get_mode_label()
    tg(f"<b>{emoji} — {sym} {ot['dir']} closed ({ot.get('close_reason','?')})</b>\n"
       f"Entry    : ${ot['entry']:,.4f}\n"
       f"TP level : {tp_lvl}\n"
       f"P&L      : {sign}${ot['pnl']:,.2f}\n"
       f"Capital  : ${capital:,.2f}\n"
       f"Mode     : {mode}")

def tg_heartbeat(S):
    """Send periodic heartbeat status via Telegram."""
    sc  = S.get("start_capital") or Cfg.IC  # actual starting balance; fallback to IC
    # Prefer live equity; fall back to S["capital"] if live fetch unavailable
    equity = S.get("live_equity") or S["capital"]
    wallet = S.get("live_wallet") or S["capital"]
    free   = S.get("live_free", 0.0)
    used   = S.get("live_used", 0.0)
    upnl   = S.get("live_unrealized", 0.0)
    ret = (equity - sc) / sc * 100 if sc > 0 else 0.0
    mdd = (S["peak"] - equity) / S["peak"] * 100 if S["peak"] > 0 else 0
    mode = executor.get_mode_label()
    lines = [
        f"<b>\U0001f4ca Heartbeat #{S['checks']}</b>",
        f"Mode    : {mode}",
        f"Equity  : ${equity:,.2f} ({ret:+.2f}%)",
        f"Wallet  : ${wallet:,.2f} | Free ${free:,.2f} | Locked ${used:,.2f}",
        f"uPnL    : ${upnl:+,.2f}",
        f"Booked  : ${S['capital']:,.2f}   (closed P&L)",
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

    # Circuit breaker status
    cb = executor.circuit_breaker
    if cb.is_tripped():
        lines.append(f"\n<b>\U0001f534 CIRCUIT BREAKER ACTIVE</b>\n{cb.trip_reason}")
    else:
        lines.append(f"\nConsec losses: {cb.consecutive_losses}/{executor.MAX_CONSECUTIVE_LOSSES}")

    tg("\n".join(lines))

def tg_circuit_breaker(reason):
    """Send urgent Telegram alert when circuit breaker trips."""
    tg(f"<b>\U0001f6a8\U0001f6a8\U0001f6a8 CIRCUIT BREAKER TRIPPED \U0001f6a8\U0001f6a8\U0001f6a8</b>\n\n"
       f"{reason}\n\n"
       f"<b>NO NEW TRADES WILL BE PLACED.</b>\n"
       f"Existing positions remain open with server-side SLs.\n\n"
       f"To resume: set CIRCUIT_BREAKER_RESET=true in .env on the VPS, "
       f"then restart. Remove the var after trading resumes.")

def tg_exec_error(sym, action, error):
    """Send Telegram alert for execution errors."""
    tg(f"<b>\u26a0\ufe0f EXECUTION ERROR</b>\n"
       f"Symbol : {sym}\n"
       f"Action : {action}\n"
       f"Error  : {error}\n\n"
       f"⚠️ Check Binance Futures position manually!")


# ─── Execution integration helpers ───────────────────────────────────────────

def execute_entry(sym: str, result: dict) -> dict:
    """Execute a real entry order via executor.py.

    Args:
        sym: pair symbol (e.g. "BTCUSDT")
        result: signal dict from get_signal()

    Returns:
        Updated trade dict with execution details, or None if execution failed.

    🔴 RISK: This places real orders when TRADING_MODE=live
    """
    sig = result["sig"]
    entry_price = result["m"]["px"]
    sl_price = result["sl"]
    size = result["sz"]

    # Check circuit breaker before entry
    if not executor.is_execution_enabled():
        log.warning(f"{sym} SIGNAL {sig} blocked by circuit breaker")
        tg(f"\u26d4 {sym} {sig} signal blocked — circuit breaker active")
        return None

    log.info(f"EXECUTING {sig} {sym}: size={size:.6f} entry≈{entry_price:.4f} SL={sl_price:.4f}")

    exec_result = executor.open_position(
        symbol=sym,
        direction=sig,
        size=size,
        sl_price=sl_price,
        entry_price=entry_price,
    )

    if not exec_result["success"]:
        log.error(f"ENTRY FAILED for {sym}: {exec_result['error']}")
        tg_exec_error(sym, f"{sig} ENTRY", exec_result["error"])
        return None

    # Build trade dict — use actual fill price if available
    fill_price = exec_result["fill_price"] or entry_price
    fill_qty = exec_result["fill_qty"] or size

    # 🔴 FIX (Bug 1+2): re-anchor SL/TP/BE to actual fill price using ATR distance.
    # Storing signal-time SL with fill-time entry breaks the ATR-distance
    # invariant under slippage — on tight SL_MULT (1.5×ATR), even $50–100 of
    # slippage on BTC can flip SL to the WRONG SIDE of entry, causing instant
    # stop-out or "would trigger immediately" rejection from Binance.
    # Live failure 2026-04-25: SL was $22 ABOVE LONG entry due to this.
    atr_d = result["atr"]
    if sig == "LONG":
        new_sl  = fill_price - atr_d * Cfg.SL_MULT
        new_tp1 = fill_price + atr_d * Cfg.TP1_MULT
        new_tp2 = fill_price + atr_d * Cfg.TP2_MULT
        new_tp3 = fill_price + atr_d * Cfg.TP3_MULT
        new_be  = fill_price + atr_d * Cfg.SL_MULT
    else:  # SHORT
        new_sl  = fill_price + atr_d * Cfg.SL_MULT
        new_tp1 = fill_price - atr_d * Cfg.TP1_MULT
        new_tp2 = fill_price - atr_d * Cfg.TP2_MULT
        new_tp3 = fill_price - atr_d * Cfg.TP3_MULT
        new_be  = fill_price - atr_d * Cfg.SL_MULT

    new_ot = {
        "symbol":         sym,
        "dir":            sig,
        "entry":          fill_price,
        "sl":             new_sl,
        "tp1":            new_tp1,
        "tp2":            new_tp2,
        "tp3":            new_tp3,
        "be":             new_be,
        "size":           fill_qty,     # actual filled quantity
        "be_hit":         False,
        "tp1_hit":        False,
        "tp2_hit":        False,
        "tp3_hit":        False,
        "rem":            1.0,
        "pnl":            0.0,
        "bars":           0,
        "reason":         result["reason"],
        "open_time":      datetime.now(timezone.utc).isoformat(),
        # Execution tracking
        "exec_order_id":  exec_result["order_id"],
        "exec_sl_id":     exec_result["sl_order_id"],
        "exec_fill_price": fill_price,
    }

    # 🔴 FIX (Bug 1 cont.): server-side SL was placed at signal-time SL by
    # open_position. If slippage > 25% of ATR, refresh the server SL to the
    # fill-anchored value so Binance's stop matches the bot's tracked SL.
    # Sub-quarter-ATR slips skip the extra REST call.
    if abs(sl_price - new_sl) > atr_d * 0.25:
        slip = fill_price - entry_price
        log.warning(
            f"Slippage re-anchor: SL ${sl_price:.4f} → ${new_sl:.4f} "
            f"(slip ${slip:+.4f}, atr ${atr_d:.4f}). Refreshing server SL."
        )
        try:
            mv = executor.move_stop_loss(
                symbol=sym, direction=sig,
                new_sl_price=new_sl, remaining_qty=fill_qty,
            )
            if not mv["success"]:
                log.error(f"Slippage re-anchor SL move failed: {mv['error']}")
                tg_exec_error(sym, "SL RE-ANCHOR", mv["error"])
        except Exception as _e:
            log.error(f"Slippage re-anchor exception: {_e}")

    return new_ot


def execute_partial_tp(sym: str, ot: dict, tp_level: str, fraction: float) -> bool:
    """Execute a partial TP close on the exchange.

    Returns True on success, False on failure. On failure, the optimistic
    state mutations made by check_exits() are ROLLED BACK so the next bar
    can re-detect the TP hit and retry — preventing rem from diverging
    from actual on-exchange position size.

    🔴 FIX (Bug 6): previously ot["rem"]/ot["pnl"]/ot["tpN_hit"] were
    decremented in check_exits BEFORE exchange confirmed close. A failed
    close left rem at the smaller value while Binance still held 100% —
    causing dust accumulation, wrong tg_tp_hit displays, and wrong
    SL-update qty after partial.

    Args:
        sym: pair symbol
        ot: open trade dict
        tp_level: "TP1", "TP2", or "TP3"
        fraction: fraction to close (0.40, 0.30, etc.)
    """
    result = executor.close_partial(
        symbol=sym,
        direction=ot["dir"],
        fraction=fraction,
        total_size=ot["size"],
        reason=tp_level,
    )
    if not result["success"]:
        log.error(f"{tp_level} execution failed for {sym}: {result['error']}")
        tg_exec_error(sym, f"{tp_level} PARTIAL CLOSE", result["error"])
        # Roll back optimistic state mutations from check_exits()
        tp_key = tp_level.lower() + "_hit"      # "tp1_hit" / "tp2_hit" / "tp3_hit"
        tp_px_key = tp_level.lower()            # "tp1" / "tp2" / "tp3"
        if ot.get(tp_key):
            # Recompute and reverse the speculative pnl + rem decrement
            dirn = ot["dir"]
            tp_px = ot[tp_px_key]
            entry = ot["entry"]
            speculative_pnl = ((tp_px - entry) if dirn == "LONG" else (entry - tp_px)) * ot["size"] * fraction
            ot["pnl"] -= speculative_pnl
            ot["rem"] += fraction
            ot[tp_key] = False
            log.warning(
                f"{sym} {tp_level} state rolled back — rem→{ot['rem']*100:.0f}%, "
                f"pnl→${ot['pnl']:+.2f}. Will retry next bar if price still beyond TP."
            )
        return False

    # After partial close, update the server-side SL to match reduced qty
    remaining_qty = ot["size"] * ot["rem"]
    if remaining_qty > 0:
        sl_result = executor.update_sl_after_partial(
            symbol=sym,
            direction=ot["dir"],
            sl_price=ot["sl"],
            new_remaining_qty=remaining_qty,
        )
        if not sl_result["success"]:
            log.error(f"SL update after {tp_level} failed: {sl_result['error']}")
            tg_exec_error(sym, f"SL UPDATE after {tp_level}", sl_result["error"])
    return True


def execute_breakeven(sym: str, ot: dict) -> None:
    """Move the server-side stop-loss to breakeven (entry price)."""
    remaining_qty = ot["size"] * ot["rem"]
    result = executor.move_stop_loss(
        symbol=sym,
        direction=ot["dir"],
        new_sl_price=ot["entry"],
        remaining_qty=remaining_qty,
    )
    if not result["success"]:
        log.error(f"BE move failed for {sym}: {result['error']}")
        tg_exec_error(sym, "BREAKEVEN SL MOVE", result["error"])


def execute_full_close(sym: str, ot: dict, reason: str) -> bool:
    """Close entire position on exchange (SL hit, timeout, TP3).

    Returns True if the position is confirmed closed (or already gone).
    Returns False if the close attempt failed — caller MUST NOT pop the trade
    from open_trades; it will be retried next check cycle.

    🔴 RISK: Returning False means position is still open on Binance.
    The server-side SL (closePosition=True) remains the safety net.
    """
    # Cancel any remaining open orders (SL/limit) first
    executor.cancel_open_orders(sym)

    if reason == "SL" and not ot.get("be_hit"):
        # Server-side SL should have already filled. Check if position is still open.
        pos = executor.get_open_position(sym)
        if pos is None or pos["qty"] <= 0:
            log.info(f"{sym} SL already filled on exchange (server-side SL)")
            return True

    # Close whatever remains
    result = executor.close_full_position(sym, ot["dir"])
    if not result["success"]:
        log.error(f"FULL CLOSE failed for {sym}: {result['error']}")
        tg_exec_error(sym, f"FULL CLOSE ({reason})", result["error"])
        return False
    return True


# ─── Main loop ────────────────────────────────────────────────────────────────

def main():
    """Main bot loop with live execution integration."""
    mode = executor.get_mode_label()
    log.info(f"=== MTF BOT v4.1 — CHAMPION — BTC/ETH/SOL — {mode} ===")
    S = load_state()

    # Restore circuit breaker from state
    if S.get("circuit_breaker"):
        executor.circuit_breaker = executor.CircuitBreaker.from_dict(S["circuit_breaker"])
    # NOTE: reset_daily is called AFTER balance sync below, not here
    # Calling it here with stale S["capital"] caused 99% false drawdown trips

    # 🔴 FIX: Always reset consecutive_losses to 0 on every startup.
    # The counter was persisted in JSON and accumulated ACROSS restarts — pre-restart
    # losses carried forward, so one more real loss immediately tripped the CB.
    # consecutive_losses is now INFORMATIONAL ONLY (trip removed from executor.py).
    # Resetting here ensures the logged count reflects the current session only.
    executor.circuit_breaker.consecutive_losses = 0

    # 🔴 FIX: Auto-clear stale CB trips on startup.
    # Two scenarios that warrant auto-clear:
    #   (a) Consecutive-loss trip — trigger permanently removed from executor.py.
    #       These trips can never be legitimate anymore.
    #   (b) Daily-DD trip from a PREVIOUS UTC day — yesterday's drawdown cannot
    #       apply to today's trading.  A new day means a fresh daily baseline.
    if executor.circuit_breaker.tripped:
        reason = executor.circuit_breaker.trip_reason
        cb_date = executor.circuit_breaker.daily_start_date
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if "consecutive losses" in reason or "consecutive loss" in reason:
            log.warning(
                f"CB: Auto-clearing stale consecutive-loss trip (trigger permanently removed).\n"
                f"Old reason: {reason}"
            )
            executor.circuit_breaker.tripped = False
            executor.circuit_breaker.trip_reason = ""
            log.info("CB: Bot will resume trading. Consecutive-loss trigger is disabled.")
        elif cb_date and cb_date != today:
            log.warning(
                f"CB: Auto-clearing stale daily-DD trip from {cb_date} (today is {today}).\n"
                f"Old reason: {reason}"
            )
            executor.circuit_breaker.tripped = False
            executor.circuit_breaker.trip_reason = ""
            log.info("CB: Bot will resume trading on new UTC day.")

    log.info(f"State: checks={S['checks']} capital=${S['capital']:.2f} trades={len(S['trades'])}")

    tg(f"<b>\U0001f680 MTF Bot v4.1 CHAMPION started</b>\n"
       f"Mode     : {mode}\n"
       f"Leverage : {executor.LEVERAGE}x\n"
       f"Pairs    : {', '.join(PAIRS)}\n"
       f"TP 50/20/30% @ 4.5R / 7.2R / 30R\n"
       f"Session  : 06–22 UTC only\n"
       f"RSI mom  : 2-bar confirmation\n"
       f"1H RSI   : filter active\n"
       f"Capital  : ${S['capital']:,.2f}\n"
       f"Backtest : PF 18.13 | MaxDD 1.5% | WR 56%\n"
       f"Circuit  : DD {executor.DAILY_LOSS_LIMIT_PCT}% daily limit only "
       f"(consec-loss CB disabled — see executor.py)")

    # Initialise exchange connection on startup (live-only)
    try:
            executor._get_exchange()
            bal = executor.get_futures_balance()
            log.info(f"Futures wallet balance: ${bal:.2f} USDT")
            tg(f"\U0001f4b0 Futures balance: ${bal:.2f} USDT")
            # 🔴 RISK: Sync S["capital"] to real balance so position sizing
            # uses actual funds, not the virtual paper-mode IC ($10,000).
            # Without this, sizing produces ~0.56 BTC on a $100 account.
            if bal > 0:
                S["capital"] = bal
                # Fix stale IC peak: fresh_state() sets peak=IC ($10,000).
                # On first real sync, reset peak to actual balance so MaxDD
                # is not falsely reported as ~99%.  start_capital tracks the
                # true starting balance for accurate return % in heartbeats.
                if not S.get("start_capital"):
                    S["start_capital"] = bal   # first sync: record real baseline
                    S["peak"]          = bal   # reset stale IC peak to real balance
                else:
                    S["peak"] = max(S.get("peak", bal), bal)
                log.info(f"Capital synced from Binance: ${bal:.2f} | peak=${S['peak']:.2f} | start=${S['start_capital']:.2f}")

                # 🔴 FIX: Force CB daily baseline to real current balance on EVERY startup.
                # reset_daily() is date-gated — it won't update daily_start_capital if the
                # date hasn't changed. This means after a CB manual reset + same-day restart,
                # daily_start_capital remains stale (e.g. $114 pre-loss), and the drawdown
                # check re-trips the breaker immediately. Forcing it here prevents that.
                executor.circuit_breaker.daily_start_capital = bal
                executor.circuit_breaker.daily_start_date = (
                    datetime.now(timezone.utc).strftime("%Y-%m-%d")
                )
                log.info(f"CB daily baseline forced to real balance: ${bal:.2f}")

            # 🔴 FIX: Reconcile open_trades against actual Binance positions.
            # Phantom trade entries (positions closed by server-side SL while bot
            # was offline) cause consecutive phantom losses on first check cycle,
            # tripping the circuit breaker before any real trade executes.
            reconcile_open_trades(S)
            cleanup_orphan_sl_orders(S)  # 🔴 FIX: cancel stale SLs + re-place fresh ones

    except Exception as e:
        log.error(f"Exchange init failed: {e}")
        tg(f"\u274c Exchange init failed: {e}\nBot will retry on first trade.")

    # 🔴 RISK: Fallback — if Binance sync failed (e.g. IP not whitelisted yet),
    # seed start_capital from whatever is in the state file so the heartbeat
    # never reports the false 99% MaxDD caused by the stale IC peak ($10,000).
    if not S.get("start_capital") and S["capital"] > 0:
        S["start_capital"] = S["capital"]
        S["peak"]          = S["capital"]   # reset stale IC peak even without live sync
        log.info(f"Fallback: start_capital=${S['start_capital']:.2f} seeded from state file (Binance sync unavailable)")

    while True:
        loop_start = time.time()
        try:
            S["checks"] += 1
            log.info(f"=== Check #{S['checks']} ===")

            # Live account snapshot for display/telemetry AND sizing.
            acct = executor.get_futures_account_state()
            S["live_equity"]     = acct["equity"]
            S["live_wallet"]     = acct["wallet"]
            S["live_free"]       = acct["free"]
            S["live_used"]       = acct["used"]
            S["live_unrealized"] = acct["unrealized_pnl"]
            if acct["ok"] and acct["equity"] > 0:
                S["peak"] = max(S.get("peak", 0.0), acct["equity"])
                # 🔴 FIX (Bug 9): refresh S["capital"] from live equity at top
                # of every cycle so the sizing formula sees fresh wallet state.
                # Previously S["capital"] was only set at startup or after a
                # close — across a 3-pair cycle, partial closes mid-loop never
                # propagated to subsequent symbols' get_signal() calls.
                S["capital"] = acct["equity"]
            sc_disp = S.get("start_capital") or Cfg.IC
            eq_ret = ((acct["equity"] - sc_disp) / sc_disp * 100) if sc_disp > 0 else 0.0
            log.info(
                f"Equity: ${acct['equity']:.2f} ({eq_ret:+.2f}%) | "
                f"Wallet: ${acct['wallet']:.2f} | Free: ${acct['free']:.2f} | "
                f"Locked: ${acct['used']:.2f} | uPnL: ${acct['unrealized_pnl']:+.2f}"
            )

            # Reset daily circuit breaker tracking
            executor.circuit_breaker.reset_daily(S["capital"])

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

                    # ── Exit check ─────────────────────────────────
                    ot = S["open_trades"].get(sym)
                    if ot:
                        events, closed, c_reason, c_px = check_exits(ot, d5)

                        # Execute events on exchange — only notify Telegram on confirmed close
                        for ev in events:
                            if ev.startswith("TP1"):
                                if execute_partial_tp(sym, ot, "TP1", Cfg.TP1_FRAC):
                                    tg_tp_hit(sym, ot, "TP1")
                            elif ev.startswith("TP2"):
                                if execute_partial_tp(sym, ot, "TP2", Cfg.TP2_FRAC):
                                    tg_tp_hit(sym, ot, "TP2")
                            elif ev.startswith("TP3"):
                                # TP3 = full close, handled below
                                tg_tp_hit(sym, ot, "TP3")
                            elif ev == "BE_TRIGGERED":
                                execute_breakeven(sym, ot)
                                tg(f"<b>\U0001f6e1 {sym} — Breakeven triggered!</b>\n"
                                   f"SL moved to entry: ${ot['entry']:,.4f}")

                        if closed:
                            ot["close_reason"] = c_reason
                            # 🔴 RISK: Only book P&L and remove trade if close ACTUALLY succeeded.
                            # If close fails (e.g. sub-minimum qty, network), keep trade in
                            # open_trades — it will be retried next cycle. Server-side SL
                            # (closePosition=True) remains the safety net while retrying.
                            close_ok = execute_full_close(sym, ot, c_reason)
                            if not close_ok:
                                log.error(f"{sym} {c_reason} CLOSE FAILED — keeping in open_trades, will retry next cycle")
                                tg_exec_error(sym, f"{c_reason} RETRY PENDING",
                                              "Close failed. Will retry next cycle. Server-side SL active.")
                                # Increment bars so TIME logic re-triggers close next check
                                ot["bars"] = max(ot["bars"], Cfg.MAX_HOLD)
                            else:
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

                                # 🔴 RISK: Feed result to circuit breaker only on confirmed close
                                executor.circuit_breaker.record_trade(ot["pnl"], S["capital"])
                                if executor.circuit_breaker.is_tripped():
                                    tg_circuit_breaker(executor.circuit_breaker.trip_reason)
                        else:
                            ep = ((px - ot["entry"]) if ot["dir"]=="LONG"
                                  else (ot["entry"] - px)) * ot["size"] * ot["rem"]
                            log.info(f"{sym} HOLDING {ot['dir']} {ot['bars']}bars rem:{ot['rem']*100:.0f}% est:${ep:+.2f}")

                    # ── Entry check ────────────────────────────────
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
                            if executor.circuit_breaker.is_tripped():
                                # 🔴 RISK: CB active — block entry (trip already logged)
                                log.warning(
                                    f"{sym} Signal {sig} BLOCKED — circuit breaker active: "
                                    f"{executor.circuit_breaker.trip_reason}"
                                )
                            else:
                                # 🔴 RISK: Execute real order
                                new_ot = execute_entry(sym, result)
                                if new_ot is not None:
                                    S["open_trades"][sym] = new_ot
                                    S["signals"] += 1
                                    log.info(f"{sym} {sig} entry={new_ot['entry']:.2f} "
                                             f"TP1={new_ot['tp1']:.2f} TP2={new_ot['tp2']:.2f} "
                                             f"TP3={new_ot['tp3']:.2f} SL={new_ot['sl']:.2f}")
                                    tg_opened(sym, new_ot)
                                else:
                                    log.warning(f"{sym} {sig} signal generated but execution failed/blocked")
                    else:
                        log.info(f"{sym} ${px:,.2f} | in trade")

                except Exception as e:
                    log.error(f"{sym} error: {e}")

            save_state(S)
            if S["checks"] % 12 == 0:
                tg_heartbeat(S)

        except Exception as e:
            log.exception(f"Main loop error: {e}")
            tg(f"\u274c Bot error: {e}\nRetrying in 5 min...")

        elapsed = time.time() - loop_start
        time.sleep(max(5, Cfg.INTERVAL - elapsed))


if __name__ == "__main__":
    main()
