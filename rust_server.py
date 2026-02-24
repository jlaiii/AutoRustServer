#!/usr/bin/env python3
"""
Rust Dedicated Server Auto-Setup & Manager  (Pterodactyl / Linux / Windows)
----------------------------------------------------------------------------
Uses DepotDownloader (64-bit, no 32-bit libs needed) instead of SteamCMD,
so it works out-of-the-box on Pterodactyl Python eggs.

Flow:
  1. Auto-installs pip packages (requests, psutil)
  2. Downloads DepotDownloader from GitHub
  3. Downloads / validates the Rust Dedicated Server (app 258550)
  4. Launches the server
  5. On crash / stop → checks for updates → relaunches
  6. Stops after too many consecutive fast crashes
"""

# ──────────────────────────────────────────────
# 0.  Auto-install pip dependencies
# ──────────────────────────────────────────────
import subprocess, sys, importlib, os
from pathlib import Path as _Path

_SCRIPT_DIR = _Path(__file__).resolve().parent
_PIP_TARGET = str(_SCRIPT_DIR / ".pip_packages")
if _PIP_TARGET not in sys.path:
    sys.path.insert(0, _PIP_TARGET)

REQUIRED_PACKAGES = ["psutil", "requests"]

def install_requirements():
    os.makedirs(_PIP_TARGET, exist_ok=True)
    for pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg)
        except ImportError:
            print(f"[SETUP] Installing missing package: {pkg}")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install",
                 "--target", _PIP_TARGET, pkg],
            )
            importlib.invalidate_caches()
            importlib.import_module(pkg)

install_requirements()

# ──────────────────────────────────────────────
# Imports (safe after auto-install)
# ──────────────────────────────────────────────
import io, time, signal, shutil, zipfile, json
import textwrap, datetime, threading, random, platform
import requests, psutil
from pathlib import Path

IS_LINUX = platform.system() != "Windows"

# ──────────────────────────────────────────────
# 1a. Logging – mirror all output to a text file
# ──────────────────────────────────────────────
LOG_FILE = Path(__file__).resolve().parent / "server_log.txt"


class _TeeWriter:
    """Write to both the original stream and a log file, adding timestamps."""

    def __init__(self, original_stream, log_handle):
        self._original = original_stream
        self._log = log_handle
        self._at_line_start = True

    # ── helpers ──────────────────────────────
    def _stamp(self) -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def write(self, text: str):
        if not text:
            return
        # Write to console unchanged
        self._original.write(text)
        # Write to log with timestamps at the start of each line
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if self._at_line_start and line:
                self._log.write(f"[{self._stamp()}] ")
            self._log.write(line)
            if i < len(lines) - 1:          # there was a newline
                self._log.write("\n")
                self._at_line_start = True
            else:
                self._at_line_start = (line == "")

    def flush(self):
        self._original.flush()
        self._log.flush()

    # Forward everything else to the original stream
    def __getattr__(self, name):
        return getattr(self._original, name)


