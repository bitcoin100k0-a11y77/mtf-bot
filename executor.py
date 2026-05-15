# executor.py — Binance Futures order execution for Champion v4.0
"""
Binance USD-M Futures execution layer.

Handles: market entries, stop-loss placement, partial TP exits,
breakeven SL moves, position queries, and circuit breaker logic.

⚠️ ENV REQUIRED: BINANCE_API_KEY, BINANCE_API_SECRET
🔴 RISK: This module places REAL orders when TRADING_MODE=live
"""

import os
import time
import math
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("Executor")


# ─── Configuration ────────────────────────────────────────────────────────────

# Bot is live-only — paper mode removed

# Get public IPv4 on startup and send to Telegram so it can be whitelisted on Binance
try:
    import urllib.request as _urlreq
    import json as _json
    _pub_ip = _urlreq.urlopen("https://api4.ipify.org", timeout=5).read().decode().strip()
    log.info(f"VPS public IPv4: {_pub_ip}")
    # Send IP to Telegram so it's visible for Binance whitelist verification
    _tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    _tg_chat  = os.getenv("TELEGRAM_CHAT_ID", "")
    if _tg_token and _tg_chat:
        _msg = f"🌐 VPS public IPv4: {_pub_ip}\n\nAdd this to Binance API key whitelist if not already done."
        _tg_url = f"https://api.telegram.org/bot{_tg_token}/sendMessage"
        _tg_data = _json.dumps({"chat_id": _tg_chat, "text": _msg}).encode()
        _req = _urlreq.Request(_tg_url, data=_tg_data, headers={"Content-Type": "application/json"})
        _urlreq.urlopen(_req, timeout=5)
        log.info("Public IP sent to Telegram.")
except Exception as _e:
    log.warning(f"Could not fetch/send public IP: {_e}")
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "5.0"))  # 🔴 RISK: halt after 5% daily DD
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "6"))   # 🔴 RISK: halt after 6 losses in a row (raised from 3 — 3 was too tight for 3-pair live bot)
LEVERAGE = int(os.getenv("FUTURES_LEVERAGE", "1"))  # 🔴 RISK: default 1x, no leverage


# ─── Exchange singleton ──────────────────────────────────────────────────────

_exchange = None

def _get_exchange():
    """Lazily initialise and return the CCXT Binance Futures exchange object."""
    global _exchange
    if _exchange is not None:
        return _exchange

    try:
        import ccxt
    except ImportError as e:
        raise RuntimeError(
            "ccxt not installed. Run: pip install ccxt"
        ) from e

    api_key = os.environ.get("BINANCE_API_KEY", "")      # ⚠️ ENV REQUIRED
    api_secret = os.environ.get("BINANCE_API_SECRET", "")  # ⚠️ ENV REQUIRED

    if not api_key or not api_secret:
        raise RuntimeError(
            "BINANCE_API_KEY and BINANCE_API_SECRET must be set. "
            "Cannot initialise executor without credentials."
        )

    _exchange = ccxt.binance({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,       # respect Binance rate limits automatically
        "options": {
            "defaultType": "future",   # USD-M Futures
            "adjustForTimeDifference": True,
        },
    })

    # Load market metadata (precision, min qty, etc.)
    _exchange.load_markets()
    log.info(f"Exchange initialised: Binance Futures | Markets loaded: {len(_exchange.markets)}")

    return _exchange


# ─── Hedge-mode probe ────────────────────────────────────────────────────────

_HEDGE_MODE: Optional[bool] = None


def _get_hedge_mode(force_reprobe: bool = False) -> bool:
    """Detect whether the Futures account runs in Hedge Mode (dualSidePosition).

    Cached after first successful probe. Defaults to False on probe failure so
    that orders placed without positionSide continue to work for one-way accounts.

    🟢 v11 Fix O-3: pass `force_reprobe=True` to bypass the cache. Used by
    validate_position_side() to re-confirm after a placement looks suspicious.
    """
    global _HEDGE_MODE
    if force_reprobe:
        _HEDGE_MODE = None
    if _HEDGE_MODE is None:
        try:
            ex = _get_exchange()
            probe = getattr(ex, "fapiPrivateGetPositionSideDual", None)
            if probe:
                resp = probe()
                _HEDGE_MODE = bool(resp.get("dualSidePosition", False))
            else:
                _HEDGE_MODE = False
            log.info(f"Hedge mode detected: {_HEDGE_MODE}")
        except Exception as e:
            log.warning(f"Hedge mode probe failed ({e}); assuming one-way")
            _HEDGE_MODE = False
    return _HEDGE_MODE


def _position_side_for(direction: str) -> Optional[str]:
    """Return 'LONG'/'SHORT' if hedge mode; None for one-way."""
    if not _get_hedge_mode():
        return None
    return "LONG" if direction == "LONG" else "SHORT"


def validate_position_side(symbol: str = "BTCUSDT") -> dict:
    """🟢 v11 Fix O-3: hedge-mode mismatch fail-fast.

    Live root-cause hypothesis: cached `_HEDGE_MODE=False` (because the
    initial probe failed at boot) on an account that ACTUALLY is in hedge
    mode (`dualSidePosition=True`). All orders then go without
    `positionSide` field → Binance accepts (returns id) then internally
    rejects/cancels because hedge mode requires `positionSide`. The
    stored sl_id later returns -2013 "Order does not exist" because the
    order never persisted.

    This helper:
      1. Forces re-probe of `fapiPrivateGetPositionSideDual`
      2. Checks any current position's `positionSide` field — if account
         is dual-side but a position shows `BOTH`, there's a config drift
      3. Returns a structured result:
            {"ok": bool, "hedge": bool, "drift": bool, "detail": str}

    Caller (bot.py main()) should:
      - On `ok=False`: log CRITICAL, send Telegram alert, halt new entries
      - On `drift=True`: log warning + force re-probe but continue

    Idempotent. Safe to call repeatedly. No side effects beyond log lines.
    """
    global _HEDGE_MODE
    result = {"ok": True, "hedge": False, "drift": False, "detail": ""}
    try:
        ex = _get_exchange()
        # Force-reprobe: bypass cache to get fresh hedge mode setting
        probe = getattr(ex, "fapiPrivateGetPositionSideDual", None)
        if not probe:
            result["detail"] = "fapiPrivateGetPositionSideDual unavailable on this ccxt version"
            log.warning(f"validate_position_side: {result['detail']}")
            return result

        resp = probe()
        fresh_hedge = bool(resp.get("dualSidePosition", False))
        cached_hedge = _HEDGE_MODE
        result["hedge"] = fresh_hedge

        # Update cache to truth
        if cached_hedge != fresh_hedge:
            log.critical(
                f"validate_position_side: HEDGE MODE CACHE MISMATCH — "
                f"cached={cached_hedge} fresh={fresh_hedge}. Updating cache."
            )
            _HEDGE_MODE = fresh_hedge
            result["drift"] = True
            result["detail"] = (
                f"hedge mode cache was {cached_hedge}, real value is {fresh_hedge}"
            )

        # Sanity-check current positions vs hedge setting
        try:
            ccxt_sym = _symbol_to_ccxt(symbol)
            positions = ex.fetch_positions([ccxt_sym])
            for pos in positions:
                qty = abs(float(pos.get("contracts", 0)))
                if qty <= 0:
                    continue
                info = pos.get("info") or {}
                pos_side = info.get("positionSide", "")
                if fresh_hedge and pos_side == "BOTH":
                    result["ok"] = False
                    result["detail"] = (
                        f"HEDGE MISMATCH: account is dualSidePosition=True but "
                        f"position {symbol} has positionSide=BOTH. Orders will "
                        f"be silently rejected. Reconfigure account."
                    )
                    log.critical(f"validate_position_side: {result['detail']}")
                    return result
                if not fresh_hedge and pos_side in ("LONG", "SHORT"):
                    result["ok"] = False
                    result["detail"] = (
                        f"HEDGE MISMATCH: account is one-way (dualSidePosition=False) "
                        f"but position {symbol} has positionSide={pos_side}. "
                        f"Reconfigure account."
                    )
                    log.critical(f"validate_position_side: {result['detail']}")
                    return result
        except Exception as pe:
            log.warning(
                f"validate_position_side: position sanity-check raised: {pe} "
                f"(non-fatal; ok={result['ok']})"
            )

        log.info(
            f"validate_position_side: ok={result['ok']} hedge={fresh_hedge} "
            f"drift={result['drift']}"
        )
        return result
    except Exception as e:
        result["ok"] = False
        result["detail"] = f"validate_position_side raised: {e}"
        log.error(f"validate_position_side fatal: {e}")
        return result


def _init_leverage(symbol: str) -> None:
    """Set leverage for a symbol. Called once per symbol on first trade."""
    ex = _get_exchange()
    try:
        ex.set_leverage(LEVERAGE, symbol)
        log.info(f"{symbol} leverage set to {LEVERAGE}x")
    except Exception as e:
        # Some symbols may not support leverage change; log and continue
        log.warning(f"{symbol} set_leverage failed (may already be set): {e}")


# ─── Precision helpers ────────────────────────────────────────────────────────

def _round_qty(symbol: str, qty: float) -> float:
    """Round quantity to exchange precision to avoid Binance rejection.

    CCXT returns precision.amount in two possible modes:
    - TICK_SIZE mode:      float < 1, e.g. 0.001 (the step size itself)
    - DECIMAL_PLACES mode: integer >= 1, e.g. 3 (number of decimal places)
    Binance Futures uses TICK_SIZE mode, so 10**precision gives ~1.002
    which causes math.floor(0.567 * 1.002) = 0 — wrong. Detect and handle both.
    """
    ex = _get_exchange()
    market = ex.market(symbol)
    precision = market.get("precision", {}).get("amount", 8)
    # Use floor to never exceed available balance
    if isinstance(precision, float) and precision < 1:
        # TICK_SIZE mode: precision IS the step size (e.g. 0.001 for BTCUSDT)
        step = precision
        return math.floor(qty / step) * step
    else:
        # DECIMAL_PLACES mode: precision is the number of decimal places
        factor = 10 ** int(precision)
        return math.floor(qty * factor) / factor


def _round_price(symbol: str, price: float) -> float:
    """Round price to exchange tick size using ccxt's price_to_precision.

    🔴 FIX ('verify-fail'): the prior implementation read
    `precision = market.get('precision', {}).get('price', 8)` and computed
    `factor = 10 ** precision`. That worked when CCXT reported `precision.price`
    as an integer digit count, but BROKE in two scenarios:

      1. CCXT in TICK_SIZE mode returns a float (e.g. 0.10 for BTCUSDT).
         `10 ** 0.10 = 1.2589` → garbage rounding (price snapped to wrong tick).
      2. `precision.price` missing entirely → fallback `8` → `factor = 1e8`,
         essentially no rounding. Binance accepts the unrounded value, silently
         snaps to the real tick, but `_verify_sl_placed` then fails to match
         the snapped price → emergency-close on a legitimately placed SL.

    `ex.price_to_precision()` queries the same market metadata but uses CCXT's
    internal precision-mode logic (TICK_SIZE vs DECIMAL_PLACES vs SIGNIFICANT_DIGITS)
    and returns a string already aligned to the exchange's tick. We cast back
    to float for downstream consumers that expect numeric type.

    Accepts both bot-format ("BTCUSDT") and CCXT-format ("BTC/USDT:USDT") symbols.
    """
    ex = _get_exchange()
    ccxt_sym = symbol if "/" in symbol else _symbol_to_ccxt(symbol)
    return float(ex.price_to_precision(ccxt_sym, price))


