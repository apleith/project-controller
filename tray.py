#!/usr/bin/env python3
"""Dev Manager — System tray app.

Runs the Flask dashboard in the background and provides a system tray icon
with quick-access controls for all registered dev applications.

Usage:
    python tray.py              # Launch tray icon + dashboard server
    pythonw tray.py             # Same, but no console window (for startup)
"""

import os
import sys
import threading
import time
import webbrowser
from io import BytesIO
from pathlib import Path

import psutil
import yaml
from PIL import Image, ImageDraw, ImageFont

BASE_DIR = Path(__file__).parent
REGISTRY_PATH = BASE_DIR / "processes.yaml"
PORT = 5050
DASHBOARD_URL = f"http://127.0.0.1:{PORT}"

# Machine modes — canonical actuator is the engine script in life-os.
MODE_FILE = Path(r"C:\life-os\meta\state\machine-mode.json")
NO_LOCAL_FLAG = Path(r"C:\life-os\meta\state\no-local-mode.flag")
MODE_SCRIPT = Path(r"C:\life-os\meta\scripts\machine-mode.ps1")
FOCUS_SCRIPT = Path(r"C:\life-os\meta\scripts\focus-mode.ps1")
MODE_LABELS = {"normal": "Normal", "no-local": "No-Local", "vr": "VR"}


def current_mode() -> str:
    import json

    try:
        if MODE_FILE.exists():
            data = json.loads(MODE_FILE.read_text(encoding="utf-8"))
            return data.get("mode") or "normal"
    except Exception:
        pass
    try:
        if NO_LOCAL_FLAG.exists():
            return "no-local"
    except Exception:
        pass
    return "normal"


def _launch_detached_ps(script: Path, args: list) -> None:
    import subprocess as sp

    cmd = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-NoProfile", "-File", str(script)] + args
    try:
        sp.Popen(
            cmd,
            creationflags=sp.CREATE_NEW_PROCESS_GROUP | sp.CREATE_NO_WINDOW,
            stdout=sp.DEVNULL,
            stderr=sp.DEVNULL,
        )
    except Exception:
        pass


def set_mode_action(mode: str) -> None:
    _launch_detached_ps(MODE_SCRIPT, ["-Mode", mode])


def reclaim_vram_action() -> None:
    _launch_detached_ps(FOCUS_SCRIPT, [])


# ---------------------------------------------------------------------------
# Registry + status helpers (duplicated from app.py to avoid circular imports)
# ---------------------------------------------------------------------------
def load_registry() -> dict:
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def find_process_on_port(port: int) -> dict | None:
    if not port:
        return None
    for conn in psutil.net_connections(kind="inet"):
        if conn.laddr.port == port and conn.status == "LISTEN":
            try:
                proc = psutil.Process(conn.pid)
                return {"pid": conn.pid, "name": proc.name()}
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return {"pid": conn.pid, "name": "unknown"}
    return None


def find_process_by_command(command: str) -> dict | None:
    if not command:
        return None
    parts = command.split()
    search_terms = [p for p in parts if not p.startswith("-") and p != "python"]
    if not search_terms:
        return None
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            if all(term in cmdline for term in search_terms):
                return {"pid": proc.info["pid"], "name": proc.info["name"]}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def is_app_running(app_cfg: dict) -> bool:
    port = app_cfg.get("port")
    if port and find_process_on_port(port):
        return True
    cmd = app_cfg.get("command", "")
    if cmd and find_process_by_command(cmd):
        return True
    return False


def get_all_status() -> tuple[int, int, list]:
    """Return (total, running, list of (zone, app, running))."""
    registry = load_registry()
    total = 0
    running = 0
    apps = []
    for zone, zone_apps in registry.items():
        if not isinstance(zone_apps, list):
            continue
        for a in zone_apps:
            total += 1
            r = is_app_running(a)
            if r:
                running += 1
            apps.append((zone, a, r))
    return total, running, apps


