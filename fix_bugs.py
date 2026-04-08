"""
fix_bugs.py -- Patches two critical live-trading bugs in Champion v4.1

Bug 1 (executor.py): _round_qty uses wrong math when CCXT returns precision
  as step size (0.001). 10**0.001 = 1.002, math.floor(0.567*1.002)=0. FIXED.

Bug 2 (bot.py): S["capital"] never synced from real Binance balance on live
  startup. Bot used $10,000 (paper IC) for sizing. FIXED.

Run once from C:\\champion, then delete this file.
"""
import pathlib, sys

BASE = pathlib.Path(__file__).parent

# Bug 1: Fix _round_qty in executor.py
executor_file = BASE / "executor.py"
if not executor_file.exists():
    print("ERROR: executor.py not found"); sys.exit(1)

executor_text = executor_file.read_text(encoding="utf-8")

OLD_RQ = '''def _round_qty(symbol: str, qty: float) -> float:
    """Round quantity to exchange precision to avoid Binance rejection."""
    ex = _get_exchange()
    market = ex.market(symbol)
    precision = market.get("precision", {}).get("amount", 8)
    # Use floor to never exceed available balance
    factor = 10 ** precision
    return math.floor(qty * factor) / factor'''

NEW_RQ = '''def _round_qty(symbol: str, qty: float) -> float:
    """Round quantity to exchange precision. Handles both CCXT precision modes:
    TICK_SIZE (float < 1, e.g. 0.001) and DECIMAL_PLACES (int >= 1, e.g. 3).
    Old code: 10**0.001=1.002, floor(0.567*1.002)=0. Now fixed.
    """
    ex = _get_exchange()
    market = ex.market(symbol)
    precision = market.get("precision", {}).get("amount", 8)
    if isinstance(precision, float) and precision < 1:
        step = precision
        return math.floor(qty / step) * step
    else:
        factor = 10 ** int(precision)
        return math.floor(qty * factor) / factor'''

if OLD_RQ in executor_text:
    executor_file.write_text(executor_text.replace(OLD_RQ, NEW_RQ), encoding="utf-8")
    print("[OK] executor.py: _round_qty fixed")
elif "TICK_SIZE" in executor_text or "step = precision" in executor_text:
    print("[OK] executor.py: already patched")
else:
    print("[!!] executor.py: pattern not found -- check manually")

# Bug 2: Sync real balance to S[capital] in bot.py
bot_file = BASE / "bot.py"
bot_text = bot_file.read_text(encoding="utf-8")

OLD_BAL = '''            bal = executor.get_futures_balance()
            log.info(f"Futures wallet balance: ${bal:.2f} USDT")
            tg(f"\\U0001f4b0 Futures balance: ${bal:.2f} USDT")
        except Exception as e:'''

NEW_BAL = '''            bal = executor.get_futures_balance()
            log.info(f"Futures wallet balance: ${bal:.2f} USDT")
            tg(f"\\U0001f4b0 Futures balance: ${bal:.2f} USDT")
            if bal > 0:
                S["capital"] = bal
                S["peak"]    = max(S.get("peak", bal), bal)
                log.info(f"Capital synced from Binance: ${bal:.2f}")
        except Exception as e:'''

if OLD_BAL in bot_text:
    bot_file.write_text(bot_text.replace(OLD_BAL, NEW_BAL), encoding="utf-8")
    print("[OK] bot.py: capital synced from Binance balance on live startup")
elif "Capital synced from Binance" in bot_text:
    print("[OK] bot.py: already patched")
else:
    print("[!!] bot.py: pattern not found -- check manually")

# Verify
print("\n-- Verification --")
ex2 = executor_file.read_text(encoding="utf-8")
print("[OK] executor.py fix" if "step = precision" in ex2 else "[!!] executor.py NOT patched")
bot2 = bot_file.read_text(encoding="utf-8")
print("[OK] bot.py fix" if "Capital synced from Binance" in bot2 else "[!!] bot.py NOT patched")
print("\n[DONE] Run: nssm stop championbot && nssm start championbot")
