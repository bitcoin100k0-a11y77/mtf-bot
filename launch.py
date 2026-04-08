# launch.py -- Windows VPS entry point for Champion v4.1
import os, sys, logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("Launcher")

REQUIRED_VARS = ["BINANCE_API_KEY", "BINANCE_API_SECRET", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"]


def load_env_file(env_path):
    if not env_path.is_file():
        log.error(f"[X] .env not found at: {env_path}"); sys.exit(1)
    loaded = 0
    for lineno, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"): continue
        if "=" not in line: continue
        key, _, value = line.partition("=")
        key = key.strip(); value = value.strip()
        if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value; loaded += 1
    log.info(f"[OK] Loaded {loaded} vars from {env_path.name}")
    return loaded


def validate_config():
    missing = [k for k in REQUIRED_VARS if not os.environ.get(k, "").strip()]
    if missing: log.error(f"[X] Missing: {missing}"); return False
    state_dir = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/data"))
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tf = state_dir / ".write_test"; tf.write_text("ok"); tf.unlink()
        log.info(f"[OK] State dir writable: {state_dir}")
    except Exception as e:
        log.error(f"[X] Cannot write state dir: {e}"); return False
    mode = os.environ.get("TRADING_MODE", "live").lower()
    if mode == "live": log.warning("[!] TRADING_MODE=live -- real orders WILL be placed.")
    else: log.info(f"[OK] TRADING_MODE={mode}")
    return True


def bridge_telegram_tokens():
    t = os.environ.get("TELEGRAM_TOKEN", "")
    b = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if t and not b:
        os.environ["TELEGRAM_BOT_TOKEN"] = t
        log.info("[OK] TELEGRAM_BOT_TOKEN bridged from TELEGRAM_TOKEN")


def main():
    env_file = Path(__file__).parent / ".env"
    log.info("=" * 60); log.info("  Champion v4.1 -- Windows VPS Launcher"); log.info("=" * 60)
    load_env_file(env_file)
    bridge_telegram_tokens()
    if not validate_config():
        log.error("Aborting -- fix .env and restart."); sys.exit(1)
    log.info(f"  Mode: {os.environ.get('TRADING_MODE','live').upper()}  Leverage: {os.environ.get('FUTURES_LEVERAGE','1')}x")
    log.info("  Starting bot.py ..."); log.info("=" * 60)
    try:
        import bot; bot.main()
    except KeyboardInterrupt:
        log.info("Stopped by user."); sys.exit(0)
    except SystemExit: raise
    except Exception as exc:
        log.exception(f"[CRASH] {exc}"); sys.exit(1)


if __name__ == "__main__":
    main()
