# AutoRustServer

Automated Rust Dedicated Server setup and management using **DepotDownloader** — no SteamCMD or 32-bit libraries required.

Works out-of-the-box on **Pterodactyl**, **Linux**, and **Windows**.

---

## Features

- **Zero-dependency bootstrap** — automatically installs required Python packages (`requests`, `psutil`) on first run
- **DepotDownloader integration** — downloads the latest release from GitHub; no manual SteamCMD setup needed
- **Automatic server installation** — downloads and validates the Rust Dedicated Server (App ID `258550`)
- **Crash recovery** — detects crashes, checks for updates, and relaunches automatically
- **Fast-crash protection** — stops after multiple consecutive quick crashes to prevent restart loops
- **Auto-updates** — checks for Rust server updates on every restart
- **Cross-platform** — supports both Linux and Windows with automatic binary detection

## Requirements

- **Python 3.10+**
- Internet access (to download DepotDownloader and the Rust server)

All other dependencies are installed automatically at runtime.

## Quick Start

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/AutoRustServer.git
cd AutoRustServer

# Run the server manager
python rust_server.py
```

On first launch the script will:

1. Install missing pip packages locally
2. Download the latest DepotDownloader from GitHub
3. Download / update the Rust Dedicated Server
4. Generate a `server.cfg`
5. Start the server

## Configuration

Edit the variables near the top of `rust_server.py` to customise your server:

| Variable | Default | Description |
|---|---|---|
| `SERVER_IDENTITY` | `myserver` | Server identity folder name |
| `SERVER_HOSTNAME` | `My Rust Server` | Server name shown in the browser |
| `SERVER_PORT` | `3109` | Game port |
| `SERVER_MAP` | `Procedural Map` | Map type |
| `SERVER_WORLDSIZE` | `3500` | World size |
| `SERVER_SEED` | `0` (random) | Map seed (`0` = random each wipe) |
| `SERVER_MAXPLAYERS` | `100` | Max player slots |
| `SERVER_DESCRIPTION` | `Auto-managed Rust server` | Server description |
| `SERVER_URL` | *(empty)* | Server website URL |
| `SERVER_BANNER` | *(empty)* | Header image URL |
| `SERVER_IP` | `0.0.0.0` | Bind address (all interfaces) |
| `RCON_PASSWORD` | `changeme` | RCON password — **change this!** |
| `RCON_WEB` | `1` | Websocket RCON (required by most panels) |
| `RESTART_DELAY` | `15` | Seconds to wait before restarting |
| `MAX_FAST_CRASHES` | `5` | Consecutive fast crashes before the manager gives up |

## How It Works

```
┌─────────────────────────┐
│   Install / Update      │
│   DepotDownloader       │
└───────────┬─────────────┘
            ▼
┌─────────────────────────┐
│   Download / Update     │
│   Rust Server           │
└───────────┬─────────────┘
            ▼
┌─────────────────────────┐
│   Write server.cfg      │
└───────────┬─────────────┘
            ▼
┌─────────────────────────┐
│   Launch Server         │◄──────────┐
└───────────┬─────────────┘           │
            ▼                         │
┌─────────────────────────┐           │
│   Monitor Process       │           │
└───────────┬─────────────┘           │
            ▼                         │
     Server exits?                    │
       │       │                      │
    Crash    Shutdown                 │
       │    requested                 │
       ▼       ▼                      │
  Check for  Exit                     │
  updates ─────────────────────────►──┘
```

## Pterodactyl

This script is designed to work as a **Pterodactyl Python egg**. Since it uses DepotDownloader instead of SteamCMD, there is no need to install 32-bit libraries in the container.

## License

This project is provided as-is. Feel free to use and modify it for your own servers.
