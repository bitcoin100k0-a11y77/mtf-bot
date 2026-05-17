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

check("v8 Fix K: closePosition wrapper retained for legacy callers",
      "closePosition wrapper" in exec_src,
      "v8 Fix K: closePosition STOP_MARKET retired; wrapper now forwards to STOP_LIMIT helper")

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

# Fix A — trust-id branch present (v8: consolidated into single helper — count >=1)
check("v4 Fix A: TRUSTING id rescue path present",
      "TRUSTING id" in exec_src,
      "v4 Fix A trust-id rescue must exist (v8: consolidated into STOP_LIMIT helper)")

check("v4 Fix A: sl_unverified flag set in executor",
      "sl_unverified" in exec_src and '"sl_unverified": True' in exec_src,
      "executor must mark sl_unverified=True on trust-id rescue")

check("v4 Fix A: sl_unverified propagated through open_position",
      'result["sl_unverified"] = True' in exec_src,
      "open_position must propagate sl_unverified to caller")

# Fix B — diag wrapped in try/except (v8: consolidated — count >=1)
check("v4 Fix B: diag wrapped in try/except",
      "diag raised" in exec_src,
      "_diagnose_sl_verify_fail call must be try-wrapped (v8: consolidated into STOP_LIMIT helper)")

# Fix C / v5 Fix H — verify defaults bumped (v4: 15/0.6 → v5: 20/0.5 = 10s budget)
check("v5 Fix H: max_polls=20 default in _verify_sl_placed",
      "max_polls: int = 20" in exec_src,
      "verify budget must be 20 polls (v5 widened from 15 to 20)")

check("v5 Fix H: poll_delay=0.5 default in _verify_sl_placed",
      "poll_delay: float = 0.5" in exec_src,
      "verify poll delay must be 0.5s (v5 reduced from 0.6 to 0.5)")

check("v5 Fix H: _verify_sl_placed final fetch_order rescue retries 3x",
      "rescue_attempt" in exec_src,
      "final fetch_order rescue must retry on transient API failure")

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

check("v4 Fix F / v5 Fix H: emergency-close path remains in bot.py",
      "SL_LOST" in bot_src and "tg_sl_lost" in bot_src,
      "main loop must retain emergency-close + Telegram alert path")

check("v4 Fix F: verify_existing_sl called in main loop",
      "executor.verify_existing_sl" in bot_src,
      "main loop must call public re-verify helper")

# Fix G — price-distance kill-switch
check("v4 Fix G: price kill-switch present",
      "sl_unverified + adverse" in bot_src,
      "main loop must short-circuit close when adverse move > 0.5%")

# ─────────────────────────────────────────────
# v5 Fix H — false-positive SL_LOST elimination
# ─────────────────────────────────────────────
check("v5 Fix H: verify_existing_sl returns status_code (4-state)",
      "status_code" in exec_src and "'gone_clean'" in exec_src and "'unknown'" in exec_src,
      "verify_existing_sl must return one of alive/gone_clean/lost/unknown")

check("v5 Fix H: verify_existing_sl retries fetch_order on transient failure",
      "for attempt in range(1, 4):" in exec_src,
      "verify_existing_sl must do 3 attempts × 2s before giving up")

check("v5 Fix H: _final_sl_lost_check helper defined in executor.py",
      "def _final_sl_lost_check" in exec_src,
      "positive-evidence helper required to gate emergency close on 'unknown'")

check("v5 Fix H: bot.py consumes new status_code values",
      'chk.get("status_code"' in bot_src and '"gone_clean"' in bot_src and '"unknown"' in bot_src,
      "bot.py main loop must branch on new 4-state status_code from verify_existing_sl")

check("v5 Fix H: bot.py re-arms SL via move_stop_loss before emergency close",
      "move_stop_loss" in bot_src and "re-arm" in bot_src,
      "on status_code='lost' bot must attempt re-arm before nuking")

