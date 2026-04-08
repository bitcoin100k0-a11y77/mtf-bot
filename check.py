import os, subprocess

LOG = r"C:\botlogs\champion.log"
STATE = r"C:\botdata\bot_state.json"

print("=" * 60)
print("  ChampionBot Health Check")
print("=" * 60)

# Service status
r = subprocess.run("sc query ChampionBot", shell=True, capture_output=True, text=True)
state_line = [l for l in r.stdout.splitlines() if "STATE" in l]
print("Service:", state_line[0].strip() if state_line else "NOT FOUND")

# TRADING_MODE in .env
env_file = r"C:\champion\.env"
if os.path.exists(env_file):
    for line in open(env_file).readlines():
        if "TRADING_MODE" in line:
            print("Mode:   ", line.strip())
            break

# Log tail
print()
print("--- Last 30 log lines ---")
if os.path.exists(LOG):
    lines = open(LOG, encoding="utf-8", errors="replace").readlines()
    for l in lines[-30:]:
        print(l, end="")
    if any("ERROR" in l or "CRITICAL" in l for l in lines[-50:]):
        print()
        print("[!] ERRORS FOUND in log -- scroll up to review")
    else:
        print()
        print("[OK] No ERROR/CRITICAL in last 50 lines")
else:
    print(f"Log not found: {LOG}")
    print("Bot may still be starting up -- wait 30s and retry")

# State file
print()
if os.path.exists(STATE):
    import json
    try:
        s = json.load(open(STATE))
        print("State file:", STATE)
        print("  Capital :", s.get("available_capital", "?"))
        print("  Open trades:", len(s.get("open_trades", {})))
        print("  CB tripped :", s.get("circuit_breaker_tripped", False))
    except Exception as e:
        print(f"State file exists but parse error: {e}")
else:
    print(f"State file not yet created: {STATE}")
    print("  (Normal on first startup -- created after first poll cycle)")
