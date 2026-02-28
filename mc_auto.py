#!/usr/bin/env python3
"""
mc_auto.py - Single-file Minecraft server installer and starter for Pterodactyl

Usage (startup command):
  python3 mc_auto.py

The script will use defaults: install dir `/home/container`, mem 512M/1G.
Optional: provide a direct JAR URL by setting the `SERVER_JAR_URL` env var
or by running `python3 mc_auto.py /home/container 512M 1G <jar_url>`.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def download_stream(url: str, dest: str, chunk: int = 8192) -> None:
    eprint(f"Downloading {url} -> {dest}")
    req = Request(url, headers={"User-Agent": "mc_auto/1.0"})
    try:
        with urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            while True:
                b = r.read(chunk)
                if not b:
                    break
                f.write(b)
    except HTTPError as ex:
        raise RuntimeError(f"HTTP error {ex.code} when downloading {url}")
    except URLError as ex:
        raise RuntimeError(f"URL error {ex} when downloading {url}")


def safe_extract_tar_gz(archive: str, dest: str) -> None:
    # extract into dest safely (no chown, no absolute paths)
    with tarfile.open(archive, "r:gz") as t:
        for member in t.getmembers():
            name = member.name
            if name.startswith("/"):
                # skip absolute paths
                continue
            if ".." in name.split("/"):
                continue
            member_path = os.path.join(dest, name)
            if member.isdir():
                ensure_dir(member_path)
            else:
                ensure_dir(os.path.dirname(member_path))
                f = t.extractfile(member)
                if f is None:
                    continue
                with open(member_path, "wb") as out:
                    shutil.copyfileobj(f, out)
                # set executable bit for bin/*
                if os.path.basename(member_path) in ("java", "javac"):
                    try:
                        os.chmod(member_path, 0o755)
                    except Exception:
                        pass


def find_java_in_tree(path: str) -> str | None:
    """Search a directory tree for an executable `java` binary and return its path."""
    if not path or not os.path.exists(path):
        return None
    for root, dirs, files in os.walk(path):
        if "java" in files:
            candidate = os.path.join(root, "java")
            if os.access(candidate, os.X_OK):
                return candidate
    return None


def download_portable_jre_try(tmp_base: str, install_dir: str | None = None) -> str:
    # Try multiple sources; return path to java binary
    # Try newer Java 21+ builds first (some server jars require newer class versions),
    # then fall back to Java 17 if 21 isn't available.
    candidates = [
        # Adoptium Temurin 21 (API latest binary)
        "https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jre/hotspot/normal/eclipse?project=jdk",
        # Temurin 21 direct release artifact
        "https://github.com/adoptium/temurin21-binaries/releases/latest/download/OpenJDK21U-jre_x64_linux_hotspot.tar.gz",
        # Liberica 21
        "https://github.com/bell-sw/liberica-releases/releases/latest/download/liberica-jre-21-linux-amd64.tar.gz",
        # Fall back to Temurin 17 options
        "https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jre/hotspot/normal/eclipse?project=jdk",
        "https://github.com/adoptium/temurin17-binaries/releases/latest/download/OpenJDK17U-jre_x64_linux_hotspot.tar.gz",
        "https://github.com/bell-sw/liberica-releases/releases/latest/download/liberica-jre-17-linux-amd64.tar.gz",
    ]
    last_err = None
    for url in candidates:
        tmpdir = tempfile.mkdtemp(prefix="mc_jre_", dir=tmp_base)
        archive = os.path.join(tmpdir, "jre.tar.gz")
        try:
            eprint(f"Attempting JRE download from: {url}")
            download_stream(url, archive)
            safe_extract_tar_gz(archive, tmpdir)
            # locate java binary
            for root, dirs, files in os.walk(tmpdir):
                if "java" in files and os.access(os.path.join(root, "java"), os.X_OK):
                    java_bin = os.path.join(root, "java")
                    # root is typically the 'bin' directory; move parent to install_dir if requested
                    if install_dir:
                        parent = os.path.dirname(root)
                        try:
                            if os.path.exists(install_dir):
                                shutil.rmtree(install_dir)
                            shutil.move(parent, install_dir)
                            java_bin = os.path.join(install_dir, os.path.relpath(java_bin, parent))
                            return java_bin
                        except Exception as ex:
                            eprint(f"Failed to move JRE into place: {ex}")
                            return java_bin
                    return java_bin
            last_err = RuntimeError("JRE downloaded but no java binary found")
        except Exception as ex:
            eprint(f"JRE attempt failed ({url}): {ex}")
            last_err = ex
            try:
                shutil.rmtree(tmpdir)
            except Exception:
                pass
            continue
    raise last_err or RuntimeError("All JRE download attempts failed")


def try_get_public_ip() -> str | None:
    services = ["https://api.ipify.org", "https://ifconfig.co/ip", "https://ifconfig.me/ip"]
    for s in services:
        try:
            req = Request(s, headers={"User-Agent": "mc_auto/1.0"})
            with urlopen(req, timeout=8) as r:
                ip = r.read().decode().strip()
                # basic validation
                parts = ip.split('.')
                if len(parts) == 4:
                    return ip
        except Exception:
            continue
    return None


def download_paper_fallback(dest: str) -> None:
    # Prefer PaperMC API to resolve version/build. Allow override via PAPER_VERSION.
    # If API download fails, try SERVER_JAR_FALLBACK_URL env var, then the known Paper 1.21.11 URL.
    env_ver = os.environ.get("PAPER_VERSION")
    if env_ver:
        version = env_ver
    else:
        try:
            api = "https://papermc.io/api/v2/projects/paper"
            req = Request(api, headers={"User-Agent": "mc_auto/1.0"})
            with urlopen(req, timeout=10) as r:
                data = json.load(r)
                versions = data.get("versions") or []
                version = versions[-1] if versions else "1.21.11"
        except Exception:
            version = "1.21.11"

    try:
        builds_url = f"https://papermc.io/api/v2/projects/paper/versions/{version}"
        req = Request(builds_url, headers={"User-Agent": "mc_auto/1.0"})
        with urlopen(req, timeout=10) as r:
            data = json.load(r)
            builds = data.get("builds") or []
            if not builds:
                raise RuntimeError(f"No builds found for Paper version {version}")
            build = builds[-1]
            jar_url = f"https://papermc.io/api/v2/projects/paper/versions/{version}/builds/{build}/downloads/paper-{version}-{build}.jar"
            download_stream(jar_url, dest)
            return
    except Exception as ex:
        # Try explicit fallback provided by panel/user
        fallback_env = os.environ.get("SERVER_JAR_FALLBACK_URL")
        if fallback_env:
            eprint(f"PaperMC API download failed ({ex}); attempting SERVER_JAR_FALLBACK_URL={fallback_env}")
            download_stream(fallback_env, dest)
            return
        # Known working Paper 1.21.11 artifact (provided by user)
        known_12111 = "https://fill-data.papermc.io/v1/objects/ec5a877eb5f01372cb23665595913f3593e3c746dfcf125d34f6f0ba69acb4d1/paper-1.21.11-125.jar"
        eprint(f"PaperMC API download failed ({ex}); attempting known fallback for 1.21.11")
        download_stream(known_12111, dest)


def main(argv: list[str]) -> int:
    # defaults
    install_dir = argv[0] if len(argv) > 0 else os.environ.get("INSTALL_DIR", "/home/container")
    mem_min = argv[1] if len(argv) > 1 else os.environ.get("MEM_MIN", "256M")
    mem_max = argv[2] if len(argv) > 2 else os.environ.get("MEM_MAX", "512M")
    jar_url = argv[3] if len(argv) > 3 else os.environ.get("SERVER_JAR_URL", "")

    ensure_dir(install_dir)
    server_jar = os.path.join(install_dir, "server.jar")

    if os.path.exists(server_jar):
        print("server.jar already present, skipping download")
    else:
        if jar_url:
            try:
                download_stream(jar_url, server_jar)
            except Exception as ex:
                eprint("Failed to download provided jar:", ex)
                return 1
        else:
            try:
                download_paper_fallback(server_jar)
            except Exception as ex:
                eprint("Automatic Paper download failed:", ex)
                return 1

    # write eula
    eula = os.path.join(install_dir, "eula.txt")
    if not os.path.exists(eula):
        with open(eula, "w", encoding="utf-8") as f:
            f.write("eula=true\n")

    # server.properties
    port = os.environ.get("SERVER_PORT") or os.environ.get("PORT") or "25565"
    max_players = os.environ.get("MAX_PLAYERS", "20")
    motd = os.environ.get("MOTD", "Managed by mc_auto")
    with open(os.path.join(install_dir, "server.properties"), "w", encoding="utf-8") as f:
        f.write(f"server-port={port}\n")
        f.write(f"max-players={max_players}\n")
        f.write(f"motd={motd}\n")

    # ensure Java
    java_path = shutil.which("java")
    env = None
    if not java_path:
        # persistent JRE cache location (inside install dir by default)
        jre_cache = os.environ.get("JRE_CACHE_DIR") or os.path.join(install_dir, ".jre")
        # If a cached JRE exists, use it (avoids re-downloading on restarts)
        cached = find_java_in_tree(jre_cache)
        if cached:
            java_path = cached
            env = os.environ.copy()
            env["PATH"] = os.path.dirname(java_path) + os.pathsep + env.get("PATH", "")
        else:
            # Decide whether to auto-install JRE:
            # - If AUTO_INSTALL_JRE env is set, respect it.
            # - Otherwise, enable auto-install when total memory >= 1GB (detect via /proc/meminfo).
            auto_env = os.environ.get("AUTO_INSTALL_JRE")
            if auto_env is None:
                mem_kb = 0
                try:
                    with open("/proc/meminfo", "r", encoding="utf-8") as mm:
                        for ln in mm:
                            if ln.startswith("MemTotal:"):
                                parts = ln.split()
                                mem_kb = int(parts[1])
                                break
                except Exception:
                    mem_kb = 0
                eprint(f"Detected MemTotal: {mem_kb} kB")
                auto_install = mem_kb >= 1000000
            else:
                auto_install = auto_env.lower() in ("yes", "1", "true")

            if auto_install:
                try:
                    tmp_base = "/tmp" if os.path.isdir("/tmp") else install_dir
                    java_bin = download_portable_jre_try(tmp_base, install_dir=jre_cache)
                    java_path = java_bin
                    env = os.environ.copy()
                    env["PATH"] = os.path.dirname(java_path) + os.pathsep + env.get("PATH", "")
                except Exception as ex:
                    eprint("Failed to install portable JRE:", ex)
                    return 1
            else:
                eprint("Java not found and AUTO_INSTALL_JRE disabled or low memory detected.")
                eprint("Options: set panel env AUTO_INSTALL_JRE=yes to attempt download (requires >1GB memory),")
                eprint("or use a Java-enabled container image or add Java to the image.")
                return 1

    cmd = [java_path, f"-Xms{mem_min}", f"-Xmx{mem_max}", "-jar", server_jar, "nogui"]
    print("Starting server:", " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=install_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)

    public_ip = try_get_public_ip()
    join = f"{public_ip or 'SERVER_IP'}:{port}"
    ready = False
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if not ready and ("Done (" in line or "For help" in line):
                ready = True
                print(f"\nServer reports ready. Join at: {join}\n")
        rc = proc.wait()
        print(f"Server exited with code {rc}")
        return rc or 0
    except KeyboardInterrupt:
        eprint("Stopping server")
        proc.terminate()
        proc.wait(timeout=10)
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        eprint("Fatal:", e)
        sys.exit(1)