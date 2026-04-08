"""
patch2.py — Fix 2 remaining bot.py items that hotfix.py missed.
Run from C:\\champion once, then delete.
"""
import pathlib, re

bot_path = pathlib.Path(__file__).parent / "bot.py"
bot = bot_path.read_text(encoding="utf-8")
orig = bot

# ── Fix 1: add reset_daily(bal) after balance sync ──────────────────────────
# Pattern without "(was ...)" — that suffix wasn't added by fix_bugs.py on VPS
old1 = '                log.info(f"Capital synced from Binance: ${bal:.2f}")\n'
new1 = (
    '                log.info(f"Capital synced from Binance: ${bal:.2f}")\n'
    '                # \U0001f534 RISK: Reset CB baseline with real capital, not stale IC\n'
    '                executor.circuit_breaker.reset_daily(bal)\n'
)
if old1 in bot:
    bot = bot.replace(old1, new1, 1)
    print("[OK]   Fix 1: reset_daily(bal) inserted after balance sync")
elif 'reset_daily(bal)' in bot:
    print("[SKIP] Fix 1: reset_daily(bal) already present")
else:
    # Try variant with "(was ...)" in case VPS has that
    old1b = '                log.info(f"Capital synced from Binance: ${bal:.2f} (was ${Cfg.IC:.2f})")\n'
    new1b = (
        '                log.info(f"Capital synced from Binance: ${bal:.2f}")\n'
        '                # \U0001f534 RISK: Reset CB baseline with real capital, not stale IC\n'
        '                executor.circuit_breaker.reset_daily(bal)\n'
    )
    if old1b in bot:
        bot = bot.replace(old1b, new1b, 1)
        print("[OK]   Fix 1b: reset_daily(bal) inserted (was-variant matched)")
    else:
        print("[!!]   Fix 1: pattern not found — check bot.py manually")

# ── Fix 2: CB message "Railway env vars" → ".env on the VPS" ────────────────
# The original spans two adjacent f-string lines — use regex with DOTALL
old2_pat = (
    r'(f"To resume: set CIRCUIT_BREAKER_RESET=true in )Railway env vars, "\s+'
    r'f"(then remove it after bot resumes\.)"'
)
new2_repl = (
    r'\1.env on the VPS, "\n'
    r'       f"\2"'
)
bot_new, n = re.subn(old2_pat, new2_repl, bot)
if n:
    bot = bot_new
    print("[OK]   Fix 2: CB message updated (.env on the VPS)")
elif 'Railway env vars' not in bot:
    print("[SKIP] Fix 2: CB message already fixed")
else:
    print("[!!]   Fix 2: CB regex didn't match — check bot.py manually")

# ── Write & verify ───────────────────────────────────────────────────────────
if bot != orig:
    bot_path.write_text(bot, encoding="utf-8")
    print("[OK] bot.py saved")
else:
    print("[INFO] No changes made")

b = bot_path.read_text(encoding="utf-8")
ok1 = "reset_daily(bal)" in b
ok2 = "Railway env vars" not in b
print(f"\n  reset_daily(bal) present : {'YES' if ok1 else 'MISSING'}")
print(f"  Railway env vars gone    : {'YES' if ok2 else 'STILL THERE (cosmetic only)'}")
print("\n[DONE] Restart service if not already running:")
print("  sc.exe start ChampionBot")
