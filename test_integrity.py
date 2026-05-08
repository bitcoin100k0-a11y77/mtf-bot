"""
# test_integrity.py
MTF Champion v4.1 — Comprehensive Integrity Test Suite
=======================================================
Checks every critical patch, every known corruption pattern,
structure sanity, and duplicate-definition detection.

Run from C:\\champion\\:
    python test_integrity.py

Exit code 0 = all clean. Exit code 1 = failures found.
Re-run after any fix until 0 failures.
"""
import ast
import re
import sys

PASS = "\u2705 PASS"
FAIL = "\u274c FAIL"
errors: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS}  {name}")
    else:
        msg = f"  {FAIL}  {name}"
        if detail:
            msg += f"  \u2192  {detail}"
        print(msg)
        errors.append(name)


def count_defs(src: str, fname: str) -> int:
    return len(re.findall(rf"^def {re.escape(fname)}\b", src, re.MULTILINE))


# ─────────────────────────────────────────────
# 1. READ FILES
# ─────────────────────────────────────────────
print("=" * 60)
print("MTF Champion v4.1 — Integrity Check")
print("=" * 60)

try:
    exec_src = open("executor.py", encoding="utf-8").read()
    bot_src  = open("bot.py",      encoding="utf-8").read()
except FileNotFoundError as e:
    print(f"\n\u274c Cannot read files: {e}")
    print("Run this script from C:\\champion\\")
    sys.exit(1)

exec_lines = exec_src.splitlines()
bot_lines  = bot_src.splitlines()

# ─────────────────────────────────────────────
# 2. SYNTAX / AST
# ─────────────────────────────────────────────
print("\n[1] Syntax Check")
for fname, src in [("executor.py", exec_src), ("bot.py", bot_src)]:
    try:
        ast.parse(src)
        check(f"{fname} parses clean (no SyntaxError)", True)
    except SyntaxError as e:
        check(f"{fname} parses clean", False, f"Line {e.lineno}: {e.msg}")
        print("\n\u274c Aborting — syntax errors must be fixed before other checks.")
        sys.exit(1)

# ─────────────────────────────────────────────
# 3. KNOWN CORRUPTION PATTERNS
# ─────────────────────────────────────────────
print("\n[2] Corruption Scan")

# The specific corruption we found: docstring text leaked into code
check("No 'USDTutures' corruption in executor.py",
      "USDTutures" not in exec_src,
      "get_futures_balance still has the merged docstring/code corruption")

check("No 'USDTutures' corruption in bot.py",
      "USDTutures" not in bot_src)

# Detect unterminated string literals (heuristic: odd number of triple-quotes)
for fname, src in [("executor.py", exec_src), ("bot.py", bot_src)]:
    tq_count = src.count('"""')
    check(f"{fname} triple-quote count is even ({tq_count})",
          tq_count % 2 == 0,
          f"odd triple-quote count ({tq_count}) suggests a broken docstring")

# ─────────────────────────────────────────────
# 4. EXECUTOR.PY — CRITICAL PATCHES
# ─────────────────────────────────────────────
print("\n[3] executor.py — Critical Patches")

check("close_full_position() defined",
      "def close_full_position" in exec_src)

check("STOP_MARKET fallback present",
      "STOP_MARKET" in exec_src,
      "dust-position close fallback for sub-minimum lot sizes is missing")

check("closePosition=True param in fallback",
      '"closePosition": True' in exec_src or "'closePosition': True" in exec_src,
      "closePosition=True required to close dust positions regardless of qty")

check("reduceOnly present (partial TP closes)",
      "reduceOnly" in exec_src,
      "partial TP close orders need reduceOnly=True")

check("get_futures_balance() defined",
      "def get_futures_balance" in exec_src)

check("get_futures_balance uses USDT key correctly",
      'balance.get("USDT"' in exec_src or "balance.get('USDT'" in exec_src,
      "corrupted key 'USDTutures wallet.' or similar")

check("CircuitBreaker class defined",
      "class CircuitBreaker" in exec_src)

check("is_tripped() method defined",
      "def is_tripped" in exec_src)

check("record_trade() method defined",
      "def record_trade" in exec_src)

check("_get_exchange() defined",
      "def _get_exchange" in exec_src)

check("cancel_open_orders() defined",
      "def cancel_open_orders" in exec_src)

check("get_open_position() defined",
      "def get_open_position" in exec_src)

check("get_futures_account_state() defined",
      "def get_futures_account_state" in exec_src)

