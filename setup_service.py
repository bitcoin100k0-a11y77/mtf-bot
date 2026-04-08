# setup_service.py -- Downloads NSSM and registers ChampionBot as a Windows service
import subprocess
import sys
import os
import shutil

PYTHON_EXE = r"C:\Program Files\Python311\python.exe"
APP_DIR = r"C:\champion"
LOG_FILE = r"C:\botlogs\champion.log"
SERVICE_NAME = "ChampionBot"


def run(cmd, check=False):
    print(f">>> {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    if check and result.returncode != 0:
        print(f"ERROR: command returned {result.returncode}")
        sys.exit(1)
    return result.returncode


def find_nssm():
    found = shutil.which("nssm")
    if found:
        return found
    candidates = [
        r"C:\ProgramData\chocolatey\bin\nssm.exe",
        r"C:\Program Files\NSSM\nssm.exe",
        r"C:\Program Files (x86)\NSSM\nssm.exe",
        r"C:\tools\nssm\win64\nssm.exe",
        r"C:\nssm-2.24\win64\nssm.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def download_winsw():
    import urllib.request
    url = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"
    dest = r"C:\nssm_tool\nssm.exe"
    os.makedirs(r"C:\nssm_tool", exist_ok=True)
    print(f"Downloading WinSW from GitHub to {dest} ...")
    urllib.request.urlretrieve(url, dest)
    print("Download complete.")
    return dest


def install_nssm():
    print("--- Trying winget ---")
    r = run("winget install --id NSSM.NSSM -e --accept-source-agreements --accept-package-agreements")
    if r == 0:
        nssm = find_nssm()
        if nssm:
            return nssm
    print("--- Trying choco ---")
    r = run("choco install nssm -y")
    if r == 0:
        nssm = find_nssm()
        if nssm:
            return nssm
    print("--- Falling back to WinSW download ---")
    return download_winsw()


def register_service(nssm):
    print(f"\n=== Registering service with: {nssm} ===\n")
    commands = [
        f'"{nssm}" install {SERVICE_NAME} "{PYTHON_EXE}"',
        f'"{nssm}" set {SERVICE_NAME} AppParameters launch.py',
        f'"{nssm}" set {SERVICE_NAME} AppDirectory "{APP_DIR}"',
        f'"{nssm}" set {SERVICE_NAME} AppStdout "{LOG_FILE}"',
        f'"{nssm}" set {SERVICE_NAME} AppStderr "{LOG_FILE}"',
        f'"{nssm}" set {SERVICE_NAME} AppRotateFiles 1',
        f'"{nssm}" set {SERVICE_NAME} AppRotateBytes 10485760',
        f'"{nssm}" set {SERVICE_NAME} Start SERVICE_AUTO_START',
    ]
    for cmd in commands:
        run(cmd)
    print("\n=== Starting service ===")
    run(f'"{nssm}" start {SERVICE_NAME}')
    print("\n=== Service status ===")
    run(f"sc query {SERVICE_NAME}")


def main():
    print("=" * 60)
    print("  ChampionBot -- Windows Service Setup")
    print("=" * 60)
    if not os.path.exists(PYTHON_EXE):
        print(f"ERROR: Python not found at {PYTHON_EXE}")
        sys.exit(1)
    print(f"[OK] Python: {PYTHON_EXE}")
    result = subprocess.run(f"sc query {SERVICE_NAME}", shell=True, capture_output=True, text=True)
    if "RUNNING" in result.stdout or "STOPPED" in result.stdout:
        print(f"[!] Service exists -- removing first...")
        run(f"sc stop {SERVICE_NAME}")
        run(f"sc delete {SERVICE_NAME}")
        import time; time.sleep(2)
    nssm = find_nssm()
    if nssm:
        print(f"[OK] NSSM found: {nssm}")
    else:
        print("[...] Installing NSSM...")
        nssm = install_nssm()
    if not nssm or not os.path.exists(nssm):
        print("ERROR: Could not obtain nssm.exe")
        sys.exit(1)
    print(f"[OK] Using: {nssm}")
    register_service(nssm)
    print("\n[DONE] Service setup complete.")
    print(f"  Logs:    C:\\botlogs\\champion.log")
    print(f"  Stop:    sc stop {SERVICE_NAME}")
    print(f"  Start:   sc start {SERVICE_NAME}")


if __name__ == "__main__":
    main()
