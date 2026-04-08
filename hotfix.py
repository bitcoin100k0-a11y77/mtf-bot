"""
apply_hotfix.py — Champion v4.1 cleanup for VPS (post fix_bugs.py state)
Run once from C:\\champion, then delete.

Fixes:
  1. Delete stale state file (phantom trade + wrong capital)
  2. Remove premature reset_daily call in bot.py (fires before balance sync)
  3. Add reset_daily(bal) after capital sync in bot.py
  4. Fix STATE_FILE: BOT_DATA_DIR replaces RAILWAY_VOLUME_MOUNT_PATH
  5. Fix CB Telegram message (.env not Railway)
  6. Remove TRADING_MODE var from executor.py
  7. Remove all paper-mode branches from executor.py
  8. Fix Railway labels in executor.py logs
  9. Simplify get_mode_label to live-only
"""
import pathlib, re, sys

BASE = pathlib.Path(__file__).parent

# ── 1. Delete stale state file ───────────────────────────────────────────────
state = pathlib.Path(r"C:\botdata\bot_state.json")
if state.exists():
    state.unlink()
    print("[OK] Deleted stale state file — fresh start")
else:
    print("[INFO] State file not found — already clean")

# ── 2–5. Patch bot.py ────────────────────────────────────────────────────────
bot_path = BASE / "bot.py"
bot = bot_path.read_text(encoding="utf-8")
orig = bot

# 2. Remove premature reset_daily (fires before balance sync → false 99% drawdown)
bot = re.sub(
    r'    executor\.circuit_breaker\.reset_daily\(S\["capital"\]\)\n\n'
    r'    log\.info\(f"State: checks=',
    '    # reset_daily called AFTER balance sync below — not here\n\n'
    '    log.info(f"State: checks=',
    bot
)

# 3. Add reset_daily AFTER capital sync with real balance
# fix_bugs.py added "(was ${Cfg.IC:.2f})" to the log line — match that
bot = bot.replace(
    '                log.info(f"Capital synced from Binance: ${bal:.2f} (was ${Cfg.IC:.2f})")',
    '                log.info(f"Capital synced from Binance: ${bal:.2f}")\n'
    '                # \U0001f534 RISK: Reset CB baseline NOW with real capital, not stale IC\n'
    '                executor.circuit_breaker.reset_daily(bal)'
)

# 4. Fix STATE_FILE env var
bot = bot.replace(
    'Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")) / "bot_state.json"',
    r'Path(os.getenv("BOT_DATA_DIR", os.getenv("RAILWAY_VOLUME_MOUNT_PATH", r"C:\botdata"))) / "bot_state.json"'
)

# 5. Fix CB Telegram message
bot = bot.replace(
    'set CIRCUIT_BREAKER_RESET=true in Railway env vars, '
    'then remove it after bot resumes.',
    'set CIRCUIT_BREAKER_RESET=true in .env on the VPS, '
    'then restart. Remove the var after trading resumes.'
)

# Also fix the executor.TRADING_MODE live check — bot is always live now
bot = bot.replace(
    '    # Initialise exchange connection on startup\n    if executor.TRADING_MODE == "live":\n        try:',
    '    # Initialise exchange connection on startup (live-only)\n    try:'
)

if bot != orig:
    bot_path.write_text(bot, encoding="utf-8")
    print("[OK] bot.py patched")
else:
    print("[SKIP] bot.py — no changes (already patched?)")

# ── 6–9. Patch executor.py ───────────────────────────────────────────────────
ex_path = BASE / "executor.py"
ex = ex_path.read_text(encoding="utf-8")
orig_ex = ex

# 6. Remove TRADING_MODE declaration
ex = re.sub(
    r'TRADING_MODE = os\.getenv\("TRADING_MODE".*?\n',
    '# Bot is live-only — paper mode removed\n',
    ex
)

# 8. Fix Railway labels
ex = ex.replace('Railway public IPv4:', 'VPS public IPv4:')
ex = ex.replace('Railway public IPv4', 'VPS public IPv4')
ex = ex.replace(
    'Add this to Binance API key whitelist to enable Futures.',
    'Add this to Binance API key whitelist if not already done.'
)
ex = ex.replace(
    "its visible even if Railway redacts logs",
    'its visible for Binance whitelist verification'
)