check("executor returns equity dict shape",
      '"equity"' in exec_src and '"wallet"' in exec_src)

check("_fetch_current_sl helper defined",
      "def _fetch_current_sl" in exec_src)

check("_sl_already_at helper defined",
      "def _sl_already_at" in exec_src)

check("_place_reduceonly_sl_with_retry defined",
      "def _place_reduceonly_sl_with_retry" in exec_src)

check("_get_hedge_mode probe defined",
      "def _get_hedge_mode" in exec_src)

check("move_stop_loss uses reduceOnly path",
      "_place_reduceonly_sl_with_retry" in exec_src)

# ─────────────────────────────────────────────
# 5. BOT.PY — CRITICAL PATCHES
# ─────────────────────────────────────────────
print("\n[4] bot.py — Critical Patches")

check("execute_full_close() returns bool (-> bool annotation)",
      "def execute_full_close" in bot_src and "-> bool" in bot_src,
      "must return bool so caller knows if close succeeded")

check("close_ok assignment pattern present",
      "close_ok = execute_full_close" in bot_src,
      "main loop must capture return value of execute_full_close")

check("retry logic on failed close (CLOSE FAILED log)",
      "CLOSE FAILED" in bot_src,
      "failed close must log CLOSE FAILED and keep trade open")

check("MAX_HOLD retry — bars reset on failed close",
      "MAX_HOLD" in bot_src and 'ot["bars"]' in bot_src,
      "on failed close, ot['bars'] must be reset to MAX_HOLD to retry next cycle")

check("P&L booking gated on close_ok",
      'S["capital"] +=' in bot_src,
      "capital must only update after confirmed close")

check("trade pop gated (S['open_trades'].pop)",
      'S["open_trades"].pop' in bot_src or "open_trades.pop" in bot_src,
      "trade must only be removed from open_trades after confirmed close")

check("circuit_breaker.record_trade() called after close",
      "circuit_breaker.record_trade" in bot_src,
      "circuit breaker must receive P&L after every closed trade")

check("tg_exec_error called on failed close",
      "tg_exec_error" in bot_src,
      "Telegram alert required on execution failure")

check("bot.py reads live equity per cycle",
      "get_futures_account_state()" in bot_src)

check("bot.py stores S[live_equity]",
      "live_equity" in bot_src)

# ─────────────────────────────────────────────
# 5b. v4 SL VERIFY-FAIL FIXES (Fix A through Fix G)
# ─────────────────────────────────────────────
print("\n[4b] v4 SL verify-fail fixes")

# Fix A — trust-id branch in BOTH retry helpers (count must be >=2)
check("v4 Fix A: TRUSTING id present in BOTH executor helpers",
      exec_src.count("TRUSTING id") >= 2,
      "trust-id rescue path must exist in reduceOnly + closePosition helpers")

check("v4 Fix A: sl_unverified flag set in executor",
      "sl_unverified" in exec_src and '"sl_unverified": True' in exec_src,
      "executor must mark sl_unverified=True on trust-id rescue")

check("v4 Fix A: sl_unverified propagated through open_position",
      'result["sl_unverified"] = True' in exec_src,
      "open_position must propagate sl_unverified to caller")

# Fix B — diag wrapped in try/except (count must be >=2)
check("v4 Fix B: diag wrapped in try/except in BOTH helpers",
      exec_src.count("diag raised") >= 2,
      "_diagnose_sl_verify_fail call must be try-wrapped in both retry helpers")

# Fix C — verify defaults bumped
check("v4 Fix C: max_polls=15 default in _verify_sl_placed",
      "max_polls: int = 15" in exec_src,
      "verify budget must be 15 polls")

check("v4 Fix C: poll_delay=0.6 default in _verify_sl_placed",
      "poll_delay: float = 0.6" in exec_src,
      "verify poll delay must be 0.6s")

# Fix D — extended diag telemetry
check("v4 Fix D: clientId in diag log",
      "clientId={info.get('clientOrderId')" in exec_src,
      "diag must log clientOrderId for next-failure debugging")

check("v4 Fix D: status_raw in diag log",
      "status_raw=" in exec_src,
      "diag must log raw Binance status alongside ccxt unified status")

# new public verify_existing_sl helper
check("v4: verify_existing_sl public helper defined",
      "def verify_existing_sl" in exec_src,
      "public re-verify helper required for bot.py Fix F deferred check")

# Fix E — telegram helpers
check("v4 Fix E: tg_sl_unverified defined in bot.py",
      "def tg_sl_unverified" in bot_src,
      "Telegram alert for trust-id path must exist")