def _symbol_to_ccxt(symbol: str) -> str:
    """Convert bot symbol format (BTCUSDT) to CCXT format (BTC/USDT:USDT)."""
    # Handle common pairs
    for base in ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA"]:
        if symbol.startswith(base) and symbol.endswith("USDT"):
            return f"{base}/USDT:USDT"
    # Fallback: try to split at USDT
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}/USDT:USDT"
    raise ValueError(f"Cannot convert symbol {symbol} to CCXT format")


# ─── Account queries ─────────────────────────────────────────────────────────

def get_futures_balance() -> float:
    """Get available USDT balance in Futures wallet.

    Returns:
        float: Available USDT balance, or 0.0 on error.
    """
    try:
        ex = _get_exchange()
        balance = ex.fetch_balance()
        usdt = balance.get("USDT", {})
        free = float(usdt.get("free", 0.0))
        log.info(f"Futures balance: {free:.2f} USDT")
        return free
    except Exception as e:
        log.error(f"Failed to fetch Futures balance: {e}")
        return 0.0


def get_futures_account_state() -> dict:
    """Full Futures account snapshot — for display / telemetry.

    Returns dict with keys:
      wallet         (float) — wallet balance, USDT, no unrealized
      free           (float) — available USDT (Binance availableBalance)
      used           (float) — locked margin = wallet - free (cross)
      unrealized_pnl (float) — sum of unrealizedPnl across all open positions
      equity         (float) — wallet + unrealized_pnl  (== Binance totalMarginBalance)
      ok             (bool)  — False on fetch error; callers may fall back

    Do NOT use this for margin pre-flight — use get_futures_balance() (free only).
    """
    try:
        ex = _get_exchange()
        balance = ex.fetch_balance()
        usdt = balance.get("USDT", {}) or {}
        wallet = float(usdt.get("total", 0.0) or 0.0)
        free   = float(usdt.get("free",  0.0) or 0.0)
        used   = float(usdt.get("used",  max(wallet - free, 0.0)) or 0.0)
        unrealized = 0.0
        try:
            for pos in ex.fetch_positions():
                qty = abs(float(pos.get("contracts", 0) or 0))
                if qty > 0:
                    unrealized += float(pos.get("unrealizedPnl", 0) or 0)
        except Exception as pe:
            log.warning(f"fetch_positions failed in equity calc ({pe}) - equity=wallet only")
        equity = wallet + unrealized
        return {
            "wallet": wallet, "free": free, "used": used,
            "unrealized_pnl": unrealized, "equity": equity,
            "ok": True,
        }
    except Exception as e:
        log.error(f"get_futures_account_state failed: {e}")
        return {
            "wallet": 0.0, "free": 0.0, "used": 0.0,
            "unrealized_pnl": 0.0, "equity": 0.0,
            "ok": False,
        }


def get_open_position(symbol: str) -> Optional[dict]:
    """Check if there is an open position on Binance for this symbol.

    Returns:
        dict with keys: side ('long'/'short'), qty, entry_price, unrealized_pnl
        or None if no position.
    """
    try:
        ex = _get_exchange()
        ccxt_sym = _symbol_to_ccxt(symbol)
        positions = ex.fetch_positions([ccxt_sym])
        for pos in positions:
            qty = abs(float(pos.get("contracts", 0)))
            if qty > 0:
                return {
                    "side": pos.get("side", "long"),
                    "qty": qty,
                    "entry_price": float(pos.get("entryPrice", 0)),
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                }
        return None
    except Exception as e:
        log.error(f"Failed to fetch position for {symbol}: {e}")
        return None


# ─── Order execution ─────────────────────────────────────────────────────────

def open_position(
    symbol: str,
    direction: str,
    size: float,
    sl_price: float,
    entry_price: float,
) -> dict:
    """Open a Futures position with a server-side stop-loss.

    Args:
        symbol: e.g. "BTCUSDT"
        direction: "LONG" or "SHORT"
        size: position size in base asset (e.g. 0.001 BTC)
        sl_price: stop-loss price
        entry_price: expected entry price (for logging)

    Returns:
        dict: {"success": bool, "order_id": str|None, "sl_order_id": str|None,
               "fill_price": float|None, "fill_qty": float|None, "error": str|None}

    📋 TEST THIS: Verify with a tiny position before full deployment.
    """
    ccxt_sym = _symbol_to_ccxt(symbol)
    side = "buy" if direction == "LONG" else "sell"
    sl_side = "sell" if direction == "LONG" else "buy"
    qty = _round_qty(ccxt_sym, size)
    sl_px = _round_price(ccxt_sym, sl_price)

    result = {
        "success": False, "order_id": None, "sl_order_id": None,
        "fill_price": None, "fill_qty": None, "error": None,
    }

    if qty <= 0:
        result["error"] = f"Quantity rounds to 0 for {symbol} (raw: {size})"
        log.error(result["error"])
        return result

    # ── Live execution ──
    ex = _get_exchange()
    _init_leverage(symbol)

    # 🔴 FIX (-2019 Margin insufficient): pre-flight margin check.
    # Strategy sizing (capital × RISK_PCT) / (atr × SL_MULT) caps loss-if-SL-hits
    # but has ZERO coupling to available margin. On small accounts (~$100) with
    # BTC ~$100k at FUTURES_LEVERAGE=1, notional can easily exceed balance and
    # Binance rejects the order with -2019. Check before spending API budget.
    # Buffer: 5% headroom for slippage between signal price and market fill,
    # plus ~0.04% taker fee on entry + ~0.04% on eventual exit.
    try:
        available = get_futures_balance()
        leverage = max(LEVERAGE, 1)
        # 🔴 FIX (-2019): 12% headroom — covers taker fees (~0.08% round-trip),
        # slippage on market fills (1-2% on 5m bar range for volatile pairs),
        # and Binance's per-symbol initial-margin-ratio quirks on small accounts.
        # Synced with bot.py Layer-A 88% — eliminates double-shrink between layers.
        margin_buffer = 0.88
        max_affordable_notional = available * leverage * margin_buffer
        notional = qty * float(entry_price)

        if notional > max_affordable_notional:
            # 🔴 FIX (margin-cap): auto-resize down instead of skip — Layer B
            # safety net for cases where bot.py's `capital` snapshot was stale
            # or LEVERAGE env changed mid-run. Source-side cap (bot.py) should
            # catch most cases; this is the belt-and-suspenders execution gate.
            new_qty = _round_qty(ccxt_sym, max_affordable_notional / float(entry_price))
            if new_qty <= 0:
                result["error"] = (
                    f"Wallet too small even after auto-resize "
                    f"(available ${available:.2f}, leverage {leverage}x, "
                    f"min qty for {symbol} > affordable). "
                    f"Fund wallet or raise FUTURES_LEVERAGE."
                )
                log.warning(result["error"])
                return result
            log.warning(
                f"Auto-resized {direction} {symbol}: "
                f"{qty} → {new_qty} (notional ${notional:.2f} → "
                f"${new_qty * float(entry_price):.2f}, "
                f"available ${available:.2f} @ {leverage}x)"
            )
            qty = new_qty
            notional = qty * float(entry_price)
            required_margin = notional / leverage
        else:
            required_margin = notional / leverage

        log.info(
            f"Margin OK: need ${required_margin:.2f} / have ${available:.2f} "
            f"(notional ${notional:.2f} @ {leverage}x)"
        )
    except Exception as _mc_err:
        # Margin check is advisory — if it fails (e.g. balance API hiccup),
        # fall through to the actual order; Binance will still reject with -2019
        # if truly under-margined and the caller will see that error.
        log.warning(f"Pre-flight margin check skipped ({_mc_err}) — proceeding with order")

    # 1) Market entry order
    try:
        log.info(f"PLACING {direction} {qty} {ccxt_sym} MARKET")
        entry_order = ex.create_order(
            symbol=ccxt_sym,
            type="market",
            side=side,
            amount=qty,
        )
        order_id = entry_order.get("id", "unknown")
        fill_price = float(entry_order.get("average", 0) or entry_order.get("price", 0) or entry_price)
        fill_qty = float(entry_order.get("filled", qty))
        log.info(f"FILLED {direction} {fill_qty} {symbol} @ {fill_price:.4f} (order: {order_id})")
        result.update({
            "success": True, "order_id": order_id,
            "fill_price": fill_price, "fill_qty": fill_qty,
        })
    except Exception as e:
        result["error"] = f"Entry order failed: {e}"
        log.error(result["error"])
        return result

    # 2) Server-side stop-loss via retry helper
    # 🔴 FIX (-4130 v2): closePosition tracker on Binance lags 500ms-1500ms+
    # after cancel. Even with min_wait_after_cancel and 3 retries, the race
    # wins under load — placing a fresh closePosition SL on a stale tracker
    # gets rejected with -4130 every time. reduceOnly STOP_MARKET orders use
    # a SEPARATE ledger and never touch the closePosition tracker. Position
    # fill_qty is known exactly here, so reduceOnly is the safer primary.
    # The helper auto-falls-back to closePosition for sub-min-lot dust.
    sl_result = _place_reduceonly_sl_with_retry(
        symbol=symbol,
        sl_side=sl_side,
        stop_price=sl_px,
        qty=fill_qty,
        max_attempts=3,
    )
    # 🔴 FIX (-2022/-4130 cross-fallback): if primary path (reduceOnly) failed,
    # try the OTHER path (closePosition) once before emergency-closing the
    # entry. -4130 only affects closePosition; -2022 only affects reduceOnly.
    # Cancel was already run inside the failed primary helper so no orphan
    # orders remain. Cross-helper attempt is independent. Cheaper than
    # eating fees on emergency rollback.
    #
    # 🔴 FIX (v3 Fix 3): SKIP closePosition fallback when:
    #   (a) last error contains -4130 → closePosition will fail with the SAME
    #       -4130 error. Don't burn 2 attempts + ~6s of latency for nothing.
    #   (b) error is verify-related AND we have a sl_order_id → reduceOnly may
    #       actually be placed server-side. Falling back would cancel it via
    #       the closePosition helper's cancel_open_orders() at start. Don't
    #       destroy our own working SL. (Fix 2 v3 above already handles the
    #       common "order exists, verify missed" case via diag-status, but
    #       this is the safety net for any remaining edge cases.)
    if not sl_result["success"]:
        err_str = sl_result.get("error", "") or ""
        skip_cp_fallback = (
            "4130" in err_str
            or ("verification poll" in err_str and sl_result.get("sl_order_id"))
        )
        if skip_cp_fallback:
            reason = "-4130 conflict" if "4130" in err_str else "order may exist server-side"
            log.warning(
                f"{symbol} skipping closePosition fallback — "
                f"won't help (reason: {reason}). "
                f"primary error: {err_str}"
            )
        else:
            log.warning(
                f"{symbol} primary SL path failed: {sl_result['error']} "
                f"— attempting closePosition fallback"
            )
            fallback = _place_closeposition_sl_with_retry(
                symbol=symbol,
                sl_side=sl_side,
                stop_price=sl_px,
                qty=fill_qty,
                max_attempts=2,
            )
            if fallback["success"]:
                log.info(
                    f"{symbol} closePosition fallback SUCCEEDED — entry protected"
                )
                sl_result = fallback

    if sl_result["success"]:
        result["sl_order_id"] = sl_result["sl_order_id"]
        # 🟢 v11 Fix O-1: propagate clientOrderId so bot.py can use it as
        # fallback identifier when orderId returns -2013 in verify path.
        result["sl_client_oid"] = sl_result.get("sl_client_oid")
        # 🟢 FIX (v4 Fix A): propagate sl_unverified flag to caller.
        # bot.py reads this to schedule a 60s re-verify (Fix F).
        if sl_result.get("sl_unverified"):
            result["sl_unverified"] = True

        # 🟢 v11 Fix O-6: place DEADMAN-SWITCH backup SL at 5% beyond primary.
        # If Binance loses the primary SL (the -2013 root-cause path), the
        # deadman fires on catastrophic move and closes the position via
        # closePosition=True (auto-sizes to current position).
        # closePosition auto-cancels server-side when position size → 0,
        # so deadman doesn't linger after a clean primary-SL hit.
        try:
            deadman_pct = 0.05  # 5% beyond primary
            if direction == "LONG":
                deadman_stop = _round_price(ccxt_sym, sl_px * (1.0 - deadman_pct))
            else:
                deadman_stop = _round_price(ccxt_sym, sl_px * (1.0 + deadman_pct))
            deadman_id = _place_deadman_sl(
                symbol, sl_side, deadman_stop, direction, fill_qty
            )
            if deadman_id:
                result["deadman_sl_id"] = deadman_id
                log.info(
                    f"{symbol} DEADMAN SL placed: id={deadman_id} "
                    f"stop={deadman_stop} (primary SL @ {sl_px}, +5% buffer)"
                )
            else:
                log.warning(
                    f"{symbol} DEADMAN SL placement returned no id — "
                    f"trade proceeds without backup. Primary SL is sole guard."
                )
        except Exception as dme:
            log.warning(
                f"{symbol} DEADMAN SL placement raised: {dme} — "
                f"trade proceeds without backup."
            )
    else:
        # 🔴 FIX (Bug 3): SL failed after 3 retries → entry is naked.
        # Old behavior left the position unguarded with success=True. New:
        # emergency-close the entry market-side. We eat ~0.04% taker fee
        # rather than risk an uncapped move. If emergency close ALSO fails,
        # log loud and surface a NAKED warning to caller for manual action.
        log.error(f"CRITICAL: SL placement failed: {sl_result['error']}")
        log.error(f"EMERGENCY-CLOSING {direction} {fill_qty} {symbol}")
        try:
            close_side = "sell" if direction == "LONG" else "buy"
            ex.create_order(
                symbol=ccxt_sym, type="market", side=close_side,
                amount=fill_qty, params={"reduceOnly": True},
            )
            log.warning(
                f"Emergency-close OK for {direction} {fill_qty} {symbol}; "
                f"entry rolled back due to SL failure"
            )
            result["success"] = False
            result["error"] = (
                f"SL placement failed; emergency-closed entry. "
                f"Original error: {sl_result['error']}"
            )
            return result
        except Exception as ec:
            log.error(f"EMERGENCY CLOSE FAILED: {ec}")
            log.error(
                f"NAKED POSITION: {direction} {fill_qty} {symbol} — "
                f"SL failed AND emergency close failed. Manual action required."
            )
            result["error"] = (
                f"NAKED POSITION — SL failed AND emergency close failed: "
                f"{sl_result['error']} | {ec}"
            )
            # Keep success=True so caller tracks the position for retry on
            # next loop. Server-side recovery via cleanup_orphan_sl_orders.
            result["success"] = True
            return result

    return result


