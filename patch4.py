"""
patch4.py - Final cleanup: remove all TRADING_MODE / paper / Railway remnants.

Changes:
  launch.py  - remove TRADING_MODE read + if/else block (lines 39-41)
             - simplify mode display line to always say LIVE
  executor.py - update 2 stale comments
  bot.py      - update 4 stale TRADING_MODE comments
  .env        - remove TRADING_MODE=live line
"""
import pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).parent


def patch_file(path, replacements, label):
    text = path.read_text(encoding="utf-8")
    orig = text
    for old, new in replacements:
        if old in text:
            text = text.replace(old, new, 1)
            print(f"  [OK]   {label}: patched '{old[:50].strip()}'")
        else:
            print(f"  [SKIP] {label}: not found '{old[:50].strip()}'")
    if text != orig:
        path.write_text(text, encoding="utf-8")
    return text != orig


print("=" * 60)
print("patch4.py -- TRADING_MODE / paper / Railway cleanup")
print("=" * 60)

# --- 1. launch.py ---
launch = ROOT / "launch.py"
if launch.exists():
    lines = launch.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines = []
    skip_next = 0
    changed = False
    for line in lines:
        if skip_next > 0:
            skip_next -= 1
            continue
        # Remove the 3-line TRADING_MODE read + if/else block;
        # replace with a single always-live warning line
        if 'mode = os.environ.get("TRADING_MODE"' in line:
            # Preserve the same indentation
            indent = line[: len(line) - len(line.lstrip())]
            new_lines.append(indent + 'log.warning("[!] LIVE MODE -- real orders WILL be placed.")' + "\n")
            skip_next = 2  # drop next 2 lines (if/else)
            changed = True
            print("  [OK]   launch.py: removed TRADING_MODE mode= block")
            continue
        # Simplify the Mode: display line
        if "os.environ.get('TRADING_MODE'" in line and "Mode:" in line:
            indent = line[: len(line) - len(line.lstrip())]
            lev_expr = "os.environ.get('FUTURES_LEVERAGE','1')"
            new_lines.append(indent + "log.info(f'  Mode: LIVE  Leverage: {" + lev_expr + "}x')" + "\n")
            changed = True
            print("  [OK]   launch.py: simplified Mode: display line")
            continue
        new_lines.append(line)
    if changed:
        launch.write_text("".join(new_lines), encoding="utf-8")
    else:
        print("  [SKIP] launch.py: patterns not found (already patched?)")
else:
    print("  [SKIP] launch.py not found")

# --- 2. executor.py ---
executor = ROOT / "executor.py"
if executor.exists():
    patch_file(executor, [
        (
            "# Send IP to Telegram so it's visible even if Railway redacts logs",
            "# Send IP to Telegram for monitoring",
        ),
    ], "executor.py")
else:
    print("  [SKIP] executor.py not found")

# --- 3. bot.py ---
bot = ROOT / "bot.py"
if bot.exists():
    patch_file(bot, [
        (
            'TRADING_MODE env var: "live" or "paper"',
            'All orders are LIVE -- paper mode removed',
        ),
        (
            "TRADING_MODE (default: live), FUTURES_LEVERAGE",
            "FUTURES_LEVERAGE",
        ),
        (
            "When TRADING_MODE=live, this bot places REAL orders with REAL money",
            "This bot places REAL orders with REAL money -- live-only",
        ),
        (
            "This places real orders when TRADING_MODE=live",
            "This places REAL orders -- live-only, no paper mode",
        ),
    ], "bot.py")
else:
    print("  [SKIP] bot.py not found")

# --- 4. .env ---
env_file = ROOT / ".env"
if env_file.exists():
    lines = env_file.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines = [l for l in lines if not l.strip().upper().startswith("TRADING_MODE")]
    removed = len(lines) - len(new_lines)
    if removed:
        env_file.write_text("".join(new_lines), encoding="utf-8")
        print(f"  [OK]   .env: removed {removed} TRADING_MODE line(s)")
    else:
        print("  [SKIP] .env: TRADING_MODE line not found")
else:
    print("  [SKIP] .env not found")

# --- 5. Syntax checks ---
print()
errors = 0
for f in [launch, bot, executor]:
    if not f or not f.exists():
        continue
    r = subprocess.run(
        ["python", "-m", "py_compile", str(f)],
        capture_output=True, text=True
    )
    if r.returncode == 0:
        print(f"  [OK]   {f.name} syntax valid")
    else:
        print(f"  [FAIL] {f.name} syntax error: {r.stderr.strip()}")
        errors += 1

print()
if errors:
    print(f"[ERROR] {errors} syntax error(s) -- do NOT restart service")
    sys.exit(1)
else:
    print("[DONE] All patches applied and syntax valid.")
    print()
    print("Now restart service:")
    print("  sc.exe stop ChampionBot; Start-Sleep 3; sc.exe start ChampionBot")
