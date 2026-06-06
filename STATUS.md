---
type: status
id: PERS-003
status: active
zone: personal
next_action: Restart Dev Manager and validate live - mode bar shows No-Local, GPU bar lists the Orator render with a working Stop, then run a real VR switch.
next_action_oneline: Restart Dev Manager and validate live - mode bar shows No-Local, GPU bar lists the Orator render with a working Stop, then run a real VR switch.
blockers: none
last_session: 2026-05-29T18:53:00-05:00
last_session_handoff: meta/handoffs/session/2026-05-29-18-53-PERS-003.md
last_modified: 2026-05-29T18:53:00-05:00
related: [PERS-002, PERS-001]
priority: normal
tldr: "Local process manager (Flask :5050 + tray) for life-os dev apps. Now hosts the machine-mode system (normal/no-local/vr) via the C:\\life-os\\meta\\scripts\\machine-mode.ps1 engine. No-local is the durable default: vLLM stopped + disabled at boot, watchdog skips local_llm apps while the flag is set."
---

# Dev Manager — Status

## Recent Changes
- 2026-05-29 (b): Added GPU-hog detection to VR mode. New shared `meta/scripts/gpu-status.ps1` (JSON: VRAM + non-managed processes pinning GPU engines, peak-sampled because renders are bursty; uses WDDM perf counters since nvidia-smi under-reports CUDA-as-3D + NVENC). machine-mode.ps1 VR reports hogs + records `gpuHogs` in state; new `-StopGpuJobs` opt-in (never auto). Dev Manager: `/api/gpu`, `/api/gpu/stop/<pid>` (protected-process guard), live GPU bar with per-process Stop. Policy: warn + offer to stop, never auto-kill (renders/parallel-session work safe). Surfaced by an Orator render pinning the GPU at 100% while VR thought it was free.
- 2026-05-29: Built machine-mode system (normal/no-local/vr) into Dev Manager. New engine C:\life-os\meta\scripts\machine-mode.ps1; vr-mode.ps1 + no-local-mode.ps1 now thin shims (originals archived to meta/scripts/archive/). app.py: /api/mode, /api/mode/<m>, /api/reclaim-vram + segmented mode bar. tray.py: "Mode ▸" submenu + Reclaim GPU VRAM. VR mode redesigned light (stop vLLM + quiet a 4-task pop-up subset; DM/cairn/weight-tracker/Discord stay up). No-local set as durable default: vLLM stopped + disabled at boot, ~21 GB VRAM freed. Owner added $SlaCriticalTasks reconciliation (keeps SIM-DAD-Ticket-Triage enabled in normal/no-local). Spec: docs/superpowers/specs/2026-05-29-machine-modes-design.md. Changes uncommitted.
- 2026-04-14: Fixed Lector config in processes.yaml: port 8501→7860, relative .venv path→absolute path. Kill button was targeting wrong port; shell=True with relative paths resolved to system Python across drives.
- 2026-04-12: Added project state integration — reads STATUS.md from each app's directory, displays project status/next_action/blockers on dashboard cards. Expandable detail panels with full project info.
- 2026-04-12: Added process log capture — stdout/stderr stored in ring buffer (200 lines), viewable per-app in expandable log panel. Clear logs button. /api/logs/<id> endpoint.
- 2026-04-12: Added /api/health endpoint — JSON summary of all apps for external integrations (ntfy, monitoring).

## Decisions Log
- 2026-05-29 (b): GPU-hog handling is warn + offer-to-stop, never auto-kill. Renders are hours of work and may belong to a parallel instance (parallel-instance rule), so VR reports + records non-managed GPU consumers and only stops them with explicit `-StopGpuJobs` / a Dev Manager Stop button (confirm). Detection uses Windows GPU-engine perf counters (peak-sampled), not nvidia-smi, because CUDA shows as the "3D" engine on GeForce/WDDM and nvidia-smi's utilization.gpu under-reports it. Logic lives once in gpu-status.ps1, shared by the engine and Dev Manager. A `$Protected`/`GPU_PROTECTED` list keeps desktop/shell processes unkillable.
- 2026-05-29: Machine modes orchestrate PowerShell (the actuator for WSL/Docker/scheduled-task work) rather than reimplementing in Python — keeps one source of truth. Modes are radio-button (one active). no-local AND vr both write no-local-mode.flag so the existing watchdog/hook/local-stack-check keep working unchanged; vr adds a curated on-screen pop-up task subset. VR deliberately does NOT kill Dev Manager (the emergency vr-mode did) so the dashboard stays the control surface. vLLM is stopped AND disabled on no-local/vr; normal uses start-only (never enable) so no-local survives reboot as the default.
- 2026-04-12: Project state reads STATUS.md files directly (no database, no sync). Same source-of-truth pattern as life-os.
- 2026-04-12: Log capture uses subprocess.PIPE instead of DEVNULL — trades DETACHED_PROCESS for log visibility. Processes still run independently via CREATE_NEW_PROCESS_GROUP.
- 2026-04-12: Auto-restart uses 3 retries in a 5-minute window, then disables. Prevents infinite restart loops.
- 2026-04-12: GitHub repo named "project-controller" (user's choice) rather than "dev-manager" (local folder name).
- 2026-04-11: Used Flask + pystray instead of Electron/Tauri — keeps it pure Python, lightweight, consistent with other tools.
- 2026-04-11: Grouped by life-os zones (Personal, Professor, LLC, Services) to match the owner's mental model.
- 2026-04-11: Ollama set as monitor-only (managed: false) since it runs as a Windows service.
- 2026-04-11: Used pythonw + Startup folder batch file for boot-time launch (no console window).