def close_partial(
    symbol: str,
    direction: str,
    fraction: float,
    total_size: float,
    reason: str = "TP",
) -> dict:
    """Close a fraction of an open position (for partial TP exits).

    Args:
        symbol: e.g. "BTCUSDT"
        direction: "LONG" or "SHORT" — the open position direction
        fraction: fraction to close (e.g. 0.40 for 40%)
        total_size: the ORIGINAL full position size
        reason: label for logging (TP1, TP2, TP3)

    Returns:
        dict: {"success": bool, "fill_price": float|None, "fill_qty": float|None, "error": str|None}
    """
    ccxt_sym = _symbol_to_ccxt(symbol)
    # To close a LONG, we sell; to close a SHORT, we buy
    close_side = "sell" if direction == "LONG" else "buy"

    result = {"success": False, "fill_price": None, "fill_qty": None, "error": None}

    try:
        ex = _get_exchange()

        # 🔴 FIX -2022 (Bug 1): pre-flight position check using ACTUAL exchange qty.
        # Bot's `total_size * fraction` may exceed remaining position qty after
        # entry slippage, prior partial fills, or auto-deleveraging. Binance
        # rejects any reduceOnly with qty > current position with -2022.
        pos = get_open_position(symbol)
        if pos is None or pos["qty"] <= 0:
            log.warning(
                f"{symbol}: no position on exchange — {reason} treated as already-closed"
            )
            result.update({"success": True, "fill_price": 0.0, "fill_qty": 0.0})
            return result

        actual_qty = pos["qty"]
        desired_qty = total_size * fraction
        # Cap close at actual remaining; floor-round to step size.
        qty = _round_qty(ccxt_sym, min(desired_qty, actual_qty))

        if qty <= 0:
            result["error"] = (
                f"{reason} close qty rounds to 0 (desired {desired_qty:.6f}, "
                f"actual {actual_qty:.6f})"
            )
            log.warning(result["error"])
            return result

        # 🔴 FIX -2022 (Bug 1): include positionSide for hedge mode.
        # In dualSidePosition=True accounts, Binance requires positionSide on
        # every reduceOnly order to disambiguate LONG-side vs SHORT-side.
        # Missing it → -2022 ReduceOnly Order is rejected.
        params = {"reduceOnly": True}
        ps = _position_side_for(direction)
        if ps:
            params["positionSide"] = ps

        log.info(
            f"{reason}: closing {fraction*100:.0f}% → {close_side} {qty} {ccxt_sym} "
            f"(actual_pos={actual_qty:.6f}, hedge={_get_hedge_mode()})"
        )
        order = ex.create_order(
            symbol=ccxt_sym,
            type="market",
            side=close_side,
            amount=qty,
            params=params,
        )
        fill_price = float(order.get("average", 0) or order.get("price", 0))
        fill_qty = float(order.get("filled", qty))
        log.info(f"{reason} FILLED: {fill_qty} @ {fill_price:.4f}")
        result.update({"success": True, "fill_price": fill_price, "fill_qty": fill_qty})
    except Exception as e:
        result["error"] = f"{reason} close failed: {e}"
        log.error(result["error"])

    return result


def close_full_position(symbol: str, direction: str) -> dict:
    """Close entire remaining position (for SL hit, timeout, TP3).

    Handles sub-minimum dust positions (left after partial TP closes) via a
    closePosition=True STOP_MARKET at an aggressively-priced trigger — the same
    proven approach used for server-side SL orders.

    Args:
        symbol: e.g. "BTCUSDT"
        direction: "LONG" or "SHORT"

    Returns:
        dict: {"success": bool, "fill_price": float|None, "error": str|None}
    """
    ccxt_sym = _symbol_to_ccxt(symbol)
    close_side = "sell" if direction == "LONG" else "buy"

    result = {"success": False, "fill_price": None, "error": None}

    try:
        ex = _get_exchange()
        # Fetch current position directly from Binance for exact quantity
        pos = get_open_position(symbol)
        if pos is None or pos["qty"] <= 0:
            log.warning(f"No open position found for {symbol} — nothing to close")
            result.update({"success": True, "fill_price": 0.0})
            return result

        qty = _round_qty(ccxt_sym, pos["qty"])

        if qty <= 0:
            # Qty rounds to zero — pure dust, no order possible
            log.warning(f"{symbol}: rounded qty = 0 (raw {pos['qty']:.6f}) — dust position, treating as closed")
            result.update({"success": True, "fill_price": pos.get("entry_price", 0.0)})
            return result

        # ── Check against exchange minimum lot size ──────────────────────────
        market_info = ex.market(ccxt_sym)
        limits = (market_info.get("limits") or {})
        min_qty = float((limits.get("amount") or {}).get("min") or 0.0)

        if min_qty > 0 and qty < min_qty:
            # 🔴 FIX: position is below Binance minimum lot size (dust after partial TPs).
            # Regular market order with `amount=qty` is rejected with -1111.
            # Fallback: STOP_MARKET + closePosition=True, same as server-side SL.
            # Trigger price is set 0.5% away from current market — fires on next tick.
            log.warning(
                f"{symbol} dust position {qty:.6f} < min {min_qty:.4f} — "
                f"falling back to closePosition STOP_MARKET"
            )
            try:
                ticker = ex.fetch_ticker(ccxt_sym)
                cur_px = float(ticker["last"])
            except Exception as te:
                log.warning(f"Ticker fetch failed for {symbol}: {te} — using entry price as reference")
                cur_px = pos.get("entry_price", 1.0)

            # LONG close (SELL STOP_MARKET): triggers when price drops TO stopPrice
            # → set 0.5% below current; fires on any small dip, typically within seconds
            # SHORT close (BUY STOP_MARKET): triggers when price rises TO stopPrice
            # → set 0.5% above current; same logic
            if direction == "LONG":
                stop_px = _round_price(ccxt_sym, cur_px * 0.995)
            else:
                stop_px = _round_price(ccxt_sym, cur_px * 1.005)

            # 🔴 FIX (-4130): dust close uses closePosition=True — same race as any SL.
            # Must cancel existing SL first; then place with retry via the shared helper.
            log.info(f"DUST CLOSE: {close_side} closePosition {ccxt_sym} @ stop={stop_px} (cur={cur_px:.4f})")
            dust_result = _place_closeposition_sl_with_retry(
                symbol=symbol,
                sl_side=close_side,
                stop_price=stop_px,
                qty=pos["qty"],
                max_attempts=3,
            )
            if dust_result["success"]:
                log.info(f"DUST CLOSE placed: {dust_result['sl_order_id']} @ stop={stop_px}")
                result.update({"success": True, "fill_price": cur_px})
            else:
                log.error(f"DUST CLOSE failed: {dust_result['error']}")
                result["error"] = dust_result["error"]
            return result

        # ── Normal close — position is above minimum lot size ────────────────
        log.info(f"FULL CLOSE: {close_side} {qty} {ccxt_sym}")
        order = ex.create_order(
            symbol=ccxt_sym,
            type="market",
            side=close_side,
            amount=qty,
            params={"reduceOnly": True},
        )
        fill_price = float(order.get("average", 0) or order.get("price", 0))
        log.info(f"FULL CLOSE FILLED @ {fill_price:.4f}")
        result.update({"success": True, "fill_price": fill_price})
    except Exception as e:
        result["error"] = f"Full close failed: {e}"
        log.error(result["error"])

    return result


