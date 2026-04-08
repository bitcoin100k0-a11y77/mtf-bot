import subprocess, time, pathlib

print("Stopping ChampionBot...")
subprocess.run(["sc.exe", "stop", "ChampionBot"], capture_output=True)
time.sleep(4)
print("Starting ChampionBot...")
r = subprocess.run(["sc.exe", "start", "ChampionBot"], capture_output=True, text=True)
print(r.stdout.strip() or r.stderr.strip())
time.sleep(3)
r2 = subprocess.run(["sc.exe", "query", "ChampionBot"], capture_output=True, text=True)
print(r2.stdout.strip())
print()
# Show last 30 lines of log
log = pathlib.Path(r"C:\botlogs\champion.log")
if log.exists():
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-30:]))