check("v4 Fix E: tg_sl_resolved defined in bot.py",
      "def tg_sl_resolved" in bot_src,
      "Telegram alert for resolved trust-id must exist")

check("v4 Fix E: tg_sl_lost defined in bot.py",
      "def tg_sl_lost" in bot_src,
      "Telegram alert for lost SL must exist")

# Fix F — 60s re-verify in main loop
check("v4 Fix F: sl_unverified_until referenced in bot.py",
      "sl_unverified_until" in bot_src,
      "main loop must store grace expiry timestamp")

check("v4 Fix F: sl_unverified RESOLVED log line",
      "sl_unverified RESOLVED" in bot_src,
      "main loop must log resolution on successful re-verify")

check("v4 Fix F: TRULY MISSING after grace log line",
      "TRULY MISSING after grace" in bot_src,
      "main loop must log + emergency-close when grace elapses")

check("v4 Fix F: verify_existing_sl called in main loop",
      "executor.verify_existing_sl" in bot_src,
      "main loop must call public re-verify helper")

# Fix G — price-distance kill-switch
check("v4 Fix G: price kill-switch present",
      "sl_unverified + adverse" in bot_src,
      "main loop must short-circuit close when adverse move > 0.5%")

# ─────────────────────────────────────────────
# 6. STRUCTURE SANITY
# ─────────────────────────────────────────────
print("\n[5] Structure Sanity")

check(f"executor.py line count in range 580–1900 (got {len(exec_lines)})",
      580 <= len(exec_lines) <= 1900,
      f"unexpected line count may indicate missing or duplicate content")

check(f"bot.py line count in range 750–1500 (got {len(bot_lines)})",
      750 <= len(bot_lines) <= 1500,
      f"unexpected line count may indicate missing or duplicate content")

# Duplicate function definitions (the corruption we saw causes duplicates)
for fn in ["close_full_position", "get_futures_balance", "_get_exchange",
           "cancel_open_orders", "get_open_position"]:
    n = count_defs(exec_src, fn)
    check(f"executor.py: {fn}() defined exactly once (found {n}x)",
          n == 1, f"duplicate or missing definition")

for fn in ["execute_full_close"]:
    n = count_defs(bot_src, fn)
    check(f"bot.py: {fn}() defined exactly once (found {n}x)",
          n == 1, f"duplicate or missing definition")

# ─────────────────────────────────────────────
# 7. SECURITY
# ─────────────────────────────────────────────
print("\n[6] Security")

for fname, src in [("executor.py", exec_src), ("bot.py", bot_src)]:
    # No hardcoded key-looking strings (starts with AKIA = AWS, or long hex)
    has_akia = "AKIA" in src
    has_hardcoded_secret = bool(re.search(r'(?:api_key|secret)\s*=\s*["\'][A-Za-z0-9+/]{20,}["\']', src, re.IGNORECASE))
    check(f"{fname} has no hardcoded secrets",
          not has_akia and not has_hardcoded_secret,
          "found what looks like a hardcoded credential")

# executor.py uses .get() then validates — that's acceptable.
# bot.py should NOT use .get() with silent empty default for critical keys.
has_bot_silent_get = bool(re.search(r'os\.environ\.get\(["\']BINANCE[^)]*,\s*["\']["\']', bot_src))
check("bot.py does not silently swallow missing API keys via .get('KEY', '')",
      not has_bot_silent_get,
      "use os.environ['KEY'] so missing keys raise KeyError immediately")

# executor.py: .get() is OK only if followed by explicit validation (RuntimeError/raise)
exec_has_get = bool(re.search(r'os\.environ\.get\(["\']BINANCE', exec_src))
exec_has_validation = "if not api_key" in exec_src or "RuntimeError" in exec_src
check("executor.py: if .get() used for API keys, explicit validation must follow",
      not exec_has_get or exec_has_validation,
      "executor.py uses .get() without downstream validation — keys could silently be empty")

# ─────────────────────────────────────────────
# RESULT
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
if errors:
    print(f"\u274c FAILED — {len(errors)} issue(s) found:\n")
    for i, e in enumerate(errors, 1):
        print(f"  {i:2d}. {e}")
    print("\nFix the issues above and re-run: python test_integrity.py")
    sys.exit(1)
else:
    print("\u2705 ALL CHECKS PASSED — bot files are clean and ready")
    print("   executor.py:", len(exec_lines), "lines")
    print("   bot.py     :", len(bot_lines), "lines")
    sys.exit(0)
