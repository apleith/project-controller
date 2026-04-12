# Dev Manager — Status

id: PERS-003
status: active
next_action: Test tray app stability over multiple boot cycles and add process log viewer to dashboard.
blockers: none
last_session: 2026-04-11T20:50:00
last_session_handoff: meta/handoffs/session/2026-04-11-20-50-PERS-NEW.md

## Recent Changes
- 2026-04-11: Built from scratch — Flask dashboard (:5050), system tray app (pystray), process registry (YAML), start/stop/kill controls, Windows startup integration. 8 apps registered across 4 zones.

## Decisions Log
- 2026-04-11: Used Flask + pystray instead of Electron/Tauri — keeps it pure Python, lightweight, consistent with other tools.
- 2026-04-11: Grouped by life-os zones (Personal, Professor, LLC, Services) to match the owner's mental model.
- 2026-04-11: Ollama set as monitor-only (managed: false) since it runs as a Windows service.
- 2026-04-11: Used pythonw + Startup folder batch file for boot-time launch (no console window).