def cancel_open_orders(symbol: str) -> bool:
    """Cancel all open orders for a symbol (used before closing position).

    🔴 FIX (-4130 race): atomic server-side cancellation + verification loop.
    Prior iterate-and-cancel + sleep(0.3) caused two failure modes:
      (a) Silent per-order cancel failure → old SL survives → -4130 on replace.
      (b) Binance's "existing closePosition in direction" tracker had up to ~1s
          propagation lag, losing the race against sleep(0.3).
    Now: one DELETE /fapi/v1/allOpenOrders call, then poll until open_orders is
    empty (max ~1.5s). If polling still shows orders, log CRITICAL so BE moves
    can see a truthful signal and back off instead of blindly placing.

    Returns:
        bool: True if open_orders is confirmed empty, False otherwise.
    """
    try:
        ex = _get_exchange()
        ccxt_sym = _symbol_to_ccxt(symbol)

        # 1) Atomic server-side cancel — single request, no per-order silent fails.
        try:
            ex.cancel_all_orders(ccxt_sym)
            log.info(f"cancel_all_orders sent for {symbol}")
        except Exception as e:
            # Fall back to iterate-and-cancel if the atomic endpoint misbehaves.
            log.warning(f"cancel_all_orders failed for {symbol}: {e} — falling back to iterate")
            try:
                for order in ex.fetch_open_orders(ccxt_sym):
                    try:
                        ex.cancel_order(order["id"], ccxt_sym)
                        log.info(f"Cancelled order {order['id']} for {symbol}")
                    except Exception as ce:
                        log.warning(f"Failed to cancel order {order['id']}: {ce}")
            except Exception as fe:
                log.error(f"Iterate-cancel fallback also failed for {symbol}: {fe}")
                return False

        # 2) Verify — poll until open_orders truly empty or timeout (~1.5s).
        # Binance's internal tracker that powers -4130 checks takes time to settle.
        deadline = time.time() + 1.5
        while time.time() < deadline:
            try:
                remaining = ex.fetch_open_orders(ccxt_sym)
            except Exception as pe:
                log.warning(f"Post-cancel poll failed for {symbol}: {pe}")
                remaining = None
            if remaining == []:
                log.info(f"Cancel verified: 0 open orders remain for {symbol}")
                return True
            time.sleep(0.2)

        # Fell through — something is still pending. Caller must decide.
        try:
            still = ex.fetch_open_orders(ccxt_sym)
            log.warning(
                f"Cancel verification timeout for {symbol}: {len(still)} order(s) still open"
            )
        except Exception:
            log.warning(f"Cancel verification timeout for {symbol}: poll unavailable")
        return False
    except Exception as e:
        log.error(f"Failed to fetch/cancel orders for {symbol}: {e}")
        return False


def _dump_open_orders_on_4130(symbol: str, ctx: str) -> None:
    """Log every open order's full state on -4130. Operational visibility.

    🔴 FIX (-4130 v2 / Fix D): When -4130 fires, we want to know exactly
    what's still on the exchange — closePosition flag, reduceOnly flag,
    side, stopPrice, positionSide, order id. One extra API call when
    already in error state; cost is acceptable for the diagnostic value.
    """
    try:
        ex = _get_exchange()
        ccxt_sym = _symbol_to_ccxt(symbol)
        open_orders = ex.fetch_open_orders(ccxt_sym)
        log.error(
            f"{symbol} -4130 diagnostic ({ctx}) — {len(open_orders)} open orders:"
        )
        for o in open_orders:
            info = o.get("info", {}) or {}
            log.error(
                f"  id={o.get('id')} type={info.get('type')} "
                f"side={info.get('side')} stopPrice={info.get('stopPrice')} "
                f"closePosition={info.get('closePosition')} "
                f"reduceOnly={info.get('reduceOnly')} "
                f"positionSide={info.get('positionSide')}"
            )
    except Exception as e:
        log.warning(f"{symbol} -4130 diagnostic dump failed: {e}")


def _is_truthy_flag(v) -> bool:
    """Coerce Binance/CCXT info-dict flag to bool.

    Accepts the JSON shapes Binance is observed to return for boolean fields:
      - Python bool True
      - int/float 1 (rare but possible across CCXT versions)
      - string "true" / "True" / "TRUE" (case-insensitive, whitespace-trimmed)
      - string "1"

    Anything else (False, 0, "false", None, missing) → False.

    🔴 FIX ('verify-fail'): the prior implementation only matched True and
    "true". When Binance/CCXT returned reduceOnly as int 1 or string "1",
    the verify match would silently fail → bot would emergency-close a
    legitimately-placed SL.
    """
    if v is True:
        return True
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v == 1:
        return True
    if isinstance(v, str) and v.strip().lower() in ("true", "1"):
        return True
    return False


def _order_matches_type(order: dict, expected_type: Optional[str]) -> bool:
    """Check whether an order matches the expected_type filter.

    expected_type:
      - "closePosition" → only orders with closePosition=True
      - "reduceOnly"    → only orders with reduceOnly=True (and NOT closePosition)
      - "stop_limit"    → 🟢 v8 Fix K: STOP_LIMIT (Binance type=STOP, not
                          STOP_MARKET) with reduceOnly=True. Used by the
                          new v8 helper to find stale STOP_LIMITs for
                          idempotency pre-check.
      - None            → any STOP_MARKET/STOP (legacy, backward-compat)

    🔴 FIX (-4130 v2 / Fix C): Without this filter, _sl_already_at would
    return a stale closePosition order's id when the caller is trying to
    place a NEW reduceOnly SL at the same price. The pre-check would skip
    placement → position effectively guarded by the wrong order type.
    Defensive fallback: if the info dict lacks the flag (CCXT version
    variance), treat as "match" to preserve legacy behavior — no regression.
    """
    if expected_type is None:
        return True
    info = order.get("info", {}) or {}
    has_cp_key = "closePosition" in info
    has_ro_key = "reduceOnly" in info
    cp = _is_truthy_flag(info.get("closePosition"))
    ro = _is_truthy_flag(info.get("reduceOnly"))
    raw_type = (info.get("type") or order.get("type") or "").upper()
    if expected_type == "closePosition":
        if not has_cp_key:
            return True  # defensive — assume legacy match
        return cp
    if expected_type == "reduceOnly":
        if not has_ro_key:
            return True  # defensive — assume legacy match
        return ro and not cp
    if expected_type == "stop_limit":
        # 🟢 v8 Fix K: STOP_LIMIT specifically — raw type 'STOP' (Binance's
        # STOP_LIMIT label) with reduceOnly=True and NOT closePosition.
        # STOP_MARKET orders (raw type 'STOP_MARKET') are excluded so a
        # legacy STOP_MARKET at the same price doesn't false-positive the
        # idempotency check.
        if raw_type != "STOP":
            return False
        if not has_ro_key:
            return True  # defensive — assume legacy match
        return ro and not cp
    return True


def _fetch_current_sl(
    symbol: str, sl_side: str, expected_type: Optional[str] = None
) -> Optional[dict]:
    """Return the currently-active STOP_MARKET on symbol matching sl_side, or None.

    Used for idempotency pre-check before placing a new SL: if server already has
    the desired SL, we skip cancel+place entirely and avoid the -4130 race.

    expected_type filters by closePosition vs reduceOnly to prevent false-positive
    matches on the wrong order kind. None = legacy any-match.
    """
    try:
        ex = _get_exchange()
        ccxt_sym = _symbol_to_ccxt(symbol)
        for o in ex.fetch_open_orders(ccxt_sym):
            info = o.get("info", {}) or {}
            o_type = (info.get("type") or o.get("type") or "").upper()
            o_side = (info.get("side") or o.get("side") or "").lower()
            if "STOP" in o_type and o_side == sl_side.lower():
                if _order_matches_type(o, expected_type):
                    return o
        return None
    except Exception as e:
        log.warning(f"fetch_current_sl failed for {symbol}: {e}")
        return None


def _sl_already_at(
    symbol: str,
    sl_side: str,
    target_px: float,
    tol_pct: float = 0.1,
    expected_type: Optional[str] = None,
) -> Optional[str]:
    """If an SL of expected_type is at target_px (±tol_pct %), return its id; else None."""
    existing = _fetch_current_sl(symbol, sl_side, expected_type=expected_type)
    if not existing:
        return None
    info = existing.get("info", {}) or {}
    try:
        cur = float(info.get("stopPrice") or existing.get("stopPrice") or 0) or 0.0
    except (TypeError, ValueError):
        return None
    if cur <= 0 or target_px <= 0:
        return None
    if abs(cur - target_px) / target_px * 100 <= tol_pct:
        return existing.get("id")
    return None


def _verify_sl_placed(
    symbol: str,
    sl_side: str,
    target_px: float,
    tol_pct: float = 0.2,
    max_polls: int = 20,
    poll_delay: float = 0.5,
    expected_type: Optional[str] = None,
    expected_id: Optional[str] = None,
) -> bool:
    """Verify a STOP_MARKET of expected_type exists at target_px (±tol_pct %) for sl_side.

    🔴 FIX (Bug 2): Binance's create_order response is NOT a guarantee that
    the order is queryable yet — order-list settlement can lag the response
    by up to a few hundred ms. Without this verification step, the helper's
    `success=True` flag can be a lie. If the SL never actually shows up on
    the exchange, the caller will think it's protected when it isn't.

    🔴 FIX (-4130 v2 / Fix C): expected_type filter prevents false-positive
    verification when a stale closePosition order matches a fresh reduceOnly
    placement at the same price (or vice versa).

    🔴 FIX ('verify-fail' Fix 2): when caller passes expected_id (the id
    returned from create_order), we scan open_orders by id FIRST. Direct
    id-match is exchange-authoritative — eliminates the brittle reconstruct-
    and-match path (price tolerance, type-flag truthy quirks) that produced
    false negatives. Falls back to legacy price+type match if id-match
    misses (handles the rare case where id is present but Binance hasn't
    populated reduceOnly flag yet, etc).

    🔴 FIX ('verify-fail' Fix 4+5): defaults bumped from
    (max_polls=4, poll_delay=0.4, tol_pct=0.1) → (10, 0.5, 0.2).
    New budget: 5s total. Adequate for Binance order-list propagation lag
    under load. Wider tolerance absorbs any residual rounding asymmetry
    between the placed price and the value Binance reports back.

    Polls fetch_open_orders up to `max_polls` times, sleeping `poll_delay`
    seconds between polls. Returns True if confirmed; False otherwise.
    """
    ex = _get_exchange()
    ccxt_sym = _symbol_to_ccxt(symbol)
    for i in range(max_polls):
        # Fast path: scan by exact order id when caller provides it.
        if expected_id:
            try:
                for o in ex.fetch_open_orders(ccxt_sym):
                    if str(o.get("id")) == str(expected_id):
                        if _order_matches_type(o, expected_type):
                            log.info(
                                f"{symbol} SL verified by id={expected_id} "
                                f"after {i+1} poll(s) (type={expected_type or 'any'})"
                            )
                            return True
                        else:
                            # Order exists with the right id but wrong type
                            # (extremely unlikely but defensible — fall through
                            # to price-match in case the type-flag is stale).
                            log.warning(
                                f"{symbol} order id={expected_id} exists but "
                                f"type filter rejected — falling through to price match"
                            )
                            break
            except Exception as e:
                log.warning(f"{symbol} verify by-id scan failed: {e}")
        # Legacy path: price + type-flag match within tolerance.
        existing_id = _sl_already_at(
            symbol, sl_side, target_px, tol_pct, expected_type=expected_type
        )
        if existing_id:
            log.info(
                f"{symbol} SL verified at {target_px} after {i+1} poll(s) "
                f"(id={existing_id}, type={expected_type or 'any'})"
            )
            return True
        time.sleep(poll_delay)
    # 🔴 FIX (v3 Fix 5 / v5 Fix H): direct fetch_order rescue. open_orders list
    # can lag the order's actual state by seconds under load. fetch_order(id) is
    # authoritative — if Binance has the order with active status, return True.
    # v5: 3 attempts × 2s — a single transient 5xx during this rescue used to
    # push the caller into the sl_unverified path unnecessarily.
    if expected_id:
        for rescue_attempt in range(1, 4):
            try:
                ex2 = _get_exchange()
                o = ex2.fetch_order(expected_id, ccxt_sym) or {}
                raw = o.get("info") or {}
                status = (
                    (o.get("status") or "").lower()
                    or (raw.get("status") or "").lower()
                )
                if status in (
                    "open", "new", "untriggered", "active",
                    "filled", "closed", "partially_filled",
                ):
                    log.info(
                        f"{symbol} SL verified by direct fetch_order id={expected_id} "
                        f"(status={status}, rescue_attempt={rescue_attempt}) — "
                        f"open_orders list lagging"
                    )
                    return True
                if status in ("canceled", "cancelled", "expired", "rejected"):
                    log.warning(
                        f"{symbol} fetch_order rescue: id={expected_id} "
                        f"status={status!r} — order DEAD, no point retrying"
                    )
                    break
            except Exception as fe:
                log.warning(
                    f"{symbol} fetch_order direct lookup attempt "
                    f"{rescue_attempt}/3 failed: {fe}"
                )
            if rescue_attempt < 3:
                time.sleep(2.0)
    log.error(
        f"{symbol} SL verification FAILED — no STOP_MARKET ({expected_type or 'any'}) "
        f"found at {target_px} after {max_polls} polls (tol {tol_pct}%, "
        f"expected_id={expected_id or 'none'})"
    )
    return False


