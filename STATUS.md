# Dev Manager — Status

id: PERS-003
status: active
next_action: Test all four new features live (project state cards, log capture, health endpoint, auto-restart). Verify tray app still works after app.py changes.
blockers: none
last_session: 2026-04-12T07:20:00
last_session_handoff: (inline — life-os sunday session)

## Recent Changes
- 2026-04-12: Added project state integration — reads STATUS.md from each app's directory, displays project status/next_action/blockers on dashboard cards. Expandable detail panels with full project info.
- 2026-04-12: Added process log capture — stdout/stderr stored in ring buffer (200 lines), viewable per-app in expandable log panel. Clear logs button. /api/logs/<id> endpoint.
- 2026-04-12: Added /api/health endpoint — JSON summary of all apps for external integrations (ntfy, monitoring).
- 2026-04-12: Added auto-restart watchdog — background thread monitors auto_start apps, restarts on crash with 3-attempt limit per 5-minute window. Logs restart attempts.
- 2026-04-12: Initial commit pushed to GitHub (apleith/project-controller).
- 2026-04-11: Built from scratch — Flask dashboard (:5050), system tray app (pystray), process registry (YAML), start/stop/kill controls, Windows startup integration. 8 apps registered across 4 zones.

## Decisions Log
- 2026-04-12: Project state reads STATUS.md files directly (no database, no sync). Same source-of-truth pattern as life-os.
- 2026-04-12: Log capture uses subprocess.PIPE instead of DEVNULL — trades DETACHED_PROCESS for log visibility. Processes still run independently via CREATE_NEW_PROCESS_GROUP.
- 2026-04-12: Auto-restart uses 3 retries in a 5-minute window, then disables. Prevents infinite restart loops.
- 2026-04-12: GitHub repo named "project-controller" (user's choice) rather than "dev-manager" (local folder name).
- 2026-04-11: Used Flask + pystray instead of Electron/Tauri — keeps it pure Python, lightweight, consistent with other tools.
- 2026-04-11: Grouped by life-os zones (Personal, Professor, LLC, Services) to match the owner's mental model.
- 2026-04-11: Ollama set as monitor-only (managed: false) since it runs as a Windows service.
- 2026-04-11: Used pythonw + Startup folder batch file for boot-time launch (no console window).