check("v5 Fix H: bot.py calls _final_sl_lost_check on grace-end + unknown",
      "_final_sl_lost_check" in bot_src,
      "bot must demand positive evidence before emergency-closing on 'unknown'")

# ─────────────────────────────────────────────
# v6 Fix I — kill UNVERIFIED alert at the source (skip-verify-on-NEW)
# ─────────────────────────────────────────────
check("v6 Fix I: fast-path skip-verify present (v8: consolidated)",
      "fast-path skip verify" in exec_src,
      "STOP_LIMIT helper must short-circuit verify when create_order acks NEW/ACCEPTED")

check("v6 Fix I: fast_path marker flag returned from helper (v8: consolidated)",
      '"fast_path": True' in exec_src,
      "STOP_LIMIT helper must surface fast_path=True so bot.py can tighten cadence")

check("v6 Fix I: bot.py reads fast_path to tighten first re-verify cadence",
      "fast_path" in bot_src and "+20s" in bot_src,
      "execute_entry must tighten first re-verify to 20s on fast-path trades")

check("v6 Fix I: tg_sl_unverified call-site present (v8: consolidated to 1)",
      exec_src.count("tg_sl_unverified(symbol") >= 1,
      "STOP_LIMIT helper's empty-status fallback must still fire Telegram alert")

# ─────────────────────────────────────────────
# v7 Fix J — kill premature TIME-labeled closes
# ─────────────────────────────────────────────
check("v7 Fix J-1: check_exits preserves pre-set close_reason",
      'pre_reason or "TIME"' in bot_src,
      "check_exits must return pre-set close_reason if a sl_unverified branch labeled it")

check("v7 Fix J-2: MAX_HOLD reads from MAX_HOLD_BARS env",
      "MAX_HOLD_BARS" in bot_src,
      "MAX_HOLD must be env-configurable via os.getenv('MAX_HOLD_BARS', ...)")

check("v7 Fix J-3: verify_existing_sl distinguishes pos=None from qty<=0",
      "get_open_position returned None" in exec_src,
      "DEAD-status branch must NOT silently treat None as flat — falls through to retry")

check("v7 Fix J-4: _final_sl_lost_check distinguishes pos=None from qty<=0",
      "do NOT default to 'flat'" in exec_src,
      "final-check flat decision must require positive position fetch")

# ─────────────────────────────────────────────
# v8 Fix K — strategy revert to v4 + STOP_LIMIT SL with bracket
# ─────────────────────────────────────────────
check("v8 Fix K: PULL_PCT reverted to v4 0.5%",
      "PULL_PCT    = 0.005" in bot_src,
      "v4 pullback width")

check("v8 Fix K: ATR_REL reverted to v4 0.70",
      "ATR_REL     = 0.70" in bot_src,
      "v4 chop filter")

check("v8 Fix K: SL_MULT reverted to v4 1.8",
      "SL_MULT     = 1.8" in bot_src,
      "v4 SL distance")

check("v8 Fix K: TP3_MULT reverted to v4 18.0",
      "TP3_MULT    = 18.0" in bot_src,
      "v4 TP3 distance")

check("v8 Fix K: TP1_FRAC reverted to v4 0.40",
      "TP1_FRAC    = 0.40" in bot_src,
      "v4 TP1 fraction")

check("v8 Fix K: TP2_FRAC reverted to v4 0.30",
      "TP2_FRAC    = 0.30" in bot_src,
      "v4 TP2 fraction")

check("v8 Fix K: MAX_HOLD_BARS default reverted to 48",
      'MAX_HOLD_BARS", "48"' in bot_src,
      "v4 max hold 48 bars (4h)")

check("v8 Fix K: 24/7 trading — session gate commented out",
      "Session filter — DISABLED" in bot_src,
      "session gate must be commented out for 24/7")

check("v8 Fix K: RSI cross-bar pattern (LONG)",
      "rp < Cfg.RSI_LO and rc > rp" in bot_src,
      "v4 RSI gate uses exact cross-bar for LONG")