def _diagnose_sl_verify_fail(symbol: str, order_id: str) -> dict:
    """Fetch a specific order by id and dump its actual state to logs.

    Called when `_verify_sl_placed` returned False even though create_order
    returned a valid id. The order may be:
      - status="closed" / "filled" → SL triggered between placement and verify
        (price moved through stop in 1-5s). Position is already closed by SL.
        Helper should NOT emergency-close again.
      - status="canceled" → Binance silently rejected after ack (e.g. invalid
        tick, margin issue post-fill). Helper should retry placement.
      - status="open" but reduceOnly/closePosition flag mismatched → defensive
        log to help diagnose Fix C/Fix 6 edge cases.
      - fetch_order itself fails → log + return empty dict; helper proceeds
        with normal retry (no false-positive success).

    Returns the order dict (unified CCXT shape) on success, empty dict on failure.
    """
    try:
        ex = _get_exchange()
        ccxt_sym = _symbol_to_ccxt(symbol)
        o = ex.fetch_order(order_id, ccxt_sym) or {}
        info = o.get("info", {}) or {}
        # 🟢 FIX (v4 Fix D): extended telemetry. Adds clientId, raw vs unified
        # status, updateTime, and local time delta so the next failure has
        # actionable data without needing to instrument live.
        log.error(
            f"{symbol} verify-fail diag: id={order_id} "
            f"clientId={info.get('clientOrderId')!r} "
            f"status_unified={o.get('status')!r} "
            f"status_raw={info.get('status')!r} "
            f"updateTime={info.get('updateTime')} "
            f"local_now={int(time.time()*1000)} "
            f"filled={o.get('filled')}/{o.get('amount')} "
            f"stopPrice={info.get('stopPrice')!r} "
            f"reduceOnly={info.get('reduceOnly')!r} "
            f"closePosition={info.get('closePosition')!r} "
            f"type={info.get('type')!r} "
            f"positionSide={info.get('positionSide')!r}"
        )
        return o
    except Exception as e:
        log.error(f"{symbol} verify-fail diag — fetch_order({order_id}) failed: {e}")
        return {}


def verify_existing_sl(
    symbol: str,
    sl_order_id: str,
    stop_price: float,
    client_oid: Optional[str] = None,
    sl_side: Optional[str] = None,
    direction: Optional[str] = None,
) -> dict:
    """🟢 v5 Fix H + v11 Fix O-2/O-4/O-5/O-8: re-verify a previously-acked SL with 4-state result.

    Used by bot.py's 60s deferred re-verify cleanup pass to determine whether a
    `sl_unverified=True` trade's SL is genuinely on Binance. Returns:
        {status_code: str, status: str, info: dict, error: str|None}

    status_code values (caller in bot.py branches on this):
      - 'alive'      : SL exists with an active or already-filled status.
                       Resolve cleanly.
      - 'gone_clean' : SL canceled/expired AND position is flat. Position
                       was closed by another path (TP fill, manual close,
                       liquidation) and Binance auto-canceled the closePosition
                       SL — this is a normal happy exit, not a failure.
      - 'lost'      : SL canceled/expired/missing AND position is still open.
                       Caller should re-arm or emergency-close.
      - 'unknown'    : transient API failure or empty status across all
                       attempts. Caller MUST NOT emergency-close on this.

    Implementation:
      - 3 attempts × 2s backoff. Each attempt calls fetch_order(id, sym) and
        inspects both ccxt's unified `status` and the Binance raw `info.status`.
      - On exception or empty status, retry. Only after all attempts
        inconclusive do we return 'unknown'.
      - On a positive DEAD status, gate against the live position state via
        get_open_position() to distinguish a legitimate clean exit from a
        truly naked position.

    Why this matters: the previous version returned `found=False` on any
    exception or non-whitelisted status. A single transient Binance 5xx wave
    or an auto-cancel by Binance on a closePosition SL after the position
    closed cleanly elsewhere both produced false-positive "SL LOST" alerts
    that emergency-closed working trades.
    """
    ALIVE = (
        "open", "new", "untriggered", "active",
        "filled", "closed", "partially_filled",
    )
    DEAD = ("canceled", "cancelled", "expired", "rejected")
    last_err: Optional[str] = None
    last_status: str = ""
    last_info: dict = {}
    saw_2013 = False
    for attempt in range(1, 4):
        try:
            ex = _get_exchange()
            ccxt_sym = _symbol_to_ccxt(symbol)
            # 🟢 v11 Fix O-2 (part A): try by orderId first. If -2013 fires,
            # fall back to clientOrderId lookup (Binance's origClientOrderId
            # query, ccxt accepts via params).
            try:
                o = ex.fetch_order(sl_order_id, ccxt_sym) or {}
            except Exception as fe1:
                err_str = str(fe1)
                if "-2013" in err_str and client_oid:
                    log.warning(
                        f"verify_existing_sl({symbol},{sl_order_id}) "
                        f"attempt={attempt} orderId -2013 — retrying via "
                        f"clientOrderId={client_oid!r}"
                    )
                    saw_2013 = True
                    try:
                        o = ex.fetch_order(
                            sl_order_id, ccxt_sym,
                            params={"origClientOrderId": client_oid},
                        ) or {}
                    except Exception as fe2:
                        log.warning(
                            f"verify_existing_sl({symbol},{sl_order_id}) "
                            f"cid fallback also raised: {fe2}"
                        )
                        raise fe2
                else:
                    if "-2013" in err_str:
                        saw_2013 = True
                    raise
            raw = (o.get("info") or {})
            unified = (o.get("status") or "").lower()
            raw_status = (raw.get("status") or "").lower()
            status = unified or raw_status
            # 🟢 v11 Fix O-8: structured telemetry on every verify attempt
            log.info(
                f"SL_FETCH_RAW symbol={symbol} id={sl_order_id} "
                f"cid={raw.get('clientOrderId')!r} attempt={attempt} "
                f"unified={unified!r} raw={raw_status!r} "
                f"cancelReason={raw.get('cancelReason')!r} "
                f"updateTime={raw.get('updateTime')!r}"
            )
            last_status, last_info = status, raw
            if status in ALIVE:
                return {
                    "status_code": "alive",
                    "status": status,
                    "info": raw,
                    "error": None,
                }
            if status in DEAD:
                # 🟢 v11 Fix O-5: capture cancellation reason from Binance
                # `info.cancelReason` field. Surfaces WHY the SL died
                # (USER_CANCELED, MARGIN_CALL, LIQUIDATION,
                # EXPIRED_IN_MATCH, STOP_PRICE_TRIGGER, etc).
                cancel_reason = raw.get("cancelReason") or raw.get("reason") or "UNKNOWN"
                log.error(
                    f"verify_existing_sl({symbol},{sl_order_id}) DEAD status="
                    f"{status!r} cancelReason={cancel_reason!r} "
                    f"raw_info_keys={list(raw.keys())!r}"
                )
                # 🟢 v7 Fix J-3: require POSITIVE confirmation that position
                # is flat. Treat None (API failure) as 'unknown', retry.
                pos = get_open_position(symbol)
                if pos is None:
                    last_err = (
                        f"DEAD status={status!r} reason={cancel_reason!r} "
                        f"but get_open_position returned None (API failure?) "
                        f"— treating as 'unknown', will retry"
                    )
                    log.warning(
                        f"verify_existing_sl({symbol},{sl_order_id}) {last_err}"
                    )
                elif float(pos.get("qty", 0) or 0) <= 0:
                    return {
                        "status_code": "gone_clean",
                        "status": status,
                        "info": raw,
                        "error": None,
                        "cancel_reason": cancel_reason,
                    }
                else:
                    return {
                        "status_code": "lost",
                        "status": status,
                        "info": raw,
                        "error": None,
                        "cancel_reason": cancel_reason,
                    }
            else:
                # Status neither ALIVE nor DEAD ⇒ empty / unknown ⇒ retry.
                last_err = (
                    f"empty/unknown status: unified={unified!r} "
                    f"raw={raw_status!r}"
                )
        except Exception as e:
            err_str = str(e)
            last_err = err_str
            if "-2013" in err_str:
                saw_2013 = True
            log.warning(
                f"verify_existing_sl({symbol},{sl_order_id}) "
                f"attempt={attempt}/3 fetch_order failed: {e}"
            )
        if attempt < 3:
            # 🟢 v11 Fix O-4: -2013-specific 10s backoff. Binance's
            # internal id index can lag under load. Other errors keep 2s.
            sleep_s = 10.0 if saw_2013 else 2.0
            log.info(
                f"verify_existing_sl({symbol},{sl_order_id}) "
                f"backing off {sleep_s}s before attempt {attempt + 1}/3 "
                f"(2013_seen={saw_2013})"
            )
            time.sleep(sleep_s)

    # 🟢 v11 Fix O-2 (part B): before declaring 'unknown', try ATTRIBUTE-
    # FINGERPRINT scan of open_orders. If Binance internally replaced the
    # SL under a new id, the original id returns -2013 but a matching SL
    # is on the book. Reuse _sl_already_at() — it already does type +
    # price match. Extends with side check.
    if sl_side and saw_2013:
        try:
            new_id = _sl_already_at(
                symbol, sl_side, stop_price, tol_pct=0.2,
                expected_type="reduceOnly",
            )
            if new_id:
                log.warning(
                    f"verify_existing_sl({symbol},{sl_order_id}) ATTRIBUTE "
                    f"SCAN RESCUE — Binance has replacement SL on book "
                    f"under new id={new_id}. Treating as alive."
                )
                return {
                    "status_code": "alive",
                    "status": "open",
                    "info": {"_replaced_id": new_id},
                    "error": None,
                    "replaced_id": new_id,
                }
        except Exception as fe:
            log.warning(f"verify_existing_sl attribute-scan rescue failed: {fe}")
    # 🟢 v9 Fix L diagnostic: dump full raw info on 'unknown' so we can
    # diagnose future false-positive emergency-closes. Past pattern showed
    # transient API quirks returning empty status; v9 captures the full
    # ccxt response shape so we can map any missing status code.
    log.warning(
        f"verify_existing_sl({symbol},{sl_order_id}) inconclusive after 3 "
        f"attempts — returning 'unknown' (last_status={last_status!r}, "
        f"last_err={last_err!r}, last_info_keys={list(last_info.keys())!r}, "
        f"last_info_type={last_info.get('type')!r}, "
        f"last_info_status={last_info.get('status')!r}, "
        f"last_info_origType={last_info.get('origType')!r})"
    )
    return {
        "status_code": "unknown",
        "status": last_status,
        "info": last_info,
        "error": last_err,
    }


