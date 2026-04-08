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
print(f"  Key length     : {len(key)}  (expected: 64)")
print(f"  Secret length  : {len(secret)}  (expected: 64)")
print(f"  Key prefix     : {key[:6]}...  (should be alphanumeric)")
print(f"  Has whitespace : {any(c in key for c in [' ', '\t', '\n', '\r'])}")
print(f"  Has quotes     : {any(c in key for c in [chr(34), chr(39)])}")
print(f"  All ASCII      : {all(ord(c) < 128 for c in key)}")

if len(key) != 64:
    print(f"  [!] KEY LENGTH WRONG -- got {len(key)}, expected 64")
    print("      Check .env: key may be truncated or has extra chars")
elif not re.match(r'^[A-Za-z0-9]+$', key):
    print("  [!] KEY CONTAINS NON-ALPHANUMERIC CHARS -- check for copy errors")
else:
    print("  [OK] Key format looks correct")

if len(secret) != 64:
    print(f"  [!] SECRET LENGTH WRONG -- got {len(secret)}, expected 64")
elif not re.match(r'^[A-Za-z0-9]+$', secret):
    print("  [!] SECRET CONTAINS NON-ALPHANUMERIC CHARS")
else:
    print("  [OK] Secret format looks correct")

# 2. Get current VPS IP (multiple methods)
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
            print(f"  IP (from {url.split('/')[2]}): {ip}")
            break
    except Exception as e:
        print(f"  {url.split('/')[2]}: failed ({e})")

if ip:
    print()
    print(f"  [ACTION REQUIRED] Whitelist this IP on Binance:")
    print(f"  {ip}")
    print()
    print("  Steps:")
    print("  1. Binance.com -> Profile -> API Management")
    print("  2. Click Edit on your API key")
    print("  3. Under 'Restrict access to trusted IPs only'")
    print("  4. Add:", ip)
    print("  5. Confirm via email, wait 5 min, restart bot")

# 3. Quick connectivity test (unauthenticated)
print()
print("--- Binance Futures Connectivity ---")
try:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(
        "https://fapi.binance.com/fapi/v1/ping", timeout=5, context=ctx
    ) as r:
        print(f"  [OK] fapi.binance.com reachable (status {r.status})")
except Exception as e:
    print(f"  [FAIL] Cannot reach fapi.binance.com: {e}")

# 4. Root cause summary
print()
print("=" * 60)
print("  ROOT CAUSE SUMMARY")
print("=" * 60)
print()
print("  Error -2008 means Binance rejected the API key.")
print("  Most common causes (in order):")
print()
print("  1. IP NOT WHITELISTED (most likely)")
print("     -> Binance returns -2008 for IP-restricted keys")
print("        when the request comes from an unlisted IP.")
print("     -> Fix: add the IP shown above to your API key.")
print()
print("  2. WRONG API KEY (less likely if format checks passed)")
print("     -> Key was copied incorrectly or is for Spot, not Futures.")
print("     -> Fix: regenerate API key on Binance Futures.")
print()
print("  3. KEY DISABLED/DELETED")
print("     -> Check Binance API Management page.")
print()
print("  In PAPER mode: -2008 only blocks balance queries.")
print("  The bot continues running -- no real orders affected.")