def _setup_logging():
    """Open the log file and redirect stdout + stderr through _TeeWriter."""
    log_handle = open(LOG_FILE, "a", encoding="utf-8", buffering=1)  # line-buffered
    log_handle.write(f"\n{'=' * 60}\n")
    log_handle.write(f"  Log session started: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
    log_handle.write(f"{'=' * 60}\n")
    sys.stdout = _TeeWriter(sys.__stdout__, log_handle)
    sys.stderr = _TeeWriter(sys.__stderr__, log_handle)


_setup_logging()

# ──────────────────────────────────────────────
# 1.  Configuration
# ──────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent

# Paths
DEPOTDL_DIR = SCRIPT_DIR / "depotdownloader"
SERVER_DIR  = SCRIPT_DIR          # install server files next to this script

if IS_LINUX:
    DEPOTDL_EXE     = DEPOTDL_DIR / "DepotDownloader"
    DEPOTDL_ASSET   = "DepotDownloader-linux-x64.zip"
    RUST_SERVER_EXE = SERVER_DIR / "RustDedicated"
else:
    DEPOTDL_EXE     = DEPOTDL_DIR / "DepotDownloader.exe"
    DEPOTDL_ASSET   = "DepotDownloader-windows-x64.zip"
    RUST_SERVER_EXE = SERVER_DIR / "RustDedicated.exe"

DEPOTDL_REPO = "SteamRE/DepotDownloader"
RUST_APP_ID  = 258550

# Server launch settings
SERVER_IDENTITY    = "myserver"
SERVER_HOSTNAME    = "My Rust Server"
SERVER_PORT        = 3109
SERVER_MAP         = "Procedural Map"
SERVER_WORLDSIZE   = 3500
SERVER_SEED        = 0              # 0 = random seed each wipe
SERVER_MAXPLAYERS  = 100
SERVER_DESCRIPTION = "Auto-managed Rust server"
SERVER_URL         = ""
SERVER_BANNER      = ""
SERVER_IP          = "0.0.0.0"       # bind address (0.0.0.0 = all interfaces)
RCON_PASSWORD      = "changeme"       # RCON password – CHANGE THIS
RCON_WEB           = 1                # 1 = websocket RCON (required by most panels)

RESTART_DELAY      = 15
MAX_FAST_CRASHES   = 5
MAX_TOTAL_CRASHES  = 3               # stop after this many consecutive non-zero exits (any uptime)

# ──────────────────────────────────────────────
# 2.  DepotDownloader helpers
# ──────────────────────────────────────────────
def get_latest_depotdl_url() -> str:
    """Query GitHub API for the latest DepotDownloader release asset URL."""
    api = f"https://api.github.com/repos/{DEPOTDL_REPO}/releases/latest"
    print(f"[DEPOT] Fetching latest release info from GitHub …")
    r = requests.get(api, timeout=30)
    r.raise_for_status()
    data = r.json()
    for asset in data.get("assets", []):
        if asset["name"] == DEPOTDL_ASSET:
            print(f"[DEPOT] Latest release: {data['tag_name']}  ({asset['name']})")
            return asset["browser_download_url"]
    raise RuntimeError(f"Could not find {DEPOTDL_ASSET} in latest release")


def download_depotdownloader() -> bool:
    """Download and extract DepotDownloader if not present."""
    if DEPOTDL_EXE.exists():
        print("[DEPOT] DepotDownloader already installed.")
        return True

    DEPOTDL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        url = get_latest_depotdl_url()
        print(f"[DEPOT] Downloading {url} …")
        r = requests.get(url, timeout=300)
        r.raise_for_status()
    except Exception as e:
        print(f"[DEPOT] ERROR downloading DepotDownloader: {e}")
        return False

    buf = io.BytesIO(r.content)
    with zipfile.ZipFile(buf) as zf:
        zf.extractall(DEPOTDL_DIR)

    # Make executable on Linux
    if IS_LINUX:
        for f in DEPOTDL_DIR.iterdir():
            if f.is_file() and not f.suffix:
                f.chmod(0o755)

    if not DEPOTDL_EXE.exists():
        # Some releases nest files – search for the binary
        for f in DEPOTDL_DIR.rglob("DepotDownloader*"):
            if f.is_file() and (f.suffix == "" or f.suffix == ".exe"):
                shutil.move(str(f), str(DEPOTDL_EXE))
                DEPOTDL_EXE.chmod(0o755)
                break

    print(f"[DEPOT] Extracted to {DEPOTDL_DIR}")
    return DEPOTDL_EXE.exists()


def run_depotdownloader(app_id: int, install_dir: Path) -> bool:
    """Run DepotDownloader to install/update an app. Returns True on success."""
    cmd = [
        str(DEPOTDL_EXE),
        "-app", str(app_id),
        "-dir", str(install_dir),
    ]
    print(f"[UPDATE] Running: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            print(f"  {line}", end="")
        proc.wait()
    except Exception as e:
        print(f"[UPDATE] ERROR: {e}")
        return False

    if proc.returncode != 0:
        print(f"[UPDATE] FAILED – exit code {proc.returncode}")
        return False
    print("[UPDATE] Done.")
    return True

# ──────────────────────────────────────────────
# 3.  Install / Update Rust server
# ──────────────────────────────────────────────
def install_or_update_server() -> bool:
    """Download / update the Rust dedicated server via DepotDownloader."""
    if not download_depotdownloader():
        return False
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    print("[UPDATE] Checking for Rust server updates …")
    ok = run_depotdownloader(RUST_APP_ID, SERVER_DIR)
    if ok and IS_LINUX and RUST_SERVER_EXE.exists():
        RUST_SERVER_EXE.chmod(0o755)
    if ok:
        print("[UPDATE] Rust server is up to date.\n")
    else:
        print("[UPDATE] Update failed – see errors above.\n")
    return ok

# ──────────────────────────────────────────────
# 4.  Server configuration helpers
# ──────────────────────────────────────────────
def write_server_cfg():
    cfg_dir = SERVER_DIR / "server" / SERVER_IDENTITY / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "server.cfg"

    cfg_content = textwrap.dedent(f"""\
        # ── Auto-generated server.cfg ──
        server.hostname "{SERVER_HOSTNAME}"
        server.description "{SERVER_DESCRIPTION}"
        server.url "{SERVER_URL}"
        server.headerimage "{SERVER_BANNER}"
        server.maxplayers {SERVER_MAXPLAYERS}
        server.worldsize {SERVER_WORLDSIZE}
        server.saveinterval 300
        server.globalchat true
        server.stability true
    """)
    cfg_file.write_text(cfg_content, encoding="utf-8")
    print(f"[CONFIG] Wrote {cfg_file}")


def build_server_env() -> dict:
    """Build environment variables for the Rust server process."""
    env = os.environ.copy()
    if IS_LINUX:
        # Rust server needs LD_LIBRARY_PATH to find its native .so plugins
        extra_paths = [
            str(SERVER_DIR),
            str(SERVER_DIR / "RustDedicated_Data" / "Plugins"),
            str(SERVER_DIR / "RustDedicated_Data" / "Plugins" / "x86_64"),
        ]
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = ":".join(extra_paths + ([existing] if existing else []))
        print(f"[SERVER] LD_LIBRARY_PATH = {env['LD_LIBRARY_PATH']}")
    return env


def build_launch_args() -> list[str]:
    seed = SERVER_SEED if SERVER_SEED != 0 else random.randint(1, 2147483647)
    args = [
        str(RUST_SERVER_EXE),
        "-batchmode",
        "-nographics",               # ← required for headless / containerised servers
        "+server.ip", SERVER_IP,
        "+server.port", str(SERVER_PORT),
        "+server.queryport", str(SERVER_PORT),   # same as game port (single-port setup)
        "+server.level", SERVER_MAP,
        "+server.seed", str(seed),
        "+server.worldsize", str(SERVER_WORLDSIZE),
        "+server.maxplayers", str(SERVER_MAXPLAYERS),
        "+server.hostname", SERVER_HOSTNAME,
        "+server.description", SERVER_DESCRIPTION,
        "+server.identity", SERVER_IDENTITY,
        "+rcon.port", str(SERVER_PORT),           # reuse game port for RCON
        "+rcon.password", RCON_PASSWORD,
        "+rcon.web", str(RCON_WEB),
    ]
    # On Linux, log to /dev/stdout so output stays on the console / panel
    # On Windows, omit -logfile so Unity prints to stdout by default
    if IS_LINUX:
        args += ["-logfile", "/dev/stdout"]
    return args

# ──────────────────────────────────────────────
# 5.  Process management
# ──────────────────────────────────────────────
server_process = None
shutdown_requested = False


def signal_handler(sig, frame):
    global shutdown_requested
    print("\n[MANAGER] Shutdown requested – stopping server …")
    shutdown_requested = True
    if server_process and server_process.poll() is None:
        server_process.terminate()
        try:
            server_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server_process.kill()


signal.signal(signal.SIGINT, signal_handler)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal_handler)
else:
    signal.signal(signal.SIGTERM, signal_handler)


def start_server() -> subprocess.Popen:
    args = build_launch_args()
    env  = build_server_env()
    seed_display = args[args.index("+server.seed") + 1] if "+server.seed" in args else "?"
    print("[SERVER] Launching Rust server …")
    print(f"[SERVER]   Port : {SERVER_PORT}")
    print(f"[SERVER]   Map  : {SERVER_MAP} | Size {SERVER_WORLDSIZE} | Seed {seed_display}")
    sys.stdout.flush()
    # Let the server inherit stdout/stderr directly – avoids pipe deadlocks
    # and lets Pterodactyl / the terminal see output in real time.
    proc = subprocess.Popen(
        args,
        cwd=str(SERVER_DIR),
        env=env,
    )
    print(f"[SERVER] PID {proc.pid} started at {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
    sys.stdout.flush()
    return proc


# ──────────────────────────────────────────────
# 6.  Main loop
# ──────────────────────────────────────────────
def main():
    global server_process

    print("=" * 60)
    print("  Rust Dedicated Server – Auto Manager (DepotDownloader)")
    print("=" * 60)
    print()

    # First-time install or update
    if not install_or_update_server():
        print("[MANAGER] FATAL – Could not download the Rust server.")
        print("[MANAGER] Exiting.")
        sys.exit(1)

    # Verify the server binary exists
    if not RUST_SERVER_EXE.exists():
        print(f"[MANAGER] FATAL – {RUST_SERVER_EXE} not found after download.")
        print("[MANAGER] Exiting.")
        sys.exit(1)

    write_server_cfg()

    consecutive_fast_crashes = 0
    consecutive_crashes = 0           # counts ANY non-zero exit

    while not shutdown_requested:
        start_time = time.time()

        server_process = start_server()

        server_process.wait()
        exit_code = server_process.returncode
        elapsed = time.time() - start_time
        print(f"\n[MANAGER] Server exited with code {exit_code} at "
              f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  (ran {elapsed:.0f}s)")

        if shutdown_requested:
            break

        # ── OOM / SIGKILL detection ──────────────────────────
        if exit_code in (-9, 137):     # SIGKILL / 128+9
            print("[MANAGER] WARNING – Server was killed (exit -9 / 137).")
            print("[MANAGER]   This usually means the OS ran out of memory (OOM killer).")
            print(f"[MANAGER]   Current SERVER_WORLDSIZE = {SERVER_WORLDSIZE}")
            print("[MANAGER]   Try reducing SERVER_WORLDSIZE (e.g. 2500-3000) or")
            print("[MANAGER]   adding more RAM to the host.")

        # ── Fast-crash counter (< 60 s uptime) ──────────────
        if elapsed < 60:
            consecutive_fast_crashes += 1
            print(f"[MANAGER] Fast crash detected ({consecutive_fast_crashes}/{MAX_FAST_CRASHES})")
            if consecutive_fast_crashes >= MAX_FAST_CRASHES:
                print(f"[MANAGER] FATAL – {MAX_FAST_CRASHES} consecutive fast crashes. Stopping.")
                print("[MANAGER] Check the server log for errors.")
                sys.exit(1)
        else:
            consecutive_fast_crashes = 0

        # ── Total consecutive crash counter (any uptime) ────
        if exit_code != 0:
            consecutive_crashes += 1
            print(f"[MANAGER] Consecutive crash #{consecutive_crashes}/{MAX_TOTAL_CRASHES}")
            if consecutive_crashes >= MAX_TOTAL_CRASHES:
                print(f"[MANAGER] FATAL – Server crashed {MAX_TOTAL_CRASHES} times in a row. Stopping.")
                print("[MANAGER] Check the server log and system memory before restarting.")
                sys.exit(1)
        else:
            consecutive_crashes = 0     # clean exit (code 0) resets the counter

        print(f"[MANAGER] Restarting in {RESTART_DELAY}s – checking for updates first …\n")
        time.sleep(RESTART_DELAY)
        install_or_update_server()
        write_server_cfg()

    print("[MANAGER] Server manager stopped. Goodbye!")


if __name__ == "__main__":
    main()