def _final_sl_lost_check(symbol: str, sl_order_id: str, ot: dict) -> str:
    """🟢 v5 Fix H: positive-evidence gate before pulling the emergency-close
    trigger when verify_existing_sl returned 'unknown' AND the 90s grace has
    elapsed.

    Rationale: 'unknown' means transient API failure, not "SL is gone." Before
    nuking a working position we demand at least one piece of positive evidence.
    Only when ALL three checks below come up dry do we conclude the SL is
    truly lost.

    Returns one of:
      - 'alive' : SL is on the order book under any id, OR fetch_order(id)
                  reports an active/filled status. Resolve.
      - 'flat'  : Position is already at zero size. Nothing to protect; the
                  trade closed by another path. Resolve silently.
      - 'lost'  : Position is open AND no SL order exists for it. Caller may
                  emergency-close.
    """
    # 🟢 v7 Fix J-4: only declare 'flat' on POSITIVE confirmation. Previous
    # version treated `pos is None` (silent fallback when get_open_position
    # caught an exception) the same as a confirmed qty=0. That allowed a
    # transient API failure during the grace-end final check to short-
    # circuit to 'flat' ⇒ caller in bot.py pinned bars=MAX_HOLD ⇒ check_exits
    # mislabeled the close as TIME ⇒ user saw a healthy trade closed at
    # ~$0 P&L. Now: pos=None falls through to the open_orders + fetch_order
    # checks below, never short-circuiting on uncertain evidence.
    try:
        pos = get_open_position(symbol)
        if pos is None:
            log.warning(
                f"{symbol} final-check: get_open_position returned None — "
                f"continuing to other checks (do NOT default to 'flat' on "
                f"transient API failure)"
            )
        elif float(pos.get("qty", 0) or 0) <= 0:
            log.info(f"{symbol} final-check: position flat — clean resolve")
            return "flat"
    except Exception as e:
        log.warning(f"{symbol} final-check pos fetch failed: {e}")

    direction = (ot or {}).get("dir", "")
    sl_side = "sell" if direction == "LONG" else "buy"
    target_pos_side = _position_side_for(direction) if direction else None
    try:
        ex = _get_exchange()
        ccxt_sym = _symbol_to_ccxt(symbol)
        for o in ex.fetch_open_orders(ccxt_sym):
            info = o.get("info") or {}
            otype = (info.get("type") or o.get("type") or "").upper()
            if otype not in ("STOP_MARKET", "STOP"):
                continue
            if (o.get("side") or "").lower() != sl_side:
                continue
            if target_pos_side and info.get("positionSide") != target_pos_side:
                continue
            log.warning(
                f"{symbol} final-check: alternate guarding SL on book "
                f"id={o.get('id')} stopPrice={info.get('stopPrice')} "
                f"closePosition={info.get('closePosition')!r} "
                f"reduceOnly={info.get('reduceOnly')!r} — treating as alive"
            )
            return "alive"
    except Exception as e:
        log.warning(f"{symbol} final-check open_orders failed: {e}")

    # 🟢 v11 Fix O-2 (final-check side): try orderId, then clientOrderId
    # fallback. If orderId returns -2013 and we have a stored cid, query
    # via origClientOrderId.
    client_oid = (ot or {}).get("exec_sl_cid")
    try:
        ex = _get_exchange()
        ccxt_sym = _symbol_to_ccxt(symbol)
        try:
            o = ex.fetch_order(sl_order_id, ccxt_sym) or {}
        except Exception as ofe:
            if "-2013" in str(ofe) and client_oid:
                log.warning(
                    f"{symbol} final-check: orderId -2013, trying "
                    f"clientOrderId={client_oid!r}"
                )
                o = ex.fetch_order(
                    sl_order_id, ccxt_sym,
                    params={"origClientOrderId": client_oid},
                ) or {}
            else:
                raise
        raw = o.get("info") or {}
        status = (o.get("status") or raw.get("status") or "").lower()
        log.info(
            f"{symbol} final-check FETCH_RAW status={status!r} "
            f"cancelReason={raw.get('cancelReason')!r}"
        )
        if status in (
            "open", "new", "untriggered", "active",
            "filled", "closed", "partially_filled",
        ):
            log.info(
                f"{symbol} final-check: fetch_order(id={sl_order_id}) "
                f"status={status!r} — alive"
            )
            return "alive"
    except Exception as e:
        log.warning(f"{symbol} final-check fetch_order failed: {e}")

    log.error(
        f"{symbol} final-check: no guarding SL on book AND fetch_order(id) "
        f"inconclusive AND position still open — declaring 'lost'"
    )
    return "lost"


def _place_deadman_sl(
    symbol: str,
    sl_side: str,
    stop_price: float,
    direction: str,
    qty: float,
) -> Optional[str]:
    """🟢 v11 Fix O-6: deadman-switch backup SL at 5% beyond primary.

    Placed AFTER the primary reduceOnly SL succeeds. Uses
    `closePosition=True STOP_MARKET` which:
      - Auto-sizes to whatever position exists at trigger time (no qty)
      - Auto-cancels server-side when position size hits zero
      - Never canceled by bot logic unless bot calls cancel_open_orders
        (only happens in execute_full_close which is intentional)

    If Binance loses the primary SL (the -2013 root-cause path that
    motivated v11), this deadman fires on catastrophic move and closes
    the position. Worst-case loss becomes 5% beyond planned SL instead
    of unbounded.

    Best-effort. Returns id on success, None on failure. Failure here
    does NOT block trade — primary SL is still the main guard.

    NOTE: closePosition + reduceOnly are mutually exclusive on Binance
    Futures. closePosition orders use a separate ledger from reduceOnly,
    so primary (reduceOnly) + deadman (closePosition) coexist without
    triggering -4130. closePosition orders ignore the `amount` field but
    ccxt still requires it; pass position qty for API satisfaction.
    """
    try:
        ex = _get_exchange()
        ccxt_sym = _symbol_to_ccxt(symbol)
        stop_price = _round_price(ccxt_sym, stop_price)
        qty_r = _round_qty(ccxt_sym, qty) if qty and qty > 0 else 0.001

        params = {
            "stopPrice": stop_price,
            "closePosition": True,
        }
        ps = _position_side_for(direction)
        if ps:
            params["positionSide"] = ps

        # Deadman uses bot-tagged clientOrderId so it's identifiable in
        # logs and Binance UI. Tag prefix `dms-` (DeadMan Switch).
        _ts_ms = int(time.time() * 1000)
        deadman_cid = f"dms-{symbol[:10]}-{_ts_ms}"[:36]
        params["newClientOrderId"] = deadman_cid

        log.info(
            f"{symbol} placing DEADMAN SL: {sl_side} closePosition "
            f"stop={stop_price} cid={deadman_cid}"
        )
        deadman_order = ex.create_order(
            symbol=ccxt_sym,
            type="stop_market",
            side=sl_side,
            amount=qty_r,
            params=params,
        )
        deadman_id = deadman_order.get("id")
        info = deadman_order.get("info") or {}
        log.info(
            f"DEADMAN_CREATE_RAW symbol={symbol} id={deadman_id} "
            f"cid={info.get('clientOrderId', deadman_cid)!r} "
            f"status={info.get('status')!r} "
            f"closePosition={info.get('closePosition')!r} "
            f"positionSide={info.get('positionSide')!r} "
            f"stopPrice={info.get('stopPrice')!r}"
        )
        return deadman_id
    except Exception as e:
        # -4130 would mean a stale closePosition SL exists from a prior
        # cycle. Should be rare since open_position runs cancel_open_orders
        # inside the primary helper. Log + return None; primary SL still
        # guards the trade.
        log.warning(f"{symbol} _place_deadman_sl raised: {e}")
        return None


def _place_closeposition_sl_with_retry(
    symbol: str,
    sl_side: str,
    stop_price: float,
    qty: float,
    max_attempts: int = 5,
    min_wait_after_cancel: float = 2.5,
) -> dict:
    """🟢 v8 Fix K: closePosition STOP_MARKET retired — incompatible with STOP_LIMIT.

    Now a thin wrapper that delegates to `_place_reduceonly_sl_with_retry`
    (which became the STOP_LIMIT path in v8). All callers (entry SL, dust
    close, BE move fallback) flow through the same STOP_LIMIT + reduceOnly
    code path.

    Why: Binance Futures does NOT allow `closePosition=True` on STOP_LIMIT
    orders — only STOP_MARKET. Since user requested STOP_LIMIT in v8, we
    must abandon closePosition and pass an explicit qty + reduceOnly flag.

    If caller passes `qty<=0` or omits, fall back to the live position's
    qty via `get_open_position()`. This preserves the legacy contract
    where the closePosition helper would size automatically.

    Inherits all retry/cancel/verify/recovery behavior from the underlying
    reduceOnly helper, including v5 Fix H 4-state safety + v6 Fix I
    fast-path + v7 Fix J close_reason preservation.
    """
    if qty is None or qty <= 0:
        pos = get_open_position(symbol)
        if pos is None or float(pos.get("qty", 0) or 0) <= 0:
            return {
                "success": False,
                "sl_order_id": None,
                "error": (
                    f"{symbol} closePosition wrapper: no qty supplied AND no "
                    f"open position to size against. Nothing to protect."
                ),
            }
        qty = float(pos["qty"])
        log.info(
            f"{symbol} closePosition wrapper: qty was omitted/zero, "
            f"using live position qty={qty}"
        )
    return _place_reduceonly_sl_with_retry(
        symbol=symbol,
        sl_side=sl_side,
        stop_price=stop_price,
        qty=qty,
        max_attempts=max_attempts,
        min_wait_after_cancel=min_wait_after_cancel,
    )


