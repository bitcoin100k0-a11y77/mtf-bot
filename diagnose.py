# diagnose.py -- Diagnose Binance -2008 Invalid Api-Key ID error
import os, sys, re, urllib.request, ssl

ENV_FILE = r"C:\champion\.env"

def load_env():
    env = {}
    for line in open(ENV_FILE, encoding="utf-8").readlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        env[k.strip()] = v
    return env

env = load_env()
key = env.get("BINANCE_API_KEY", "")
secret = env.get("BINANCE_API_SECRET", "")

print("=" * 60)
print("  Binance API Key Diagnostics")
print("=" * 60)

# 1. Key format checks
print()
print("--- API Key Format ---")
print("  Key length    :", len(key), " (expected: 64)")
print("  Secret length :", len(secret), " (expected: 64)")
print("  Key prefix    :", key[:6] + "...  (should be alphanumeric)")
has_ws = any(c in key for c in (" ", "\t", "\n", "\r"))
has_q  = any(c in key for c in ('"', "'"))
all_ascii = all(ord(c) < 128 for c in key)
print("  Has whitespace:", has_ws)
print("  Has quotes    :", has_q)
print("  All ASCII     :", all_ascii)

if len(key) != 64:
    print("  [!] KEY LENGTH WRONG -- got", len(key), "expected 64")
    print("      Check .env -- key may be truncated or has extra chars")
elif not re.match(r"^[A-Za-z0-9]+$", key):
    print("  [!] KEY HAS NON-ALPHANUMERIC CHARS -- check for copy errors")
else:
    print("  [OK] Key format looks correct")

if len(secret) != 64:
    print("  [!] SECRET LENGTH WRONG -- got", len(secret), "expected 64")
elif not re.match(r"^[A-Za-z0-9]+$", secret):
    print("  [!] SECRET HAS NON-ALPHANUMERIC CHARS")
else:
    print("  [OK] Secret format looks correct")

# 2. Get current VPS IP
print()
print("--- VPS Public IP ---")
ip = None
for url in [
    "https://api4.ipify.org",
    "https://ifconfig.me/ip",
    "https://ipecho.net/plain",
]:
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(url, timeout=5, context=ctx) as r:
            ip = r.read().decode().strip()
            host = url.split("/")[2]
            print("  IP (from " + host + "):", ip)
            break
    except Exception as e:
        host = url.split("/")[2]
        print("  " + host + ": failed --", str(e)[:60])

if ip:
    print()
    print("  >>> WHITELIST THIS IP ON BINANCE: " + ip)
    print()
    print("  How:")
    print("  1. Binance.com -> Profile (top right) -> API Management")
    print("  2. Click Edit on your API key")
    print("  3. Restrict access to trusted IPs -> Add IP:", ip)
    print("  4. Save -> confirm via email -> wait 5 min")
    print("  5. Restart bot: sc stop ChampionBot && sc start ChampionBot")

# 3. Connectivity test
print()
print("--- Binance Futures Connectivity ---")
try:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(
        "https://fapi.binance.com/fapi/v1/ping", timeout=5, context=ctx
    ) as r:
        print("  [OK] fapi.binance.com reachable")
except Exception as e:
    print("  [FAIL] Cannot reach fapi.binance.com:", str(e)[:80])

print()
print("=" * 60)
print("  SUMMARY: error -2008 = IP not whitelisted (most likely)")
print("  Add the IP above to your Binance API key, then restart.")
print("  In PAPER mode this only blocks balance queries -- safe.")
print("=" * 60)
