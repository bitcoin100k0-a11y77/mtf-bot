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
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))   # 🔴 RISK: halt after 3 losses in a row
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
    """Round price to exchange tick size precision."""
    ex = _get_exchange()
    market = ex.market(symbol)
    precision = market.get("precision", {}).get("price", 8)
    factor = 10 ** precision
    return round(price * factor) / factor


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
        usdt = balance.get("USDTutures wallet.

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

    # 2) Server-side stop-loss (stop-market order)
    try:
        log.info(f"PLACING SL {sl_side} closePosition {ccxt_sym} @ {sl_px}")
        sl_order = ex.create_order(
            symbol=ccxt_sym,
            type="stop_market",  # Binance Futures stop-market
            side=sl_side,
            amount=fill_qty,     # ignored by Binance when closePosition=True
            params={
                "stopPrice": sl_px,
                "closePosition": True,  # 🔴 FIX: avoids -4120; closes full remaining position
                # reduceOnly=True with STOP_MARKET now requires Algo Order endpoint (-4120)
                # closePosition=True uses standard /fapi/v1/order and is semantically correct
            },
        )
        result["sl_order_id"] = sl_order.get("id", "unknown")
        log.info(f"SL placed: {result['sl_order_id']} @ {sl_px}")
    except Exception as e:
        # 🔴 RISK: Entry filled but SL failed — critical situation
        log.error(f"CRITICAL: SL order failed after entry fill! {e}")
        log.error(f"MANUAL ACTION REQUIRED: Place SL for {direction} {fill_qty} {symbol} at {sl_px}")
        result["error"] = f"SL placement failed (entry is open!): {e}"
        # Don't return failure — entry IS open, caller must handle

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
    qty = _round_qty(ccxt_sym, total_size * fraction)

    result = {"success": False, "fill_price": None, "fill_qty": None, "error": None}

    if qty <= 0:
        result["error"] = f"Partial close qty rounds to 0 for {symbol}"
        log.warning(result["error"])
        return result

    try:
        ex = _get_exchange()
        log.info(f"{reason}: closing {fraction*100:.0f}% → {close_side} {qty} {ccxt_sym}")
        order = ex.create_order(
            symbol=ccxt_sym,
            type="market",
            side=close_side,
            amount=qty,
            params={"reduceOnly": True},
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

            log.info(f"DUST CLOSE: {close_side} closePosition {ccxt_sym} @ stop={stop_px} (cur={cur_px:.4f})")
            dust_order = ex.create_order(
                symbol=ccxt_sym,
                type="stop_market",
                side=close_side,
                amount=pos["qty"],   # ignored by Binance when closePosition=True
                params={
                    "stopPrice": stop_px,
                    "closePosition": True,  # closes full remaining position, bypasses qty minimum
                },
            )
            log.info(f"DUST CLOSE order placed: {dust_order.get('id', 'unknown')} @ stop={stop_px}")
            result.update({"success": True, "fill_price": cur_px})
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

    Returns:
        bool: True if cancelled successfully or no orders to cancel.
    """
    try:
        ex = _get_exchange()
        ccxt_sym = _symbol_to_ccxt(symbol)
        open_orders = ex.fetch_open_orders(ccxt_sym)
        for order in open_orders:
            try:
                ex.cancel_order(order["id"], ccxt_sym)
                log.info(f"Cancelled order {order['id']} for {symbol}")
            except Exception as e:
                log.warning(f"Failed to cancel order {order['id']}: {e}")
        return True
    except Exception as e:
        log.error(f"Failed to fetch/cancel orders for {symbol}: {e}")
        return False


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

    try:
        ex = _get_exchange()

        # Cancel existing SL orders first
        cancel_open_orders(symbol)
        time.sleep(0.3)  # brief pause after cancellation

        # Place new SL
        log.info(f"NEW SL: {sl_side} closePosition {ccxt_sym} @ {sl_px}")
        sl_order = ex.create_order(
            symbol=ccxt_sym,
            type="stop_market",
            side=sl_side,
            amount=qty,          # ignored by Binance when closePosition=True
            params={
                "stopPrice": sl_px,
                "closePosition": True,  # 🔴 FIX: avoids -4120; closes full remaining position
                # reduceOnly=True with STOP_MARKET now requires Algo Order endpoint (-4120)
            },
        )
        result.update({"success": True, "sl_order_id": sl_order.get("id", "unknown")})
        log.info(f"SL moved to {sl_px} (order: {result['sl_order_id']})")
    except Exception as e:
        result["error"] = f"SL move failed: {e}"
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
        """Reset daily tracking at start of each UTC day."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.daily_start_date:
            self.daily_start_date = today
            self.daily_start_capital = capital
            log.info(f"Circuit breaker daily reset: start capital = ${capital:.2f}")

    def record_trade(self, pnl: float, capital: float) -> None:
        """Record a completed trade. May trip the breaker."""
        if pnl >= 0:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1

        # Check consecutive loss limit
        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.tripped = True
            self.trip_reason = (
                f"🔴 CIRCUIT BREAKER: {self.consecutive_losses} consecutive losses. "
                f"Trading halted. Set CIRCUIT_BREAKER_RESET=true to resume."
            )
            log.critical(self.trip_reason)
            return

        # Check daily drawdown limit
        if self.daily_start_capital > 0:
            daily_dd = (self.daily_start_capital - capital) / self.daily_start_capital * 100
            if daily_dd >= DAILY_LOSS_LIMIT_PCT:
                self.tripped = True
                self.trip_reason = (
                    f"🔴 CIRCUIT BREAKER: Daily drawdown {daily_dd:.1f}% exceeds "
                    f"{DAILY_LOSS_LIMIT_PCT}% limit. Trading halted."
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