# Fix CB comments
ex = ex.replace(
    'Allow manual reset via Railway env var',
    'Allow manual reset via .env file (CIRCUIT_BREAKER_RESET=true)'
)
ex = ex.replace(
    'CIRCUIT_BREAKER_RESET should be removed from Railway after reset',
    'Remove CIRCUIT_BREAKER_RESET from .env after bot resumes'
)

# 9. Simplify get_mode_label
ex = re.sub(
    r"    return f\"\{'🟢 LIVE' if TRADING_MODE == 'live' else '🟡 PAPER'\}\"",
    '    return "🟢 LIVE"',
    ex
)

# 7. Remove paper branches
ex = re.sub(
    r"    if TRADING_MODE == \"paper\":\n"
    r"        log\.info\(\"\[PAPER\] get_futures_balance.*?\n"
    r"        return 0\.0\n\n",
    "",
    ex
)
ex = re.sub(
    r"    if TRADING_MODE == \"paper\":\n"
    r"        return None\n\n"
    r"    try:\n"
    r"        ex = _get_exchange\(\)\n"
    r"        ccxt_sym = _symbol_to_ccxt\(symbol\)\n"
    r"        positions",
    "    try:\n        ex = _get_exchange()\n        ccxt_sym = _symbol_to_ccxt(symbol)\n        positions",
    ex
)
ex = re.sub(
    r"    # \u2500+ Paper mode: log but don't execute \u2500+\n"
    r"    if TRADING_MODE == \"paper\":\n"
    r"        log\.info\(f\"\[PAPER\].*?\n"
    r"        result\.update\(\{.*?\n"
    r"            \"success\": True.*?\n"
    r"            \"sl_order_id\": \"PAPER\".*?\n"
    r"        \}\)\n"
    r"        return result\n\n",
    "",
    ex,
    flags=re.DOTALL
)
ex = re.sub(
    r"    if TRADING_MODE == \"paper\":\n"
    r"        log\.info\(f\"\[PAPER\] \{reason\} close.*?\n"
    r"        result\.update\(\{\"success\": True.*?\}\)\n"
    r"        return result\n\n",
    "",
    ex
)
ex = re.sub(
    r"    if TRADING_MODE == \"paper\":\n"
    r"        log\.info\(f\"\[PAPER\] FULL CLOSE.*?\n"
    r"        result\.update\(\{\"success\": True.*?\}\)\n"
    r"        return result\n\n",
    "",
    ex
)
ex = re.sub(
    r"    if TRADING_MODE == \"paper\":\n"
    r"        log\.info\(f\"\[PAPER\] cancel_open_orders.*?\n"
    r"        return True\n\n",
    "",
    ex
)
ex = re.sub(
    r"    if TRADING_MODE == \"paper\":\n"
    r"        log\.info\(f\"\[PAPER\] move SL.*?\n"
    r"        result\.update\(\{\"success\": True.*?\}\)\n"
    r"        return result\n\n",
    "",
    ex
)

if ex != orig_ex:
    ex_path.write_text(ex, encoding="utf-8")
    print("[OK] executor.py patched")
else:
    print("[SKIP] executor.py — no changes (already patched?)")

# ── Verify ───────────────────────────────────────────────────────────────────
print("\n── Verification ──")
b = bot_path.read_text(encoding="utf-8")
e = ex_path.read_text(encoding="utf-8")

checks = [
    ("BOT_DATA_DIR"                           in b,  "bot: BOT_DATA_DIR in STATE_FILE"),
    ("reset_daily(S[\"capital\"])\n\n    log" not in b, "bot: premature reset_daily removed"),
    ("reset_daily(bal)"                        in b,  "bot: reset_daily after balance sync"),
    ("Railway env vars"                       not in b, "bot: CB message fixed"),
    ("[PAPER]"                                not in e, "executor: no paper branches"),
    ("TRADING_MODE = os.getenv"              not in e, "executor: TRADING_MODE var removed"),
    ("Railway public IPv4:"                  not in e, "executor: Railway log fixed"),
]
all_ok = True
for ok,label in checks:
    if not ok: all_ok = False
    print(f"  {'[OK]' if ok else '[!!]'} {label}")

print(f"\n{'[DONE] All good.' if all_ok else '[WARN] Check !! items'}")
print("\nNow run:")
print("  nssm stop championbot")
print("  nssm start championbot")