def _place_reduceonly_sl_with_retry(
    symbol: str,
    sl_side: str,
    stop_price: float,
    qty: float,
    max_attempts: int = 5,
    min_wait_after_cancel: float = 1.5,
) -> dict:
    """🟢 v9 Fix L: reduceOnly STOP_MARKET SL — reverts v8's STOP_LIMIT.

    Background: v8 Fix K switched SL from STOP_MARKET → STOP_LIMIT with a
    0.3% bracket. Live deployment surfaced a regression: SL_LOST alerts
    returning after 90s grace with emergency closes on positions that
    Binance still held. Most likely v8 cause: STOP_LIMIT pre-trigger
    lifecycle reads differently in ccxt's `fetch_order` than STOP_MARKET,
    confusing `verify_existing_sl`'s 4-state classifier and/or
    `_final_sl_lost_check`'s type filter on the open_orders scan. v9
    reverts SL execution to v7's known-good STOP_MARKET reduceOnly path
    while keeping ALL v8 strategy reverts (v4 Cfg, 24/7, RSI cross-bar).

    reduceOnly STOP_MARKET bypasses Binance's closePosition tracker race
    (-4130) and uses an independent ledger. Position fill_qty is known
    exactly at placement so explicit qty is fine.

    Inherits all prior safety: v6 Fix I fast-path on order_known_active,
    v5 Fix H 4-state deferred re-verify, v7 Fix J close_reason preservation.
    """
    ccxt_sym = _symbol_to_ccxt(symbol)
    ex = _get_exchange()
    result: dict = {"success": False, "sl_order_id": None, "error": None}
    last_err: Optional[Exception] = None

    # Idempotency pre-check — reduceOnly only, so stale closePosition at same
    # price doesn't false-positive and skip the placement we actually want.
    existing_id = _sl_already_at(
        symbol, sl_side, stop_price, expected_type="reduceOnly"
    )
    if existing_id:
        log.info(
            f"{symbol} reduceOnly SL already at target {stop_price} — "
            f"skipping replace (id={existing_id})"
        )
        return {"success": True, "sl_order_id": existing_id, "error": None}

    for attempt in range(1, max_attempts + 1):
        cancelled_clean = cancel_open_orders(symbol)
        wait = min_wait_after_cancel if cancelled_clean else max(min_wait_after_cancel, 2.0)
        log.info(
            f"{symbol} reduceOnly SL attempt {attempt}/{max_attempts}: "
            f"cancelled_clean={cancelled_clean}, waiting {wait:.2f}s"
        )
        time.sleep(wait)

        try:
            # 🟢 v11 Fix O-1: bot-generated clientOrderId for SL placement.
            # Format: 'botsl-{SYMBOL}-{UTC_MS}'. Binance preserves this id
            # in fetch_order params via `origClientOrderId` query — survives
            # the -2013 'Order does not exist' lookup quirk on orderId.
            # Max length 36 chars per Binance docs. Stripped of slashes.
            _ts_ms = int(time.time() * 1000)
            client_oid = f"botsl-{symbol[:10]}-{_ts_ms}"[:36]
            # Pre-store in result so any success-path return preserves it.
            result["sl_client_oid"] = client_oid
            params = {
                "stopPrice": stop_price,
                "reduceOnly": True,
                "newClientOrderId": client_oid,
            }
            ps = _position_side_for("LONG" if sl_side == "sell" else "SHORT")
            if ps:
                params["positionSide"] = ps
            log.info(
                f"PLACING SL (attempt {attempt}/{max_attempts}): "
                f"{sl_side} reduceOnly {ccxt_sym} qty={qty} @ {stop_price} "
                f"cid={client_oid}"
            )
            sl_order = ex.create_order(
                symbol=ccxt_sym,
                type="stop_market",   # 🟢 v9 Fix L: revert to STOP_MARKET (was 'stop' STOP_LIMIT in v8)
                side=sl_side,
                amount=qty,
                params=params,
            )
            order_id = sl_order.get("id", "unknown")

            # 🟢 v11 Fix O-8: telemetry — log raw create_order response so
            # future SL_LOST events self-document. One line, key=value pairs.
            _ci = sl_order.get("info") or {}
            log.info(
                f"SL_CREATE_RAW symbol={symbol} id={order_id} "
                f"cid={_ci.get('clientOrderId', client_oid)!r} "
                f"status={(sl_order.get('status') or '').lower()!r} "
                f"origStatus={_ci.get('status')!r} "
                f"reduceOnly={_ci.get('reduceOnly')!r} "
                f"closePosition={_ci.get('closePosition')!r} "
                f"positionSide={_ci.get('positionSide')!r} "
                f"stopPrice={_ci.get('stopPrice')!r} "
                f"updateTime={_ci.get('updateTime')!r}"
            )

            # 🟢 FIX (v4 Fix A): inspect the create_order response status
            # BEFORE going to verify. If Binance synchronously rejected
            # (REJECTED/EXPIRED/CANCELED), retry. If accepted (NEW/ACCEPTED),
            # the id is post-commit and verify becomes a sanity check.
            # When verify+diag both lag (Binance read-side stale), we trust
            # the id rather than emergency-closing a working trade.
            create_info = sl_order.get("info") or {}
            create_raw_status = (create_info.get("status") or "").upper()
            create_unified_status = (sl_order.get("status") or "").lower()
            order_known_active = (
                create_raw_status in ("NEW", "ACCEPTED", "PARTIALLY_FILLED")
                or create_unified_status in ("open", "new", "untriggered", "active")
            )
            order_known_dead = (
                create_raw_status in ("REJECTED", "EXPIRED", "EXPIRED_IN_MATCH", "CANCELED")
                or create_unified_status in ("rejected", "expired", "canceled")
            )
            if order_known_dead:
                last_err = Exception(
                    f"create_order returned dead status: "
                    f"raw={create_raw_status} unified={create_unified_status}"
                )
                log.error(
                    f"{symbol} SL {order_id} dead at create — retry. "
                    f"err={last_err}"
                )
                if attempt < max_attempts:
                    time.sleep(max(2.5, 2.0 * attempt))
                    continue
                break

            # 🟢 FIX (v6 Fix I): fast-path skip-verify when create_order's
            # response carries an authoritative active status. See closePosition
            # path above for full rationale. The sl_unverified flag keeps the
            # v5 Fix H deferred re-verify (tightened to 20s on fast-path) as the
            # safety net; tg_sl_unverified does NOT fire on this branch.
            if order_known_active:
                log.info(
                    f"{symbol} reduceOnly SL {order_id} create-acked "
                    f"(raw={create_raw_status!r} unified={create_unified_status!r}) "
                    f"— fast-path skip verify (≈16s REST polling avoided). "
                    f"v5 Fix H deferred check will silently confirm."
                )
                result.update({
                    "success": True,
                    "sl_order_id": order_id,
                    "error": None,
                    "verify_recovered": False,
                    "sl_unverified": True,
                    "fast_path": True,
                })
                return result

            # 🔴 FIX (Bug 2): verify the reduceOnly SL is actually queryable in
            # open orders before declaring success. Without this, a race where
            # create_order succeeds but order-list lookup fails leaves the bot
            # trusting a non-existent SL and the position effectively naked.
            # Type-aware to reject stale closePosition at same price.
            # 🔴 FIX ('verify-fail' Fix 2): pass expected_id for direct id-match.
            # 🟢 NOTE (v6 Fix I): only reached on empty-status responses now.
            if not _verify_sl_placed(
                symbol, sl_side, stop_price,
                expected_type="reduceOnly",   # 🟢 v9 Fix L: revert (v8 used "stop_limit")
                expected_id=order_id,
            ):
                # 🔴 FIX ('verify-fail' Fix 3): diag fetch_order to find out
                # WHY verify failed. If status is filled/closed, the SL
                # already triggered between placement and verify (instant fill
                # near stop) — position is closed; do NOT emergency-close
                # AGAIN, just declare success.
                # 🟢 FIX (v4 Fix B): wrap diag in try/except — if diag itself
                # raises (network/exchange dead), don't crash retry loop.
                try:
                    diag = _diagnose_sl_verify_fail(symbol, order_id)
                except Exception as de:
                    log.warning(f"{symbol} diag raised: {de}")
                    diag = {}
                diag_status = (diag.get("status") or "").lower()
                if diag_status in ("filled", "closed"):
                    log.warning(
                        f"{symbol} SL order {order_id} ALREADY FILLED — stop "
                        f"triggered between placement and verify. Position "
                        f"already closed by SL. Treating as success."
                    )
                    result.update({
                        "success": True,
                        "sl_order_id": order_id,
                        "error": None,
                        "filled_at_placement": True,
                    })
                    return result
                # 🔴 FIX (v3 Fix 2): order exists with active status — verify
                # just couldn't find it via heuristic match. Trust the id.
                # Order is on Binance, position protected. This is the path
                # that was incorrectly triggering Fix 7 self-destruct before.
                if diag_status in ("open", "new", "untriggered", "active"):
                    info = diag.get("info") or {}
                    log.warning(
                        f"{symbol} SL order {order_id} EXISTS server-side "
                        f"(status={diag_status}) but verify heuristic missed it. "
                        f"Trusting id. reduceOnly={info.get('reduceOnly')!r} "
                        f"closePosition={info.get('closePosition')!r} "
                        f"stopPrice={info.get('stopPrice')!r}"
                    )
                    result.update({
                        "success": True,
                        "sl_order_id": order_id,
                        "error": None,
                        "verify_recovered": True,
                    })
                    return result
                # 🟢 FIX (v4 Fix A core): verify+diag BOTH empty/lagging BUT
                # we have a known-active id from create_order. This is a
                # Binance read-side lag, NOT a placement failure. DO NOT retry
                # (would create duplicate SL). DO NOT emergency-close. Declare
                # success with sl_unverified flag — bot.py main loop will
                # re-verify in 60s with a fresh API state (Fix F + Fix G).
                if order_known_active or (
                    create_raw_status == "" and create_unified_status == ""
                ):
                    log.warning(
                        f"{symbol} SL {order_id} write-acked "
                        f"(raw={create_raw_status} unified={create_unified_status}) "
                        f"but read-side lagging. TRUSTING id. "
                        f"Telegram alert + cleanup re-verify in 60s."
                    )
                    try:
                        from bot import tg_sl_unverified
                        tg_sl_unverified(symbol, order_id, stop_price)
                    except Exception as te:
                        log.warning(f"tg_sl_unverified send failed: {te}")
                    result.update({
                        "success": True,
                        "sl_order_id": order_id,
                        "error": None,
                        "verify_recovered": False,
                        "sl_unverified": True,
                    })
                    return result
                last_err = Exception(
                    f"reduceOnly SL placement returned id={order_id} but "
                    f"verification poll found no reduceOnly STOP_MARKET at {stop_price}"
                )
                log.error(str(last_err))
                # 🔴 FIX (v3) — Fix 7 (inner cross-fallback) REMOVED.
                # Previously: after 2nd verify-fail, switched to closePosition.
                # That path's cancel_open_orders() killed the reduceOnly SL we
                # just placed (Binance returned a real id; order existed). Then
                # closePosition placement hit -4130 from a separate stale-state
                # issue and 5 retries failed. Net: working SL destroyed,
                # position emergency-closed. The diag-status branch above (Fix
                # 2 v3) now rescues the legitimate "order exists, verify just
                # missed it" case directly. Outer fallback in open_position
                # still handles genuine reduceOnly placement failures.
                if attempt < max_attempts:
                    time.sleep(max(2.5, 2.0 * attempt))
                    continue
                break

            result.update({
                "success": True,
                "sl_order_id": order_id,
                "error": None,
            })
            log.info(f"SL placed (reduceOnly): {result['sl_order_id']} @ {stop_price}")
            return result

        except Exception as e:
            last_err = e
            err_str = str(e)
            # qty below Binance minimum lot — non-retryable; fall back to closePosition
            # which ignores qty entirely and closes whatever position exists.
            if "minimum amount" in err_str.lower():
                # 🟢 v8 Fix K: previously fell back to closePosition (which
                # ignores qty). In v8 closePosition is a thin wrapper around
                # this helper, so a fallback would infinite-loop. Sub-minimum
                # positions cannot be guarded by STOP_LIMIT + reduceOnly.
                # Surface the error to caller (open_position emergency-closes;
                # move_stop_loss logs and continues). Dust positions are
                # handled by the dust-close path in close_full_position.
                log.error(
                    f"{symbol} reduceOnly qty {qty} below exchange minimum "
                    f"— STOP_LIMIT cannot guard sub-min positions in v8. "
                    f"Returning error to caller for upstream dust handling."
                )
                return {
                    "success": False,
                    "sl_order_id": None,
                    "error": (
                        f"qty {qty} below Binance minimum lot — dust "
                        f"position; cannot place STOP_LIMIT SL"
                    ),
                }
            is_4130 = "4130" in err_str
            if is_4130 and attempt < max_attempts:
                backoff = max(2.5, 2.0 * attempt)
                log.warning(
                    f"{symbol} reduceOnly SL hit -4130 on attempt {attempt} — "
                    f"backing off {backoff:.1f}s and retrying."
                )
                _dump_open_orders_on_4130(symbol, "reduceOnly path")
                time.sleep(backoff)
                continue
            log.error(
                f"{symbol} reduceOnly SL failed (attempt {attempt}/{max_attempts}): {e}"
            )
            break

    try:
        pos = get_open_position(symbol)
        oo = len(ex.fetch_open_orders(ccxt_sym))
        log.error(
            f"-4130 exhausted on {symbol} (reduceOnly): hedge={_get_hedge_mode()} "
            f"pos_qty={pos['qty'] if pos else 0} open_orders={oo} "
            f"last_err={str(last_err)[:200]}"
        )
    except Exception:
        pass

    result["error"] = f"reduceOnly SL placement failed after {max_attempts} attempt(s): {last_err}"
    return result