check("v8 Fix K: RSI cross-bar pattern (SHORT)",
      "rp > Cfg.RSI_HI and rc < rp" in bot_src,
      "v4 RSI gate uses exact cross-bar for SHORT")

check("v9 Fix L: SL reverted to STOP_MARKET (was STOP_LIMIT in v8)",
      'type="stop_market"' in exec_src,
      "v9 reverts v8 STOP_LIMIT after live regression — back to v7 STOP_MARKET reduceOnly")

check("v9 Fix L: post-partial SL re-arm disabled",
      "_post_partial_sl_rearm" in bot_src and "# _post_partial_sl_rearm" in bot_src,
      "re-arm helper kept but call-sites commented out — STOP_MARKET reduceOnly oversize is benign")

# ─────────────────────────────────────────────
# v10 Fix M — kill false-positive SL_LOST + fix TypeError bug
# ─────────────────────────────────────────────
check("v10 Fix M-0: every close_full_position call site has direction arg",
      # All call sites must have 'sym, ot["dir"]' nearby OR multiline form
      "close_full_position(sym)\n" not in bot_src
      and "close_full_position(sym)" not in bot_src.replace("close_full_position(sym, ot", ""),
      "every close_full_position(sym) call MUST pass ot[\"dir\"] — TypeError bug in v5-v9 sl_unverified branches")

check("v10 Fix M-1: lost branch uses _final_sl_lost_check positive-evidence gate",
      "M-1 _final_sl_lost_check" in bot_src,
      "lost branch must demand positive evidence before emergency close — prevents false 'lost' on race")

check("v10 Fix M-2: tg_sl_lost suppressed when close was no-op",
      "SL_GHOST_RESOLVED" in bot_src and "close_no_op" in bot_src,
      "if close_full_position returned fill_price=0.0, position already 0 — replace alarming SL_LOST with informational notice")

# ─────────────────────────────────────────────
# v11 Fix O — root-cause defense-in-depth for SL_LOST
# ─────────────────────────────────────────────
check("v11 Fix O-1: clientOrderId generated for SL placement",
      "newClientOrderId" in exec_src and "botsl-" in exec_src,
      "every SL placement must include bot-generated newClientOrderId for fallback lookup")

check("v11 Fix O-1: clientOrderId propagated through executor → bot",
      "sl_client_oid" in exec_src and "exec_sl_cid" in bot_src,
      "clientOrderId flows from placement helper → open_position result → ot dict")

check("v11 Fix O-2: verify_existing_sl falls back to origClientOrderId on -2013",
      "origClientOrderId" in exec_src,
      "verify must retry via clientOrderId when orderId returns -2013")

check("v11 Fix O-2: attribute-fingerprint rescue scan after -2013",
      "ATTRIBUTE" in exec_src and "saw_2013" in exec_src,
      "verify_existing_sl must scan open_orders by attribute when orderId is -2013")

check("v11 Fix O-3: validate_position_side helper defined",
      "def validate_position_side" in exec_src,
      "hedge-mode mismatch validator required for fail-fast on misconfig")

check("v11 Fix O-3: validate_position_side called at bot startup",
      "validate_position_side" in bot_src and "HEDGE MODE MISMATCH" in bot_src,
      "main() must call validator before first trade")

check("v11 Fix O-4: -2013-specific backoff (10s) in verify_existing_sl",
      "sleep_s = 10.0 if saw_2013" in exec_src,
      "10s backoff on -2013 (Binance internal id-index lag); 2s otherwise")

check("v11 Fix O-5: cancellation reason captured from fetch_order info",
      "cancelReason" in exec_src,
      "DEAD-status branch must capture Binance cancelReason field")

check("v11 Fix O-6: _place_deadman_sl helper defined",
      "def _place_deadman_sl" in exec_src,
      "deadman-switch backup SL helper required")

check("v11 Fix O-6: open_position places deadman SL after primary",
      "DEADMAN" in exec_src and "deadman_sl_id" in exec_src,
      "deadman backup placed after primary SL success")

