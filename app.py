#!/usr/bin/env python3
"""Dev Manager — Local process manager for all dev applications.

Groups apps by life-os zone (Personal, Professor, LLC, Services).
Start, stop, and monitor processes from a single dashboard.
"""

import collections
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil
import yaml
from flask import Flask, jsonify, render_template_string, request

BASE_DIR = Path(__file__).parent
REGISTRY_PATH = BASE_DIR / "processes.yaml"

app = Flask(__name__)

# Track processes we've started (pid -> app_id)
managed_pids: dict[str, int] = {}

# Process log buffers: app_id -> deque of log lines
LOG_MAX_LINES = 200
process_logs: dict[str, collections.deque] = {}

# Auto-restart tracking: app_id -> {retries, last_crash, disabled}
MAX_RETRIES = 3
RETRY_WINDOW = 300  # seconds — reset retry count after this many seconds without a crash
restart_state: dict[str, dict] = {}

# "No-local" mode: when this flag file exists, the watchdog will NOT auto-start
# (or auto-restart) any app tagged `local_llm: true` in processes.yaml. Lets the
# owner keep life-os + dashboards live while the local LLM stack stays down
# (e.g. to free the GPU/RAM). Toggle via C:\life-os\meta\scripts\no-local-mode.ps1.
NO_LOCAL_FLAG = Path(r"C:\life-os\meta\state\no-local-mode.flag")

# Machine modes (normal | no-local | vr). The engine script is the canonical actuator; Dev
# Manager just reads state and launches it. See
# C:\life-os\meta\scripts\machine-mode.ps1 and the design spec under docs/superpowers/specs/.
MODE_FILE = Path(r"C:\life-os\meta\state\machine-mode.json")
MODE_SCRIPT = Path(r"C:\life-os\meta\scripts\machine-mode.ps1")
FOCUS_SCRIPT = Path(r"C:\life-os\meta\scripts\focus-mode.ps1")
GPU_STATUS_SCRIPT = Path(r"C:\life-os\meta\scripts\gpu-status.ps1")
VALID_MODES = ("normal", "no-local", "vr")

# Processes the GPU-hog stop endpoint refuses to kill, even if they pin the GPU. Mirrors the
# $Protected list in gpu-status.ps1 — desktop compositor, shell, session-critical bits.
GPU_PROTECTED = {
    "dwm", "explorer", "csrss", "winlogon", "wininit", "services", "lsass", "fontdrvhost",
    "shellexperiencehost", "searchhost", "startmenuexperiencehost", "textinputhost", "sihost",
    "ctfmon", "dllhost", "shellhost", "systemsettings", "applicationframehost",
}


def no_local_mode() -> bool:
    try:
        return NO_LOCAL_FLAG.exists()
    except Exception:
        return False


def current_mode() -> dict:
    """Read the active machine mode from machine-mode.json, inferring legacy state."""
    import json

    try:
        if MODE_FILE.exists():
            data = json.loads(MODE_FILE.read_text(encoding="utf-8"))
            mode = data.get("mode") or "normal"
            return {
                "mode": mode,
                "status": data.get("status", "active"),
                "entered_at": data.get("enteredAt"),
                "quieted_tasks": data.get("disabledByMode", []),
            }
    except Exception:
        pass
    # No state file: infer from the flag (pre-engine / hand-set state).
    if no_local_mode():
        return {"mode": "no-local", "status": "active", "entered_at": None, "quieted_tasks": []}
    return {"mode": "normal", "status": "active", "entered_at": None, "quieted_tasks": []}


