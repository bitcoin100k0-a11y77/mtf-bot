# fixperm.py
# Run this ONCE from an admin PowerShell on the VPS to grant
# write access to C:\botdata and C:\champion for all users.
# After running, the bot can save bot_state.json from any PowerShell.

import subprocess
import sys


def fix_permissions(path):
    """Grant full control to the Users group on the given directory."""
    print(f"\n=== Fixing permissions on {path} ===")

    # Grant BUILTIN\Users full control, inherited to all files/subdirs
    result = subprocess.run(
        ["icacls", path, "/grant", "Users:(OI)(CI)F", "/T"],
        capture_output=True,
        text=True,
    )
    print("Return code:", result.returncode)
    if result.stdout.strip():
        print(result.stdout)
    if result.stderr.strip():
        print("STDERR:", result.stderr)
    return result.returncode == 0


paths = [r"C:\botdata", r"C:\champion"]
all_ok = True
for p in paths:
    ok = fix_permissions(p)
    status = "OK" if ok else "FAILED"
    print(f"  --> {p}: {status}")
    if not ok:
        all_ok = False

print("\n" + ("All permissions fixed." if all_ok else "Some failures — check output above."))
print("You can now run 'python launch.py' from a non-admin PowerShell.")
