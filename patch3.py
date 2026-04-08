"""
patch3.py — Fix IndentationError in bot.py introduced by hotfix.py.
Hotfix removed the `if executor.TRADING_MODE == "live":` wrapper but left
the `except` at 8-space indent while `try:` is now at 4-space indent.
Run from C:\\champion, then delete.
"""
import pathlib, re, sys

bot_path = pathlib.Path(__file__).parent / "bot.py"
bot = bot_path.read_text(encoding="utf-8")
orig = bot

old_block = (
    "        except Exception as e:\n"
    "            log.error(f\"Exchange init failed: {e}\")\n"
    "            tg(f\"\\u274c Exchange init failed: {e}\\nBot will retry on first trade.\")"
)
new_block = (
    "    except Exception as e:\n"
    "        log.error(f\"Exchange init failed: {e}\")\n"
    "        tg(f\"\\u274c Exchange init failed: {e}\\nBot will retry on first trade.\")"
)

if old_block in bot:
    bot = bot.replace(old_block, new_block, 1)
    print("[OK] Indentation fixed: except moved from 8-space to 4-space")
else:
    old_block2 = (
        "        except Exception as e:\n"
        "            log.error(f\"Exchange init failed: {e}\")\n"
        "            tg(f\"\u274c Exchange init failed: {e}\nBot will retry on first trade.\")"
    )
    new_block2 = (
        "    except Exception as e:\n"
        "        log.error(f\"Exchange init failed: {e}\")\n"
        "        tg(f\"\u274c Exchange init failed: {e}\nBot will retry on first trade.\")"
    )
    if old_block2 in bot:
        bot = bot.replace(old_block2, new_block2, 1)
        print("[OK] Indentation fixed (variant 2)")
    else:
        bot_new, n = re.subn(r'^        (except Exception as e:)$', r'    \1', bot, flags=re.MULTILINE)
        if n:
            bot = bot_new
            bot = re.sub(r'^            (log\.error\(f"Exchange init failed)', r'        \1', bot, flags=re.MULTILINE)
            bot = re.sub(r'^            (tg\(f".*?Bot will retry)', r'        \1', bot, flags=re.MULTILINE)
            print(f"[OK] Indentation fixed via regex ({n} match)")
        else:
            print("[!!] Pattern not found — showing lines 729-740:")
            lines = bot.splitlines()
            for i, line in enumerate(lines[728:740], start=729):
                print(f"  {i}: {repr(line)}")
            sys.exit(1)

if bot != orig:
    bot_path.write_text(bot, encoding="utf-8")
    print("[OK] bot.py saved")

import subprocess
result = subprocess.run(["python", "-m", "py_compile", str(bot_path)], capture_output=True, text=True)
if result.returncode == 0:
    print("[OK] bot.py syntax valid")
else:
    print(f"[!!] Syntax error remains: {result.stderr.strip()}")
    sys.exit(1)
print("\n[DONE] Run: sc.exe start ChampionBot")
