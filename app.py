#!/usr/bin/env python3
"""Dev Manager — Local process manager for all dev applications.

Groups apps by life-os zone (Personal, Professor, LLC, Services).
Start, stop, and monitor processes from a single dashboard.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import yaml
from flask import Flask, jsonify, render_template_string, request

BASE_DIR = Path(__file__).parent
REGISTRY_PATH = BASE_DIR / "processes.yaml"

app = Flask(__name__)

# Track processes we've started (pid -> app_id)
managed_pids: dict[str, int] = {}


def load_registry() -> dict:
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


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
    """Return status of all registered apps."""
    registry = load_registry()
    zones = {}
    for zone_id, apps in registry.items():
        if not isinstance(apps, list):
            continue
        zone_apps = []
        for a in apps:
            status = get_app_status(a)
            zone_apps.append({**a, **status})
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
        # Start the process detached
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=work_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
        managed_pids[app_id] = proc.pid
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

.app-row {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 14px; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); margin-bottom: 6px; transition: border-color 0.15s;
}
.app-row:hover { border-color: #3a3a4a; }
.app-status { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.app-status.running { background: var(--green); box-shadow: 0 0 6px var(--green); }
.app-status.stopped { background: var(--red); opacity: 0.4; }
.app-status.unmanaged { background: var(--text-muted); }
.app-info { flex: 1; min-width: 0; }
.app-name { font-size: 14px; font-weight: 600; }
.app-desc { font-size: 11px; color: var(--text-muted); margin-top: 1px; }
.app-meta { display: flex; gap: 16px; font-size: 11px; color: var(--text-muted); flex-shrink: 0; }
.app-meta span { white-space: nowrap; }
.app-port { font-family: var(--mono); font-size: 11px; color: var(--blue); }
.app-pid { font-family: var(--mono); font-size: 11px; }
.app-uptime { color: var(--green); }
.app-actions { display: flex; gap: 6px; flex-shrink: 0; }

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
</style>
</head>
<body>

<div class="toolbar">
  <div>
    <h1>Dev Manager</h1>
    <div class="subtitle">Local process manager for life-os</div>
  </div>
  <div class="toolbar-right">
    <button class="btn btn-danger" onclick="killStale()">Kill Stale Processes</button>
    <button class="btn" onclick="refresh()">Refresh</button>
  </div>
</div>

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
}

function render(zones) {
  let totalApps = 0, runningApps = 0;
  let html = '';

  for (const [zoneId, apps] of Object.entries(zones)) {
    const label = ZONE_LABELS[zoneId] || zoneId;
    html += `<div class="zone zone-${zoneId}">`;
    html += `<div class="zone-header"><span class="zone-dot"></span>${label}</div>`;

    for (const a of apps) {
      totalApps++;
      if (a.running) runningApps++;
      const isManaged = a.managed !== false;
      const statusClass = !isManaged ? 'unmanaged' : a.running ? 'running' : 'stopped';

      html += `<div class="app-row">
        <div class="app-status ${statusClass}"></div>
        <div class="app-info">
          <div class="app-name">${a.name}</div>
          <div class="app-desc">${a.description || ''}</div>
        </div>
        <div class="app-meta">
          ${a.port ? `<span class="app-port">:${a.port}</span>` : ''}
          ${a.running ? `<span class="app-pid">PID ${a.pid}</span>` : ''}
          ${a.uptime ? `<span class="app-uptime">${a.uptime}</span>` : ''}
        </div>
        <div class="app-actions">
          ${a.running && a.port ? `<button class="btn btn-open" onclick="window.open('http://localhost:${a.port}')">Open</button>` : ''}
          ${isManaged && !a.running ? `<button class="btn btn-start" onclick="startApp('${a.id}')">Start</button>` : ''}
          ${isManaged && a.running ? `<button class="btn btn-stop" onclick="stopApp('${a.id}')">Stop</button><button class="btn btn-danger" onclick="killApp('${a.id}', '${a.name}')">Kill</button>` : ''}
          ${!isManaged ? `<span style="font-size:10px;color:var(--text-muted)">monitor only</span>` : ''}
        </div>
      </div>`;
    }
    html += '</div>';
  }

  document.getElementById('zones').innerHTML = html;
  document.getElementById('summary').innerHTML = `
    <div class="summary-card"><span class="num">${totalApps}</span>registered</div>
    <div class="summary-card running"><span class="num">${runningApps}</span>running</div>
    <div class="summary-card"><span class="num">${totalApps - runningApps}</span>stopped</div>
  `;
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