check("v11 Fix O-6: deadman_sl_id stored on trade dict",
      "deadman_sl_id" in bot_src,
      "bot.py tracks deadman SL id alongside primary")

check("v11 Fix O-7: active liveness ping for non-sl_unverified trades",
      "liveness ping" in bot_src,
      "main loop must verify SL liveness on every open trade each cycle")

check("v11 Fix O-8: SL_CREATE_RAW telemetry on placement",
      "SL_CREATE_RAW" in exec_src,
      "telemetry log on every SL create_order response")

check("v11 Fix O-8: SL_FETCH_RAW telemetry on verify",
      "SL_FETCH_RAW" in exec_src,
      "telemetry log on every verify_existing_sl fetch_order attempt")

# ─────────────────────────────────────────────
# v12 Fix P — margin-aware execution + Telegram noise gate
# ─────────────────────────────────────────────
check("v12 Fix P-1: get_signal accepts free_margin parameter",
      "def get_signal(d5, capital, free_margin=None)" in bot_src,
      "get_signal must accept free_margin for margin-aware sizing")

check("v12 Fix P-1: sizing uses free_margin when available",
      "_sizing_base = free_margin" in bot_src,
      "sizing base should prefer live FREE margin over total capital")

check("v12 Fix P-1: caller passes live_free to get_signal",
      'free_margin=S.get("live_free")' in bot_src,
      "main loop must pass S['live_free'] when calling get_signal")

check("v12 Fix P-2: MAX_CONCURRENT cfg defined",
      "MAX_CONCURRENT" in bot_src and "MAX_CONCURRENT_POSITIONS" in bot_src,
      "Cfg.MAX_CONCURRENT must exist for concurrency cap")

check("v12 Fix P-2: entry gate blocks at MAX_CONCURRENT",
      "BLOCKED — concurrent" in bot_src and "Cfg.MAX_CONCURRENT" in bot_src,
      "main loop must block new entries when at concurrency cap")

check("v12 Fix P-3: min-notional silent-skip gate at entry",
      "min notional" in bot_src and "_skip_for_min_notional" in bot_src,
      "main loop must skip entry silently when free < min notional")

check("v12 Fix P-4: tg_exec_error throttled per (sym, action)",
      "_TG_ERROR_LAST" in bot_src and "THROTTLED" in bot_src,
      "tg_exec_error must throttle duplicates to one per 30 min per key")

check("v12 Fix P-5: executor error message suggests needed leverage",
      "FUTURES_LEVERAGE>=" in exec_src,
      "wallet-too-small error must compute and suggest needed leverage")

check("v8 Fix K: closePosition helper delegates to reduceOnly STOP_LIMIT path",
      "closePosition wrapper" in exec_src and "_place_reduceonly_sl_with_retry" in exec_src,
      "closePosition path must forward to STOP_LIMIT helper")

check("v8 Fix K: _order_matches_type has stop_limit case",
      'expected_type == "stop_limit"' in exec_src,
      "type matcher must recognize STOP_LIMIT for idempotency pre-check")

check("v8 Fix K: post-partial SL re-arm helper",
      "def _post_partial_sl_rearm" in bot_src,
      "partial TP1/TP2 must re-arm SL with new qty")

check("v8 Fix K: post-partial re-arm called after TP1",
      "post-TP1" in bot_src,
      "TP1 path must invoke re-arm")

check("v8 Fix K: post-partial re-arm called after TP2",
      "post-TP2" in bot_src,
      "TP2 path must invoke re-arm")

# ─────────────────────────────────────────────
# 6. STRUCTURE SANITY
# ─────────────────────────────────────────────
print("\n[5] Structure Sanity")

check(f"executor.py line count in range 580–2500 (got {len(exec_lines)})",
      580 <= len(exec_lines) <= 2500,
      f"unexpected line count may indicate missing or duplicate content")

check(f"bot.py line count in range 750–2100 (got {len(bot_lines)})",
      750 <= len(bot_lines) <= 2100,
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
