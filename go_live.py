"""
go_live.py — One-shot live mode switch for Champion v4.1
Patches bot.py (RISK_PCT 0.75% -> 1.0%) and updates .env
(TRADING_MODE=live, FUTURES_LEVERAGE=5)
Run once from C:\\champion, then delete this file.
"""
import pathlib, re, sys

BASE = pathlib.Path(__file__).parent

# 1. Patch bot.py
bot_file = BASE / "bot.py"
if not bot_file.exists():
    print("ERROR: bot.py not found"); sys.exit(1)

original = bot_file.read_text(encoding="utf-8")

if "0.01" in original and "1.0% risk" in original:
    print("[OK] bot.py already patched to 1.0% risk -- skipping")
else:
    patched = re.sub(
        r"RISK_PCT\s*=\s*0\.0075\s*#.*",
        "RISK_PCT    = 0.01        # RISK: 1.0% risk per trade",
        original
    )
    if "RISK_PCT    = 0.01" not in patched:
        print("ERROR: Pattern not found in bot.py -- check manually"); sys.exit(1)
    bot_file.write_text(patched, encoding="utf-8")
    print("[OK] bot.py patched: RISK_PCT 0.75% -> 1.0%")

# 2. Patch .env
env_file = BASE / ".env"
if not env_file.exists():
    print("ERROR: .env not found"); sys.exit(1)

env_text = env_file.read_text(encoding="utf-8")
env_text = re.sub(r"TRADING_MODE\s*=\s*\w+", "TRADING_MODE=live", env_text)
env_text = re.sub(r"FUTURES_LEVERAGE\s*=\s*\d+", "FUTURES_LEVERAGE=5", env_text)
env_file.write_text(env_text, encoding="utf-8")
print("[OK] .env updated: TRADING_MODE=live, FUTURES_LEVERAGE=5")

# 3. Verify
print("\n-- Verification --")
for line in env_file.read_text(encoding="utf-8").splitlines():
    if any(k in line for k in ("TRADING_MODE", "FUTURES_LEVERAGE")):
        print(f"  {line}")
for line in bot_file.read_text(encoding="utf-8").splitlines():
    if "RISK_PCT" in line and "=" in line and "0.01" in line:
        print(f"  bot.py -> {line.strip()}")

print("\n[DONE] Now run: nssm stop championbot && nssm start championbot")