def move_stop_loss(
    symbol: str,
    direction: str,
    new_sl_price: float,
    remaining_qty: float,
) -> dict:
    """Move stop-loss to a new price (e.g. breakeven).

    Cancels existing SL orders and places a new one.

    Args:
        symbol: e.g. "BTCUSDT"
        direction: "LONG" or "SHORT"
        new_sl_price: the new stop price
        remaining_qty: current position size remaining

    Returns:
        dict: {"success": bool, "sl_order_id": str|None, "error": str|None}
    """
    ccxt_sym = _symbol_to_ccxt(symbol)
    sl_side = "sell" if direction == "LONG" else "buy"
    sl_px = _round_price(ccxt_sym, new_sl_price)
    qty = _round_qty(ccxt_sym, remaining_qty)

    result = {"success": False, "sl_order_id": None, "error": None}

    # 🔴 FIX (-4130 v2): switch MOVE path to reduceOnly to bypass Binance's
    # closePosition duplicate-tracker race. Entry SL still uses closePosition in
    # open_position (no prior tracker state = no race). Pre-check inside the helper
    # short-circuits if server already has the target SL (idempotent restarts).
    #
    # qty==0 guard: after TP partials, remaining_qty can round down below the
    # Binance minimum lot (e.g. 0.001 ETH). reduceOnly requires valid qty — fall
    # back to closePosition which ignores qty and always works regardless of size.
    min_lot = 0.0
    try:
        min_lot = float(
            _get_exchange().market(ccxt_sym).get("limits", {}).get("amount", {}).get("min") or 0.0
        )
    except Exception:
        pass
    use_closeposition_fallback = qty <= 0 or (min_lot > 0 and qty < min_lot)
    if use_closeposition_fallback:
        log.warning(
            f"{symbol} qty={qty} below min_lot={min_lot} — using closePosition (ignores qty)"
        )

    try:
        if use_closeposition_fallback:
            sl_result = _place_closeposition_sl_with_retry(
                symbol=symbol, sl_side=sl_side,
                stop_price=sl_px, qty=max(qty, remaining_qty),
            )
        else:
            sl_result = _place_reduceonly_sl_with_retry(
                symbol=symbol,
                sl_side=sl_side,
                stop_price=sl_px,
                qty=qty,
                max_attempts=5,
            )
        result["success"]     = sl_result["success"]
        result["sl_order_id"] = sl_result["sl_order_id"]
        result["error"]       = sl_result["error"]
        if sl_result["success"]:
            log.info(f"SL moved to {sl_px} (order: {sl_result['sl_order_id']})")
        else:
            log.error(f"SL move failed: {sl_result['error']}")
    except Exception as e:
        result["error"] = f"SL move failed (unexpected): {e}"
        log.error(result["error"])

    return result


def update_sl_after_partial(
    symbol: str,
    direction: str,
    sl_price: float,
    new_remaining_qty: float,
) -> dict:
    """After a partial TP close, update the SL order to reflect reduced quantity.

    This cancels the old SL and places a new one for the remaining qty.
    """
    return move_stop_loss(symbol, direction, sl_price, new_remaining_qty)


# ─── Circuit breaker ─────────────────────────────────────────────────────────

class CircuitBreaker:
    """Tracks losses and halts trading if thresholds are breached.

    🔴 RISK: When triggered, requires manual restart (set CIRCUIT_BREAKER_RESET=true).
    """

    def __init__(self):
        self.consecutive_losses: int = 0
        self.daily_start_capital: float = 0.0
        self.daily_start_date: str = ""
        self.tripped: bool = False
        self.trip_reason: str = ""

    def reset_daily(self, capital: float) -> None:
        """Reset daily tracking at start of each UTC day.

        🔴 FIX: If a trip was set on a previous UTC day (e.g. daily drawdown
        from yesterday), auto-clear it on the new day.  Yesterday's drawdown
        cannot exceed today's limit — only today's losses can apply today.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.daily_start_date:
            if self.tripped:
                log.warning(
                    f"CB: New UTC day ({today}) — auto-clearing previous day's trip.\n"
                    f"Old reason: {self.trip_reason}"
                )
                self.tripped = False
                self.trip_reason = ""
            self.daily_start_date = today
            self.daily_start_capital = capital
            log.info(f"Circuit breaker daily reset: start capital = ${capital:.2f}")

    def record_trade(self, pnl: float, capital: float) -> None:
        """Record a completed trade. Checks DAILY DRAWDOWN ONLY.

        The consecutive-loss check has been permanently disabled.

        WHY: consecutive_losses persists in bot_state.json across restarts.
        Pre-restart losses carried forward into the new session, causing false
        CB trips on the very next real loss even after clean restarts. The
        trigger was state-file corruption — not actual risk events.

        The daily drawdown check is the ONLY remaining trip trigger because:
          - It uses real Binance balance (synced from exchange on every startup)
          - It resets to actual balance each UTC day
          - It cannot be faked by phantom trades or JSON state artifacts
          - 5% daily DD on a real account is always a meaningful signal

        🔴 RISK: consecutive_losses is now INFORMATIONAL ONLY — logged for
        monitoring and displayed in heartbeat, but NEVER trips the breaker.
        Use DAILY_LOSS_LIMIT_PCT env var to control maximum daily drawdown.
        """
        # Track consecutive losses for informational logging (not a CB trigger)
        if pnl >= 0:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            log.info(
                f"CB tracker: consecutive_losses={self.consecutive_losses} "
                f"(informational — consecutive-loss trigger permanently disabled)"
            )

        # 🔴 RISK: Daily drawdown — the ONLY remaining CB trigger.
        # Uses real Binance balance, not state-file records. Cannot be faked.
        if self.daily_start_capital > 0:
            daily_dd = (self.daily_start_capital - capital) / self.daily_start_capital * 100
            if daily_dd >= DAILY_LOSS_LIMIT_PCT:
                self.tripped = True
                self.trip_reason = (
                    f"🔴 CIRCUIT BREAKER: Daily drawdown {daily_dd:.1f}% exceeds "
                    f"{DAILY_LOSS_LIMIT_PCT}% limit. Trading halted. "
                    f"Set CIRCUIT_BREAKER_RESET=true to resume."
                )
                log.critical(self.trip_reason)

    def is_tripped(self) -> bool:
        """Check if circuit breaker is tripped.

        Also checks for manual reset via env var.
        """
        if self.tripped:
            # Allow manual reset via .env file (CIRCUIT_BREAKER_RESET=true)
            if os.getenv("CIRCUIT_BREAKER_RESET", "").lower() == "true":
                log.info("Circuit breaker manually reset via CIRCUIT_BREAKER_RESET env var")
                self.tripped = False
                self.trip_reason = ""
                self.consecutive_losses = 0
                # 🔴 FIX: Clear the daily baseline so reset_daily() is forced to
                # re-initialise with current real capital on the next call.
                # Without this, daily_start_capital remains stale (e.g. $114 before
                # losses) causing the drawdown check to re-trip the breaker immediately
                # after reset — even before a new trade is placed.
                self.daily_start_capital = 0.0
                self.daily_start_date = ""
                # Note: Remove CIRCUIT_BREAKER_RESET from .env after bot resumes
        return self.tripped

    def to_dict(self) -> dict:
        """Serialize for state persistence."""
        return {
            "consecutive_losses": self.consecutive_losses,
            "daily_start_capital": self.daily_start_capital,
            "daily_start_date": self.daily_start_date,
            "tripped": self.tripped,
            "trip_reason": self.trip_reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CircuitBreaker":
        """Deserialize from state."""
        cb = cls()
        cb.consecutive_losses = data.get("consecutive_losses", 0)
        cb.daily_start_capital = data.get("daily_start_capital", 0.0)
        cb.daily_start_date = data.get("daily_start_date", "")
        cb.tripped = data.get("tripped", False)
        cb.trip_reason = data.get("trip_reason", "")
        return cb


# ─── Module-level circuit breaker instance ────────────────────────────────────

circuit_breaker = CircuitBreaker()


def is_execution_enabled() -> bool:
    """Check if trading is allowed (not halted by circuit breaker)."""
    return not circuit_breaker.is_tripped()


def get_mode_label() -> str:
    """Return human-readable mode string for Telegram/logging."""
    if circuit_breaker.is_tripped():
        return "🔴 HALTED (circuit breaker)"
    return "🟢 LIVE"