def _launch_detached_ps(script: Path, args: list[str]) -> None:
    """Fire-and-forget a PowerShell script with no console window."""
    cmd = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-NoProfile", "-File", str(script)] + args
    subprocess.Popen(
        cmd,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def load_registry() -> dict:
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_status_md(path: Path) -> dict | None:
    """Parse a project STATUS.md into a dict of key fields."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    result = {}
    for key in ("id", "status", "next_action", "blockers", "last_session"):
        match = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
        if match:
            result[key] = match.group(1).strip()
    # Extract project name from heading
    heading = re.search(r"^#\s+(.+?)(?:\s*—|\s*$)", text, re.MULTILINE)
    if heading:
        result["project_name"] = heading.group(1).strip()
    return result if result else None


def get_project_state(app_cfg: dict) -> dict | None:
    """Read STATUS.md from an app's directory."""
    work_dir = app_cfg.get("dir")
    if not work_dir:
        return None
    status_path = Path(work_dir) / "STATUS.md"
    return parse_status_md(status_path)


def find_process_on_port(port: int) -> dict | None:
    """Find a process listening on a given port."""
    if not port:
        return None
    for conn in psutil.net_connections(kind="inet"):
        if conn.laddr.port == port and conn.status == "LISTEN":
            try:
                proc = psutil.Process(conn.pid)
                return {
                    "pid": conn.pid,
                    "name": proc.name(),
                    "cmdline": " ".join(proc.cmdline()),
                    "create_time": proc.create_time(),
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return {"pid": conn.pid, "name": "unknown", "cmdline": "", "create_time": 0}
    return None


def find_process_by_command(command: str, work_dir: str) -> dict | None:
    """Find a running process matching a command pattern."""
    if not command:
        return None
    # Extract the key part of the command for matching
    parts = command.split()
    search_terms = [p for p in parts if not p.startswith("-") and p != "python"]
    if not search_terms:
        return None

    for proc in psutil.process_iter(["pid", "name", "cmdline", "cwd", "create_time"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            if all(term in cmdline for term in search_terms):
                return {
                    "pid": proc.info["pid"],
                    "name": proc.info["name"],
                    "cmdline": cmdline,
                    "create_time": proc.info["create_time"],
                }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def get_app_status(app_cfg: dict) -> dict:
    """Get the runtime status of a registered app."""
    port = app_cfg.get("port")
    command = app_cfg.get("command", "")
    work_dir = app_cfg.get("dir", "")

    proc = None
    if port:
        proc = find_process_on_port(port)
    if not proc and command:
        proc = find_process_by_command(command, work_dir)

    if proc:
        uptime = time.time() - proc.get("create_time", time.time())
        hours, remainder = divmod(int(uptime), 3600)
        minutes, _ = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"
        return {
            "running": True,
            "pid": proc["pid"],
            "uptime": uptime_str,
        }
    return {"running": False, "pid": None, "uptime": None}


@app.route("/")
def dashboard():
    return render_template_string(TEMPLATE)


@app.route("/api/status")
def api_status():
    """Return status of all registered apps, including project state and logs."""
    registry = load_registry()
    zones = {}
    for zone_id, apps in registry.items():
        if not isinstance(apps, list):
            continue
        zone_apps = []
        for a in apps:
            status = get_app_status(a)
            project = get_project_state(a) or {}
            log_lines = list(process_logs.get(a.get("id", ""), []))
            zone_apps.append({
                **a,
                **status,
                "project": project,
                "log_tail": log_lines[-50:],  # last 50 lines for UI
            })
        zones[zone_id] = zone_apps
    return jsonify(zones)


@app.route("/api/start/<app_id>", methods=["POST"])
def api_start(app_id):
    """Start a registered app."""
    registry = load_registry()
    app_cfg = None
    for zone, apps in registry.items():
        if not isinstance(apps, list):
            continue
        for a in apps:
            if a.get("id") == app_id:
                app_cfg = a
                break

    if not app_cfg:
        return jsonify({"error": "App not found"}), 404
    if app_cfg.get("managed") is False:
        return jsonify({"error": "This service is not managed — monitor only"}), 400

    command = app_cfg.get("command")
    work_dir = app_cfg.get("dir")
    if not command or not work_dir:
        return jsonify({"error": "No command or directory configured"}), 400

    # Check if already running
    status = get_app_status(app_cfg)
    if status["running"]:
        return jsonify({"error": "Already running", "pid": status["pid"]}), 409

    try:
        proc = _start_process(app_id, command, work_dir)
        # Give it a moment to bind the port
        time.sleep(1.5)
        new_status = get_app_status(app_cfg)
        return jsonify({"ok": True, "pid": new_status.get("pid") or proc.pid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stop/<app_id>", methods=["POST"])
def api_stop(app_id):
    """Stop a running app (graceful, then force)."""
    registry = load_registry()
    app_cfg = None
    for zone, apps in registry.items():
        if not isinstance(apps, list):
            continue
        for a in apps:
            if a.get("id") == app_id:
                app_cfg = a
                break

    if not app_cfg:
        return jsonify({"error": "App not found"}), 404
    if app_cfg.get("managed") is False:
        return jsonify({"error": "This service is not managed — monitor only"}), 400

    status = get_app_status(app_cfg)
    if not status["running"]:
        return jsonify({"error": "Not running"}), 400

    pid = status["pid"]
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        # Kill children first (Flask reloader forks), then parent
        for child in children:
            child.kill()
        parent.kill()
        parent.wait(timeout=5)
        managed_pids.pop(app_id, None)
        return jsonify({"ok": True, "killed": pid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/kill/<app_id>", methods=["POST"])
def api_kill(app_id):
    """Force-kill a running app and all its child processes."""
    registry = load_registry()
    app_cfg = None
    for zone, apps in registry.items():
        if not isinstance(apps, list):
            continue
        for a in apps:
            if a.get("id") == app_id:
                app_cfg = a
                break

    if not app_cfg:
        return jsonify({"error": "App not found"}), 404

    status = get_app_status(app_cfg)
    if not status["running"]:
        return jsonify({"error": "Not running"}), 400

    pid = status["pid"]
    killed_pids = []
    try:
        parent = psutil.Process(pid)
        # Collect entire process tree
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.kill()
                killed_pids.append(child.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        parent.kill()
        killed_pids.append(pid)
        parent.wait(timeout=5)
        managed_pids.pop(app_id, None)
        return jsonify({"ok": True, "killed": killed_pids})
    except Exception as e:
        return jsonify({"error": str(e), "partial_killed": killed_pids}), 500


@app.route("/api/kill-stale", methods=["POST"])
def api_kill_stale():
    """Kill orphaned Python processes not in the registry."""
    registry = load_registry()

    # Gather all known commands
    known_commands = set()
    for zone, apps in registry.items():
        if not isinstance(apps, list):
            continue
        for a in apps:
            cmd = a.get("command", "")
            if cmd:
                known_commands.add(cmd)

    killed = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["name"] not in ("python.exe", "python3.exe"):
                continue
            cmdline = " ".join(proc.info["cmdline"] or [])
            # Skip VS Code LSP servers
            if "lsp_server" in cmdline or "language_server" in cmdline:
                continue
            # Skip this process
            if proc.info["pid"] == os.getpid():
                continue
            # Check if it's a known registered app
            is_known = any(term in cmdline for term in ["run.py serve", "app.py", "main.py", "launch_main"])
            if is_known:
                # Check if any registered app claims this process
                claimed = False
                for zone, apps in registry.items():
                    if not isinstance(apps, list):
                        continue
                    for a in apps:
                        status = get_app_status(a)
                        if status.get("pid") == proc.info["pid"]:
                            claimed = True
                            break
                if not claimed:
                    proc.kill()
                    killed.append({"pid": proc.info["pid"], "cmd": cmdline[:100]})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return jsonify({"ok": True, "killed": killed})


def _start_process(app_id: str, command: str, work_dir: str) -> subprocess.Popen:
    """Start a process with log capture. Returns the Popen object."""
    if app_id not in process_logs:
        process_logs[app_id] = collections.deque(maxlen=LOG_MAX_LINES)

    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # CREATE_NO_WINDOW: spawned PowerShell/cmd children run headless; no
        # visible console window pops up. Without this, dev-manager spawned
        # from tray still gives each child a fresh console. Added 2026-05-14
        # so vLLM and LiteLLM stop cluttering the taskbar.
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    managed_pids[app_id] = proc.pid

    # Background thread to read stdout and store in log buffer
    def _reader():
        try:
            for line in proc.stdout:
                ts = datetime.now().strftime("%H:%M:%S")
                process_logs[app_id].append(f"[{ts}] {line.rstrip()}")
        except Exception:
            pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return proc


# ---------------------------------------------------------------------------
# Logs API
# ---------------------------------------------------------------------------
@app.route("/api/logs/<app_id>")
def api_logs(app_id):
    """Return full log buffer for an app."""
    lines = list(process_logs.get(app_id, []))
    return jsonify({"app_id": app_id, "lines": lines, "count": len(lines)})


@app.route("/api/logs/<app_id>/clear", methods=["POST"])
def api_clear_logs(app_id):
    """Clear log buffer for an app."""
    if app_id in process_logs:
        process_logs[app_id].clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Machine mode endpoints (normal | no-local | vr)
# ---------------------------------------------------------------------------
@app.route("/api/mode")
def api_mode():
    """Return the active machine mode."""
    return jsonify(current_mode())


@app.route("/api/mode/<mode>", methods=["POST"])
def api_set_mode(mode):
    """Switch machine mode by launching the engine script detached."""
    if mode not in VALID_MODES:
        return jsonify({"error": f"Unknown mode '{mode}'", "valid": list(VALID_MODES)}), 400
    if not MODE_SCRIPT.exists():
        return jsonify({"error": f"Engine script not found: {MODE_SCRIPT}"}), 500
    try:
        _launch_detached_ps(MODE_SCRIPT, ["-Mode", mode])
        return jsonify({"ok": True, "mode": mode, "status": "applying"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reclaim-vram", methods=["POST"])
def api_reclaim_vram():
    """One-shot: close GUI VRAM nibblers for a heavy local-LLM session (focus-mode)."""
    if not FOCUS_SCRIPT.exists():
        return jsonify({"error": f"Focus script not found: {FOCUS_SCRIPT}"}), 500
    try:
        _launch_detached_ps(FOCUS_SCRIPT, [])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/gpu")
def api_gpu():
    """Live GPU VRAM + non-managed processes pinning the GPU (via gpu-status.ps1).

    Surfaces ad-hoc GPU jobs (Orator renders, games, encoders) the mode system does NOT
    manage, so VR mode doesn't falsely promise a free GPU. Read-only.
    """
    import json

    if not GPU_STATUS_SCRIPT.exists():
        return jsonify({"error": "gpu-status.ps1 not found", "vram": None, "hogs": []}), 200
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(GPU_STATUS_SCRIPT), "-MinUtil", "20", "-Samples", "2"],
            capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        data = json.loads(out.stdout) if out.stdout.strip() else {"vram": None, "hogs": []}
        # Normalize hogs to a list (ConvertTo-Json emits a single object, not a list, for one item).
        hogs = data.get("hogs") or []
        if isinstance(hogs, dict):
            hogs = [hogs]
        data["hogs"] = hogs
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e), "vram": None, "hogs": []}), 200


@app.route("/api/gpu/stop/<int:proc_id>", methods=["POST"])
def api_gpu_stop(proc_id):
    """Stop a specific GPU-hogging process by PID. Refuses protected/system processes."""
    try:
        proc = psutil.Process(proc_id)
        name = (proc.name() or "").lower().replace(".exe", "")
        if name in GPU_PROTECTED:
            return jsonify({"error": f"Refusing to stop protected process '{name}'"}), 400
        if proc_id == os.getpid():
            return jsonify({"error": "Refusing to stop Dev Manager itself"}), 400
        for child in proc.children(recursive=True):
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        proc.kill()
        proc.wait(timeout=5)
        return jsonify({"ok": True, "killed": proc_id, "name": name})
    except psutil.NoSuchProcess:
        return jsonify({"error": "Process no longer running"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Health check endpoint
# ---------------------------------------------------------------------------
@app.route("/api/health")
def api_health():
    """Return a JSON summary of all app statuses — for external integrations."""
    registry = load_registry()
    total = 0
    running = 0
    apps_summary = []
    for zone_id, apps in registry.items():
        if not isinstance(apps, list):
            continue
        for a in apps:
            total += 1
            status = get_app_status(a)
            if status["running"]:
                running += 1
            project = get_project_state(a) or {}
            apps_summary.append({
                "id": a.get("id"),
                "name": a.get("name"),
                "zone": zone_id,
                "running": status["running"],
                "pid": status.get("pid"),
                "uptime": status.get("uptime"),
                "project_status": project.get("status"),
                "next_action": project.get("next_action"),
            })
    return jsonify({
        "healthy": running > 0,
        "total": total,
        "running": running,
        "stopped": total - running,
        "timestamp": datetime.now().isoformat(),
        "apps": apps_summary,
    })


# ---------------------------------------------------------------------------
# Auto-restart watchdog
# ---------------------------------------------------------------------------
def _watchdog():
    """Background thread that monitors auto_start apps and restarts on crash."""
    time.sleep(10)  # let Flask boot first
    while True:
        try:
            registry = load_registry()
            for zone_id, apps in registry.items():
                if not isinstance(apps, list):
                    continue
                for a in apps:
                    if not a.get("auto_start"):
                        continue
                    if a.get("managed") is False:
                        continue
                    # No-local mode: leave local LLM services down.
                    if a.get("local_llm") and no_local_mode():
                        continue
                    app_id = a.get("id", "")
                    command = a.get("command")
                    work_dir = a.get("dir")
                    if not command or not work_dir:
                        continue

                    status = get_app_status(a)
                    if status["running"]:
                        # Running — reset retry state
                        if app_id in restart_state:
                            rs = restart_state[app_id]
                            if time.time() - rs.get("last_crash", 0) > RETRY_WINDOW:
                                rs["retries"] = 0
                        continue

                    # Not running — check if we should restart
                    if app_id not in restart_state:
                        restart_state[app_id] = {"retries": 0, "last_crash": 0, "disabled": False}
                    rs = restart_state[app_id]

                    if rs["disabled"]:
                        continue

                    # Reset retries if outside the window
                    if time.time() - rs.get("last_crash", 0) > RETRY_WINDOW:
                        rs["retries"] = 0

                    if rs["retries"] >= MAX_RETRIES:
                        rs["disabled"] = True
                        ts = datetime.now().strftime("%H:%M:%S")
                        if app_id not in process_logs:
                            process_logs[app_id] = collections.deque(maxlen=LOG_MAX_LINES)
                        process_logs[app_id].append(
                            f"[{ts}] WATCHDOG: {a.get('name')} crashed {MAX_RETRIES} times in {RETRY_WINDOW}s — auto-restart disabled"
                        )
                        continue

                    # Restart it
                    rs["retries"] += 1
                    rs["last_crash"] = time.time()
                    ts = datetime.now().strftime("%H:%M:%S")
                    if app_id not in process_logs:
                        process_logs[app_id] = collections.deque(maxlen=LOG_MAX_LINES)
                    process_logs[app_id].append(
                        f"[{ts}] WATCHDOG: {a.get('name')} not running — restarting (attempt {rs['retries']}/{MAX_RETRIES})"
                    )
                    try:
                        _start_process(app_id, command, work_dir)
                    except Exception as e:
                        process_logs[app_id].append(f"[{ts}] WATCHDOG: restart failed — {e}")
        except Exception:
            pass
        time.sleep(15)


# Start watchdog thread
_watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
_watchdog_thread.start()


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dev Manager</title>
<style>
:root {
  --bg: #0f0f14;
  --surface: #1a1a24;
  --surface2: #22222e;
  --border: #2a2a3a;
  --text: #e2e5ea;
  --text-muted: #6b7088;
  --green: #22c55e;
  --green-bg: rgba(34,197,94,0.1);
  --red: #ef4444;
  --red-bg: rgba(239,68,68,0.1);
  --blue: #3b82f6;
  --blue-bg: rgba(59,130,246,0.1);
  --yellow: #eab308;
  --yellow-bg: rgba(234,179,8,0.1);
  --purple: #a855f7;
  --purple-bg: rgba(168,85,247,0.1);
  --radius: 8px;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --mono: "Cascadia Code", "JetBrains Mono", monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--font); background: var(--bg); color: var(--text); min-height: 100vh; padding: 24px 32px; }
h1 { font-size: 22px; font-weight: 700; margin-bottom: 4px; }
.subtitle { font-size: 12px; color: var(--text-muted); margin-bottom: 24px; }
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.toolbar-right { display: flex; gap: 8px; }

.zone { margin-bottom: 28px; }
.zone-header {
  display: flex; align-items: center; gap: 8px;
  font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px;
  color: var(--text-muted); margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid var(--border);
}
.zone-dot { width: 8px; height: 8px; border-radius: 50%; }
.zone-personal .zone-dot { background: var(--blue); }
.zone-professor .zone-dot { background: var(--yellow); }
.zone-llc .zone-dot { background: var(--purple); }
.zone-services .zone-dot { background: var(--text-muted); }

.app-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 10px;
}
.app-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius);
  display: flex; flex-direction: column;
  transition: border-color 0.15s;
  overflow: hidden;
}
.app-card:hover { border-color: #3a3a4a; }
.app-card-main {
  display: flex; flex-direction: column;
  padding: 12px 14px; gap: 8px;
  cursor: pointer; user-select: none;
  flex: 1;
}
.app-card-header {
  display: flex; align-items: center; gap: 10px;
}
.app-status { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.app-status.running { background: var(--green); box-shadow: 0 0 6px var(--green); }
.app-status.stopped { background: var(--red); opacity: 0.4; }
.app-status.unmanaged { background: var(--text-muted); }
.app-name { font-size: 14px; font-weight: 600; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.app-port {
  font-family: var(--mono); font-size: 11px; color: var(--blue);
  padding: 2px 6px; background: var(--blue-bg); border-radius: 3px;
  flex-shrink: 0;
}
.app-desc { font-size: 11px; color: var(--text-muted); line-height: 1.4; }
.app-project { font-size: 11px; display: flex; align-items: flex-start; gap: 6px; flex-wrap: wrap; }
.app-project .proj-status { padding: 1px 6px; border-radius: 3px; font-weight: 600; font-size: 10px; text-transform: uppercase; flex-shrink: 0; }
.proj-status.active { background: var(--green-bg); color: var(--green); }
.proj-status.blocked { background: var(--red-bg); color: var(--red); }
.proj-status.deferred { background: var(--yellow-bg); color: var(--yellow); }
.proj-next { color: var(--text-muted); line-height: 1.4; }
.app-card-footer {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 14px; gap: 8px;
  border-top: 1px solid var(--border); background: rgba(0,0,0,0.15);
}
.app-meta { display: flex; gap: 10px; font-size: 11px; color: var(--text-muted); min-width: 0; }
.app-meta span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.app-pid { font-family: var(--mono); font-size: 11px; }
.app-uptime { color: var(--green); }
.app-actions { display: flex; gap: 6px; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; }
.app-unmanaged-tag { font-size: 10px; color: var(--text-muted); font-style: italic; }
.app-detail {
  display: none; padding: 10px 14px;
  border-top: 1px solid var(--border); background: rgba(0,0,0,0.2);
}
.app-detail.open { display: block; }
.log-panel {
  background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
  padding: 8px 10px; margin-top: 6px; max-height: 200px; overflow-y: auto;
  font-family: var(--mono); font-size: 11px; line-height: 1.5; color: var(--text-muted);
  white-space: pre-wrap; word-break: break-all;
}
.log-panel:empty::after { content: "No logs captured yet."; font-style: italic; }
.log-toolbar { display: flex; justify-content: space-between; align-items: center; margin-top: 6px; }
.detail-row { font-size: 11px; color: var(--text-muted); margin-top: 4px; }
.detail-row strong { color: var(--text); }
.blocker-tag { color: var(--red); font-weight: 600; }

.btn {
  padding: 5px 12px; font-size: 11px; font-weight: 600; border: 1px solid var(--border);
  border-radius: 5px; cursor: pointer; transition: all 0.15s; background: var(--surface2); color: var(--text);
}
.btn:hover { background: var(--border); }
.btn:disabled { opacity: 0.3; cursor: not-allowed; }
.btn-start { color: var(--green); border-color: rgba(34,197,94,0.3); }
.btn-start:hover { background: var(--green-bg); }
.btn-stop { color: var(--red); border-color: rgba(239,68,68,0.3); }
.btn-stop:hover { background: var(--red-bg); }
.btn-danger { color: var(--red); border-color: rgba(239,68,68,0.3); }
.btn-danger:hover { background: var(--red-bg); }
.btn-open { color: var(--blue); border-color: rgba(59,130,246,0.3); }
.btn-open:hover { background: var(--blue-bg); }

.summary { display: flex; gap: 16px; margin-bottom: 20px; }
.summary-card {
  padding: 12px 18px; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); font-size: 12px; color: var(--text-muted);
}
.summary-card .num { font-size: 22px; font-weight: 700; color: var(--text); display: block; }
.summary-card.running .num { color: var(--green); }

.toast {
  position: fixed; bottom: 20px; right: 24px; padding: 8px 16px;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  font-size: 12px; color: var(--text); box-shadow: 0 4px 12px rgba(0,0,0,0.3);
  z-index: 100; display: none;
}

/* Machine mode bar */
.mode-bar {
  display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  padding: 12px 16px; margin-bottom: 20px;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
}
.mode-bar.mode-no-local { border-color: rgba(234,179,8,0.5); background: var(--yellow-bg); }
.mode-bar.mode-vr { border-color: rgba(168,85,247,0.6); background: var(--purple-bg); }
.mode-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: var(--text-muted); }
.mode-seg { display: inline-flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
.mode-seg button {
  padding: 6px 14px; font-size: 12px; font-weight: 600; border: none; cursor: pointer;
  background: var(--surface2); color: var(--text-muted); border-right: 1px solid var(--border);
}
.mode-seg button:last-child { border-right: none; }
.mode-seg button:hover { color: var(--text); }
.mode-seg button.active { color: #fff; }
.mode-seg button.active[data-mode="normal"] { background: var(--green); }
.mode-seg button.active[data-mode="no-local"] { background: var(--yellow); color: #1a1a24; }
.mode-seg button.active[data-mode="vr"] { background: var(--purple); }
.mode-seg button:disabled { opacity: 0.6; cursor: wait; }
.mode-applying { font-size: 11px; color: var(--yellow); font-style: italic; }
.mode-note { font-size: 11px; color: var(--text-muted); flex: 1; min-width: 160px; }
.mode-spacer { flex: 1; }

/* GPU status + hog warnings */
.gpu-bar {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 8px 16px; margin-bottom: 16px; font-size: 12px;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
}
.gpu-bar.busy { border-color: rgba(234,179,8,0.45); }
.gpu-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: var(--text-muted); }
.gpu-meter { font-family: var(--mono); color: var(--text-muted); }
.gpu-hog {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 3px 8px; border-radius: 4px; background: var(--yellow-bg);
  color: var(--yellow); font-size: 11px;
}
.gpu-hog .gpu-cmd { color: var(--text-muted); font-family: var(--mono); max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.gpu-hog.protected { background: rgba(107,112,136,0.15); color: var(--text-muted); }
.btn-stopgpu { padding: 2px 8px; font-size: 10px; color: var(--red); border-color: rgba(239,68,68,0.3); }
.btn-stopgpu:hover { background: var(--red-bg); }
</style>
</head>
<body>

<div class="toolbar">
  <div>
    <h1>Dev Manager</h1>
    <div class="subtitle">Local process manager for life-os</div>
  </div>
  <div class="toolbar-right">
    <a class="btn" href="http://nsf-leith:5151/today" target="_blank" rel="noopener" style="text-decoration:none;display:inline-flex;align-items:center;">Today</a>
    <button class="btn btn-danger" onclick="killStale()">Kill Stale Processes</button>
    <button class="btn" onclick="refresh()">Refresh</button>
  </div>
</div>

<div class="mode-bar" id="mode-bar"></div>
<div id="gpu-status"></div>
<div class="summary" id="summary"></div>
<div id="zones"></div>
<div class="toast" id="toast"></div>

<script>
const ZONE_LABELS = {
  personal: 'Personal',
  professor: 'Professor',
  llc: 'LLC',
  services: 'Services',
};

async function refresh() {
  const resp = await fetch('/api/status');
  const zones = await resp.json();
  render(zones);
  refreshMode();
  refreshGpu();
}

async function refreshGpu() {
  try {
    const resp = await fetch('/api/gpu');
    const g = await resp.json();
    renderGpu(g);
  } catch (e) { /* leave last render */ }
}

function renderGpu(g) {
  const el = document.getElementById('gpu-status');
  const v = g.vram;
  const hogs = g.hogs || [];
  if (!v && hogs.length === 0) { el.innerHTML = ''; return; }
  const usedGb = v ? (v.usedMiB / 1024).toFixed(1) : '?';
  const totGb = v ? (v.totalMiB / 1024).toFixed(0) : '?';
  let meter = v ? `${usedGb} / ${totGb} GB VRAM · compute ${v.computeUtil}%` : 'GPU info unavailable';
  let hogHtml = '';
  for (const h of hogs) {
    const cmd = h.cmd ? `<span class="gpu-cmd" title="${escHtml(h.cmd)}">${escHtml(h.cmd)}</span>` : '';
    const stop = h.protected
      ? ''
      : `<button class="btn btn-stopgpu" onclick="stopGpuJob(${h.pid}, '${escHtml(h.name)}')">Stop</button>`;
    hogHtml += `<span class="gpu-hog${h.protected ? ' protected' : ''}">${escHtml(h.name)} (PID ${h.pid}) ${h.util}% ${h.engine}${cmd ? ' · ' + cmd : ''} ${stop}</span>`;
  }
  el.innerHTML = `<div class="gpu-bar${hogs.length ? ' busy' : ''}">
    <span class="gpu-label">GPU</span>
    <span class="gpu-meter">${meter}</span>
    ${hogs.length ? hogHtml : '<span style="color:var(--green)">free of non-managed jobs</span>'}
  </div>`;
}

async function stopGpuJob(pid, name) {
  if (!confirm(`Stop "${name}" (PID ${pid})? If this is a render or a remote session, that work/connection will be lost.`)) return;
  toast('Stopping ' + name + '…');
  try {
    const resp = await fetch('/api/gpu/stop/' + pid, { method: 'POST' });
    const data = await resp.json();
    toast(data.ok ? name + ' stopped' : 'Error: ' + data.error);
  } catch (e) { toast('Error: ' + e); }
  setTimeout(refreshGpu, 800);
}

const MODE_META = {
  'normal':   { label: 'Normal',   note: 'Everything runs. Local LLMs watchdog-managed.' },
  'no-local': { label: 'No-Local', note: 'Local LLMs off + pinned. life-os fully live.' },
  'vr':       { label: 'VR',       note: 'GPU freed for VR (vLLM down), pop-up tasks quieted. Dev Manager, cairn, Discord stay up.' },
};

let modeBusy = false;

async function refreshMode() {
  if (modeBusy) return;
  try {
    const resp = await fetch('/api/mode');
    const m = await resp.json();
    renderMode(m);
  } catch (e) { /* leave last render */ }
}

function renderMode(m) {
  const bar = document.getElementById('mode-bar');
  const cur = m.mode || 'normal';
  const applying = m.status === 'applying';
  bar.className = 'mode-bar mode-' + cur;
  let seg = '';
  for (const id of ['normal', 'no-local', 'vr']) {
    const active = id === cur ? ' active' : '';
    const dis = (applying || modeBusy) ? ' disabled' : '';
    seg += `<button class="${active.trim()}" data-mode="${id}"${dis} onclick="setMode('${id}')">${MODE_META[id].label}</button>`;
  }
  bar.innerHTML = `
    <span class="mode-label">Machine Mode</span>
    <div class="mode-seg">${seg}</div>
    ${applying ? '<span class="mode-applying">switching…</span>' : ''}
    <span class="mode-note">${MODE_META[cur].note}</span>
    <button class="btn btn-open" ${applying ? 'disabled' : ''} onclick="reclaimVram()">Reclaim GPU VRAM</button>
  `;
}

async function setMode(mode) {
  if (modeBusy) return;
  const cur = document.querySelector('.mode-seg button.active');
  if (cur && cur.dataset.mode === mode) { toast('Already in ' + MODE_META[mode].label + ' mode'); return; }
  if (mode === 'vr' && !confirm('Switch to VR mode? Stops vLLM (frees ~23GB VRAM) and quiets pop-up tasks. Dev Manager stays up.')) return;
  modeBusy = true;
  toast('Switching to ' + MODE_META[mode].label + '… (vLLM reload can take 30-90s on the way back)');
  try {
    const resp = await fetch('/api/mode/' + mode, { method: 'POST' });
    const data = await resp.json();
    if (!data.ok) toast('Error: ' + data.error);
  } catch (e) { toast('Error: ' + e); }
  // Poll mode until it settles to active, then release the lock.
  setTimeout(() => { modeBusy = false; pollModeUntilActive(0); }, 1500);
}

async function pollModeUntilActive(tries) {
  try {
    const resp = await fetch('/api/mode');
    const m = await resp.json();
    renderMode(m);
    if (m.status === 'applying' && tries < 40) {
      setTimeout(() => pollModeUntilActive(tries + 1), 3000);
    } else {
      toast('Mode: ' + (MODE_META[m.mode] ? MODE_META[m.mode].label : m.mode));
      refresh();
    }
  } catch (e) { /* ignore */ }
}

async function reclaimVram() {
  if (!confirm('Close GUI VRAM nibblers (Discord, Edge WebView, Parsec, NVIDIA Broadcast, PowerToys, etc.) to free VRAM for a heavy local-LLM session?')) return;
  toast('Reclaiming GPU VRAM…');
  try {
    const resp = await fetch('/api/reclaim-vram', { method: 'POST' });
    const data = await resp.json();
    toast(data.ok ? 'Closing GUI VRAM apps…' : 'Error: ' + data.error);
  } catch (e) { toast('Error: ' + e); }
}

function render(zones) {
  let totalApps = 0, runningApps = 0;
  let blockerCount = 0;
  let html = '';

  for (const [zoneId, apps] of Object.entries(zones)) {
    const label = ZONE_LABELS[zoneId] || zoneId;
    html += `<div class="zone zone-${zoneId}">`;
    html += `<div class="zone-header"><span class="zone-dot"></span>${label}</div>`;
    html += `<div class="app-grid">`;

    for (const a of apps) {
      totalApps++;
      if (a.running) runningApps++;
      const isManaged = a.managed !== false;
      const statusClass = !isManaged ? 'unmanaged' : a.running ? 'running' : 'stopped';
      const p = a.project || {};
      const projStatus = p.status || '';
      const projClass = projStatus === 'blocked' ? 'blocked' : projStatus === 'deferred' ? 'deferred' : projStatus ? 'active' : '';
      if (projStatus === 'blocked') blockerCount++;
      const hasBlocker = p.blockers && p.blockers !== 'none' && p.blockers !== 'None';
      const logLines = (a.log_tail || []);

      html += `<div class="app-card" id="card-${a.id}">
        <div class="app-card-main" onclick="toggleDetail('${a.id}')">
          <div class="app-card-header">
            <div class="app-status ${statusClass}"></div>
            <div class="app-name">${a.name}</div>
            ${a.port ? `<span class="app-port">:${a.port}</span>` : ''}
          </div>
          ${a.description ? `<div class="app-desc">${a.description}</div>` : ''}
          ${projStatus ? `<div class="app-project">
            <span class="proj-status ${projClass}">${projStatus}</span>
            ${p.next_action ? `<span class="proj-next">${truncate(p.next_action, 110)}</span>` : ''}
          </div>` : ''}
        </div>
        <div class="app-card-footer">
          <div class="app-meta">
            ${a.running ? `<span class="app-pid">PID ${a.pid}</span>` : ''}
            ${a.uptime ? `<span class="app-uptime">${a.uptime}</span>` : ''}
            ${!isManaged ? `<span class="app-unmanaged-tag">monitor only</span>` : ''}
            ${isManaged && !a.running ? `<span style="color:var(--text-muted)">stopped</span>` : ''}
          </div>
          <div class="app-actions" onclick="event.stopPropagation()">
            ${a.running && a.port ? `<button class="btn btn-open" onclick="window.open('http://localhost:${a.port}')">Open</button>` : ''}
            ${isManaged && !a.running ? `<button class="btn btn-start" onclick="startApp('${a.id}')">Start</button>` : ''}
            ${isManaged && a.running ? `<button class="btn btn-stop" onclick="stopApp('${a.id}')">Stop</button><button class="btn btn-danger" onclick="killApp('${a.id}', '${a.name}')">Kill</button>` : ''}
          </div>
        </div>
        <div class="app-detail" id="detail-${a.id}">
          ${p.project_name ? `<div class="detail-row"><strong>Project:</strong> ${p.project_name} (${p.id || ''})</div>` : ''}
          ${p.last_session ? `<div class="detail-row"><strong>Last session:</strong> ${p.last_session}</div>` : ''}
          ${hasBlocker ? `<div class="detail-row"><strong>Blocker:</strong> <span class="blocker-tag">${p.blockers}</span></div>` : ''}
          ${p.next_action ? `<div class="detail-row"><strong>Next action:</strong> ${p.next_action}</div>` : ''}
          <div class="log-toolbar">
            <strong style="font-size:11px;">Process Logs</strong>
            <button class="btn" style="padding:2px 8px;font-size:10px;" onclick="clearLogs('${a.id}')">Clear</button>
          </div>
          <div class="log-panel" id="log-${a.id}">${logLines.map(escHtml).join('\\n')}</div>
        </div>
      </div>`;
    }
    html += '</div></div>';
  }

  document.getElementById('zones').innerHTML = html;
  // Restore open panels
  for (const id of openPanels) {
    const el = document.getElementById('detail-' + id);
    if (el) el.classList.add('open');
  }
  document.getElementById('summary').innerHTML = `
    <div class="summary-card"><span class="num">${totalApps}</span>registered</div>
    <div class="summary-card running"><span class="num">${runningApps}</span>running</div>
    <div class="summary-card"><span class="num">${totalApps - runningApps}</span>stopped</div>
    ${blockerCount ? `<div class="summary-card" style="border-color:rgba(239,68,68,0.3)"><span class="num" style="color:var(--red)">${blockerCount}</span>blocked</div>` : ''}
  `;
}

const openPanels = new Set();

function toggleDetail(id) {
  const el = document.getElementById('detail-' + id);
  if (!el) return;
  el.classList.toggle('open');
  if (el.classList.contains('open')) openPanels.add(id);
  else openPanels.delete(id);
}

function truncate(s, n) {
  return s.length > n ? s.slice(0, n) + '...' : s;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function clearLogs(id) {
  await fetch('/api/logs/' + id + '/clear', { method: 'POST' });
  const el = document.getElementById('log-' + id);
  if (el) el.textContent = '';
  toast('Logs cleared for ' + id);
}

async function startApp(id) {
  toast('Starting ' + id + '...');
  const resp = await fetch('/api/start/' + id, { method: 'POST' });
  const data = await resp.json();
  if (data.ok) toast(id + ' started (PID ' + data.pid + ')');
  else toast('Error: ' + data.error);
  setTimeout(refresh, 500);
}

async function stopApp(id) {
  toast('Stopping ' + id + '...');
  const resp = await fetch('/api/stop/' + id, { method: 'POST' });
  const data = await resp.json();
  if (data.ok) toast(id + ' stopped');
  else toast('Error: ' + data.error);
  setTimeout(refresh, 500);
}

async function killApp(id, name) {
  if (!confirm(`Force-kill "${name}" and all its child processes?`)) return;
  toast('Killing ' + name + '...');
  const resp = await fetch('/api/kill/' + id, { method: 'POST' });
  const data = await resp.json();
  if (data.ok) toast(name + ' killed (' + data.killed.length + ' process(es))');
  else toast('Error: ' + data.error);
  setTimeout(refresh, 500);
}

async function killStale() {
  if (!confirm('Kill orphaned Python processes not claimed by any registered app?')) return;
  toast('Scanning...');
  const resp = await fetch('/api/kill-stale', { method: 'POST' });
  const data = await resp.json();
  if (data.ok) {
    const n = data.killed.length;
    toast(n ? `Killed ${n} stale process(es)` : 'No stale processes found');
  }
  setTimeout(refresh, 500);
}

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.display = 'block';
  clearTimeout(el._t);
  el._t = setTimeout(() => el.style.display = 'none', 3000);
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