# ---------------------------------------------------------------------------
# Icon generation
# ---------------------------------------------------------------------------
def create_icon(running: int, total: int) -> Image.Image:
    """Generate a tray icon showing running/total count."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background circle
    if running == total:
        bg = (34, 197, 94)     # green — all running
    elif running == 0:
        bg = (239, 68, 68)     # red — none running
    else:
        bg = (234, 179, 8)     # yellow — partial
    draw.ellipse([2, 2, size - 2, size - 2], fill=bg)

    # Number in center
    text = str(running)
    try:
        font = ImageFont.truetype("segoeui.ttf", 32)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2, (size - th) / 2 - 4), text, fill="white", font=font)

    return img


# ---------------------------------------------------------------------------
# Tray menu actions
# ---------------------------------------------------------------------------
def open_dashboard():
    webbrowser.open(DASHBOARD_URL)


def start_app_action(app_cfg):
    """Start an app from the tray menu."""
    import subprocess as sp
    command = app_cfg.get("command")
    work_dir = app_cfg.get("dir")
    if not command or not work_dir:
        return
    try:
        sp.Popen(
            command, shell=True, cwd=work_dir,
            stdout=sp.DEVNULL, stderr=sp.DEVNULL,
            creationflags=sp.CREATE_NEW_PROCESS_GROUP | sp.DETACHED_PROCESS,
        )
    except Exception:
        pass


def _find_app_process(app_cfg):
    """Find the process for an app config."""
    port = app_cfg.get("port")
    proc_info = None
    if port:
        proc_info = find_process_on_port(port)
    if not proc_info:
        proc_info = find_process_by_command(app_cfg.get("command", ""))
    return proc_info


def stop_app_action(app_cfg):
    """Stop an app from the tray menu."""
    proc_info = _find_app_process(app_cfg)
    if not proc_info:
        return
    try:
        parent = psutil.Process(proc_info["pid"])
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def kill_app_action(app_cfg):
    """Force-kill an app and all children from the tray menu."""
    proc_info = _find_app_process(app_cfg)
    if not proc_info:
        return
    try:
        parent = psutil.Process(proc_info["pid"])
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        parent.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def quit_app(icon):
    """Quit the tray app (stops the dashboard server too)."""
    icon.stop()
    os._exit(0)


# ---------------------------------------------------------------------------
# Build the tray menu
# ---------------------------------------------------------------------------
def build_menu():
    import pystray

    ZONE_LABELS = {
        "personal": "Personal",
        "professor": "Professor",
        "llc": "LLC",
        "services": "Services",
    }

    total, running, apps = get_all_status()
    mode = current_mode()

    mode_menu = pystray.Menu(
        *[
            pystray.MenuItem(
                MODE_LABELS[m],
                (lambda _, mm=m: set_mode_action(mm)),
                checked=(lambda item, mm=m: current_mode() == mm),
                radio=True,
            )
            for m in ("normal", "no-local", "vr")
        ],
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Reclaim GPU VRAM (focus)", lambda _: reclaim_vram_action()),
    )

    items = [
        pystray.MenuItem(f"Dev Manager — {running}/{total} running", open_dashboard, default=True),
        pystray.MenuItem(f"Mode: {MODE_LABELS.get(mode, mode)} ▸", mode_menu),
        pystray.Menu.SEPARATOR,
    ]

    current_zone = None
    for zone, a, r in apps:
        if zone != current_zone:
            current_zone = zone
            items.append(pystray.MenuItem(f"— {ZONE_LABELS.get(zone, zone)} —", None, enabled=False))

        is_managed = a.get("managed") is not False
        status = "Running" if r else "Stopped"
        port_str = f" :{a['port']}" if a.get("port") else ""
        label = f"  {'●' if r else '○'} {a['name']}{port_str}"

        if is_managed:
            if r:
                sub = pystray.Menu(
                    pystray.MenuItem("Stop", lambda _, app=a: stop_app_action(app)),
                    pystray.MenuItem("Kill", lambda _, app=a: kill_app_action(app)),
                    *(
                        [pystray.MenuItem("Open in Browser", lambda _, p=a["port"]: webbrowser.open(f"http://localhost:{p}"))]
                        if a.get("port") else []
                    ),
                )
            else:
                sub = pystray.Menu(
                    pystray.MenuItem("Start", lambda _, app=a: start_app_action(app)),
                )
            items.append(pystray.MenuItem(label, sub))
        else:
            items.append(pystray.MenuItem(label, None, enabled=False))

    items.extend([
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Dashboard", open_dashboard),
        pystray.MenuItem("Quit Dev Manager", quit_app),
    ])

    return pystray.Menu(*items)


# ---------------------------------------------------------------------------
# Periodic refresh — update icon and menu every 10 seconds
# ---------------------------------------------------------------------------
def refresh_loop(icon):
    while True:
        try:
            total, running, _ = get_all_status()
            icon.icon = create_icon(running, total)
            icon.menu = build_menu()
        except Exception:
            pass
        time.sleep(10)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    import pystray

    # Start Flask dashboard in background thread
    def run_flask():
        from app import app as flask_app
        flask_app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Wait for Flask to start
    time.sleep(1)

    # Create tray icon
    total, running, _ = get_all_status()
    icon = pystray.Icon(
        "dev-manager",
        create_icon(running, total),
        "Dev Manager",
        menu=build_menu(),
    )

    # Start refresh loop in background
    refresh_thread = threading.Thread(target=refresh_loop, args=(icon,), daemon=True)
    refresh_thread.start()

    # Run the tray icon (blocks)
    icon.run()


if __name__ == "__main__":
    main()
