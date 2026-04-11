# reset_cb.py — Emergency circuit breaker reset
"""
Directly resets the circuit breaker state in bot_state.json.
Run this when the bot is halted and you need to resume immediately.

Usage (from C:\\champion\\):
    python reset_cb.py

Effect:
  - Sets circuit_breaker.tripped = false
  - Sets circuit_breaker.consecutive_losses = 0
  - Clears circuit_breaker.trip_reason
  - Does NOT affect open_trades, capital, or trade history
  - Does NOT require CIRCUIT_BREAKER_RESET env var

After running:
    nssm restart ChampionBot

🔴 RISK: Only run this if you have reviewed why the CB tripped
and are satisfied that trading conditions are safe to resume.
If CB tripped due to daily drawdown (5%), ensure the drawdown
was not caused by a strategy failure before resuming.
"""

import json
import os
import sys


# State file locations (try both)
STATE_PATHS = [
    r"C:\champion\bot_state.json",
    r"C:\botdata\bot_state.json",
]


def reset_cb() -> None:
    """Reset circuit breaker state in bot_state.json."""
    state_path = None
    for p in STATE_PATHS:
        if os.path.exists(p):
            state_path = p
            break

    if not state_path:
        print(f"\u274c Could not find bot_state.json in any of:")
        for p in STATE_PATHS:
            print(f"   {p}")
        sys.exit(1)

    print(f"\u2139\ufe0f  Reading state from: {state_path}")

    with open(state_path, "r", encoding="utf-8") as f:
        S = json.load(f)

    cb = S.get("circuit_breaker", {})

    print("\nCurrent CB state:")
    print(f"  tripped            : {cb.get('tripped', False)}")
    print(f"  consecutive_losses : {cb.get('consecutive_losses', 0)}")
    print(f"  trip_reason        : {cb.get('trip_reason', '(none)')}")
    print(f"  daily_start_capital: ${cb.get('daily_start_capital', 0):.2f}")
    print(f"  daily_start_date   : {cb.get('daily_start_date', '(none)')}")
    print(f"\nCurrent capital in state: ${S.get('capital', 0):.2f}")
    print(f"Open trades: {list(S.get('open_trades', {}).keys()) or 'none'}")

    if not cb.get("tripped", False):
        print("\n\u2705 CB is already NOT tripped — no reset needed.")
        print("   If bot is still halted, check logs for another cause.")
        return

    # Confirm with user
    print("\n\u26a0\ufe0f  About to reset circuit breaker.")
    answer = input("Type YES to confirm reset: ").strip()
    if answer != "YES":
        print("Aborted.")
        sys.exit(0)

    # Apply reset
    cb["tripped"] = False
    cb["consecutive_losses"] = 0
    cb["trip_reason"] = ""
    # Leave daily_start_capital intact — it will be force-synced from Binance on restart

    S["circuit_breaker"] = cb

    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(S, f, indent=2)

    print(f"\n\u2705 CB reset written to: {state_path}")
    print("\nNext step:")
    print("    nssm restart ChampionBot")
    print("\nThe new bot.py will also auto-clear any future consecutive-loss trips")
    print("on startup (that trigger has been permanently removed from executor.py).")


if __name__ == "__main__":
    reset_cb()
