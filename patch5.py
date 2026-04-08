"""
patch5.py - Fix launch.py f-string quoting error on the Mode: display line.

The Mode: display line was left with a single-quoted f-string containing
single-quoted arguments, causing a SyntaxError. This script replaces it
with a correctly double-quoted f-string.
"""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).parent

print("=" * 60)
print("patch5.py -- Fix launch.py f-string syntax error")
print("=" * 60)

p = ROOT / "launch.py"
if not p.exists():
    print("[SKIP] launch.py not found")
    raise SystemExit(0)

lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
new_lines = []
fixed = False

dq = chr(34)  # double-quote -- avoids quoting conflict in this source file
sq = chr(39)  # single-quote -- used inside the f-string expression

for line in lines:
    if "Mode: LIVE  Leverage" in line:
        indent = line[: len(line) - len(line.lstrip())]
        # Produces: log.info(f"  Mode: LIVE  Leverage: {os.environ.get('FUTURES_LEVERAGE','1')}x")
        good = (
            indent
            + "log.info(f"
            + dq
            + "  Mode: LIVE  Leverage: {os.environ.get("
            + sq + "FUTURES_LEVERAGE" + sq
            + ","
            + sq + "1" + sq
            + ")}x"
            + dq
            + ")"
            + "\n"
        )
        new_lines.append(good)
        print("[OK] Fixed Mode display line -> " + good.strip())
        fixed = True
    else:
        new_lines.append(line)

if not fixed:
    print("[SKIP] Mode display line not found (already patched?)")
else:
    p.write_text("".join(new_lines), encoding="utf-8")

# Syntax check
r = subprocess.run(
    ["python", "-m", "py_compile", str(p)],
    capture_output=True,
    text=True,
)
if r.returncode == 0:
    print("[OK] launch.py syntax valid")
else:
    print("[FAIL] launch.py syntax error: " + r.stderr.strip())
    raise SystemExit(1)

print()
print("[DONE] Restart service:")
print("  sc.exe stop ChampionBot; Start-Sleep 3; sc.exe start ChampionBot")
