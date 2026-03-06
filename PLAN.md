# People Counter - Implementation Plan

## Overview

Cross-platform (Windows + Linux) Python application that uses a webcam at a building entrance to count people walking in. Counts are saved to a local file immediately so no data is lost on power failure.

Ships as a "total appearances" counter (entries + exits). Position camera facing a one-way entrance, or interpret count as total appearances.

---

## Tech Stack

| Component | Choice | Reason |
|---|---|---|
| Language | Python 3.10+ | Best CV/ML ecosystem, cross-platform |
| Detection | **YOLO26s** (Ultralytics) | Best CPU speed + accuracy ratio for person detection. NMS-free inference reduces latency. Fallback to YOLO26n on slow hardware |
| Acceleration | **OpenVINO** (optional) | Auto-detected on Intel Iris Xe / Arc iGPUs. ~2-3x faster inference vs CPU-only. Falls back to CPU transparently if unavailable |
| Tracking | ByteTrack (built into Ultralytics) | Tracks person identity across frames so a stationary person is counted only once |
| Camera | OpenCV (`cv2.VideoCapture`) | Cross-platform webcam access |
| Storage | **CSV** (append-mode) | Human-readable, easy to analyze in Excel/scripts, append-friendly, survives power loss with flush |
| GUI | OpenCV `imshow` (optional) | Zero extra dependencies, toggle via `--headless` flag |

### Why CSV over JSON/TXT?

- **JSON**: Not safely appendable (requires valid structure). Power loss mid-write = corrupted file.
- **TXT**: Works but lacks structure for later analysis.
- **CSV**: Append one line per event, flush immediately. Power loss = at most lose 1 line. Easy to open in Excel, parse with pandas, or `grep`.

### Why YOLO26s?

- Current Ultralytics flagship (January 2026), successor to YOLO11
- NMS-free inference — predictions generated directly, lower latency
- DFL removal — simpler inference, broader edge/export support (TFLite, CoreML, OpenVINO, TensorRT, ONNX)
- ~43% faster CPU inference vs YOLO11 (nano variant benchmark)
- ProgLoss + STAL — better small-object detection (useful for distant camera mounts)
- Built-in tracking + object counting solutions
- `pip install ultralytics` — no manual model setup
- YOLO26n available as lighter fallback for slow hardware

### Intel iGPU Acceleration (OpenVINO)

- Intel Iris Xe and Arc iGPUs are supported via OpenVINO, which Ultralytics has built-in export support for
- One-time export: `model.export(format="openvino")` creates an `_openvino_model/` directory alongside the `.pt` file
- Subsequent runs load the exported model directly — no re-export needed
- Auto-detection at startup: check for Intel iGPU, check if `openvino` is installed, check if exported model exists. Log the result clearly:
  - `"Intel Iris Xe detected, OpenVINO available — using GPU-accelerated inference"`
  - `"Intel iGPU detected but openvino not installed — pip install openvino for ~2-3x speedup"`
  - `"No Intel iGPU detected — using CPU inference"`
- `--device` flag allows explicit override: `cpu`, `openvino` (auto is default)
- Falls back to CPU gracefully if OpenVINO export or inference fails at runtime

---

## Architecture

```
Webcam → Frame Capture → YOLO26 Person Detection → ByteTrack Tracker → Counting Logic → CSV Writer
                                                                              ↓
                                                                     GUI Display (optional)
```

### Core Modules

```
people_counter/
├── main.py              # Entry point, CLI argument parsing
├── detector.py          # YOLO26 person detection wrapper
├── tracker.py           # ByteTrack integration + counting logic
├── counter.py           # Orchestrator: capture → detect → track → count
├── storage.py           # CSV writer with immediate flush
├── config.py            # Configuration (thresholds, paths, camera index)
└── requirements.txt     # Dependencies
```

---

## Counting Logic

### How it works

1. Each frame: YOLO26 detects all persons → ByteTrack assigns a persistent `track_id` to each person
2. When a `track_id` is seen for the first time → mark as "entered frame"
3. When a `track_id` has not been seen for `lost_timeout_seconds` (default 3.0s, using `time.monotonic()`) → consider the person as "left the camera"
4. On "left" event → increment count, write a row to CSV with timestamp and `event_type=left`
5. On shutdown/disconnect → flush all active tracks to CSV with appropriate `event_type` (`shutdown_flush` or `disconnect_flush`)
5. A person standing still continuously keeps the same `track_id` → counted only once when they eventually leave

### Handling edge cases

| Scenario | Behavior |
|---|---|
| Person walks in and out twice | Counted as 2 (tracker assigns new ID after leaving) |
| Person stands still all day | Counted as 1 (same track_id persists) |
| Two people enter simultaneously | Counted as 2 (separate track_ids) |
| Power loss | All previously flushed CSV rows are safe |
| Camera disconnect | Logs error, reconnects with capped exponential backoff (5s → 10s → 30s). Flushes all active tracks with `event_type=disconnect_flush` |
| App shutdown (Ctrl+C, SIGTERM) | Flushes all active tracks with `event_type=shutdown_flush` in `try/finally` block |

---

## CSV Format

File: `people_count_YYYY-MM-DD.csv` (one file per day, auto-rotated at midnight)

```csv
timestamp,track_id,duration_seconds,event_type
2026-03-04 09:15:23,1,3.2,left
2026-03-04 09:15:45,2,1.8,left
2026-03-04 09:17:02,3,12.5,disconnect_flush
```

- **timestamp**: When the person left the camera view (or was flushed)
- **track_id**: Internal tracker ID (useful for debugging, not for identification)
- **duration_seconds**: How long the person was visible (helps filter false detections — very short durations likely aren't real people)
- **event_type**: Why the track ended — `left` (normal departure), `disconnect_flush` (camera lost), `shutdown_flush` (app stopped), `expired` (track timed out). Makes data self-documenting and enables post-processing to reconcile reconnect artifacts

Total counts are derived from the event log (e.g., `wc -l` or a script). No separate summary file — a read-modify-write summary would be a corruption vector contradicting the append-only design. A CLI command to generate summaries on demand can be added later.

---

## CLI Interface

```bash
# GUI mode (default) - shows live camera feed with bounding boxes
python main.py

# Headless mode - no GUI, just logs to CSV
python main.py --headless

# Custom camera
python main.py --camera 1              # camera index
python main.py --camera rtsp://...     # RTSP stream

# Use lighter model on slow hardware
python main.py --model yolo26n.pt

# Custom output directory
python main.py --output ./logs

# Set confidence threshold
python main.py --confidence 0.5

# Force CPU inference (skip iGPU auto-detection)
python main.py --device cpu

# Force OpenVINO (error if unavailable)
python main.py --device openvino
```

---

## Configuration Defaults

| Setting | Default | Description |
|---|---|---|
| `camera` | `0` | Webcam index or RTSP URL |
| `model` | `yolo26s.pt` | YOLO model file (auto-downloads on first run) |
| `device` | `auto` | Inference device: `auto` (detect Intel iGPU → OpenVINO, else CPU), `cpu`, `openvino` |
| `confidence` | `0.5` | Minimum detection confidence (validated: 0.0 < x <= 1.0) |
| `lost_timeout_seconds` | `3.0` | Seconds before a person is considered "left" (time-based, not frame-count, so behavior is consistent regardless of FPS). Set to 3.0s (not 2.0s) — primary use case is long-term reports where latency is irrelevant and accuracy matters |
| `output_dir` | `./output` | Directory for CSV files |
| `headless` | `false` | Run without GUI |
| `log_level` | `INFO` | Logging verbosity |

---

## Implementation Steps

### Phase 1: Core Detection + Counting
1. Set up project structure and `requirements.txt`
2. Implement `detector.py` — YOLO26 wrapper, person-only detection
3. Implement `tracker.py` — ByteTrack tracking + "entered/left" logic using `time.monotonic()` for lost timeout
4. Implement `storage.py` — CSV writer with `open(..., 'a')` + `flush()` after every write. Header via `file.tell() == 0` (avoids TOCTOU race). Wrap writes in `try/except` with consecutive-failure counter; log `ERROR` per failure, `CRITICAL` after 10 consecutive failures
5. Implement `counter.py` — orchestrate the pipeline: capture → detect → track → count → save
6. Implement `main.py` — CLI args with `argparse`, launch the counter. Camera arg: try `int()` conversion first (device index), fall back to string (URL/path) — `cv2.VideoCapture("1")` opens a file, not device 1
7. Signal/shutdown handling — `try/finally` around main loop as primary cleanup. `SIGINT` on all platforms; `SIGTERM` on Linux only (`if sys.platform != 'win32'`); `SIGBREAK` on Windows. Context managers for camera + file resources
8. Startup checks — `os.makedirs(output_dir, exist_ok=True)`, verify directory is writable (test write), model file existence check (clear error + download instructions if missing), `cv2.CAP_PROP_OPEN_TIMEOUT_MSEC` for RTSP
9. Input validation — argparse validator enforcing `0.0 < confidence <= 1.0`
10. Periodic status logging — log frame count + people count every 60 seconds
11. Heartbeat file — write `datetime.now().isoformat()` to `heartbeat.txt` every 60s. Log to file (not just stdout) so native crashes leave a trail
12. Process restart awareness — log `WARNING` on startup noting any in-progress tracks from a prior crash are lost
13. AGPL license warning — comment block at top of `main.py` warning that adding a network API triggers AGPL source disclosure
14. Frame drop strategy — set `cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)` to always process the most recent frame. Fallback: single `cap.grab()` before each `cap.read()` if the backend doesn't honor `CAP_PROP_BUFFERSIZE`
15. Headless auto-detection — check display availability at startup instead of requiring `--headless`. Linux: `os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')`. Windows: wrap first `imshow()` in try/except. Log: "No display detected, running in headless mode." `--headless` flag remains as an explicit override
16. Tune occlusion defaults — increase ByteTrack's `track_buffer` to 60-90 frames for better re-identification after brief occlusion
17. FPS / performance metrics — extend the 60-second heartbeat log to include effective FPS (`frame_count / elapsed`) and mean detection latency (`time.monotonic()` around YOLO call)
18. OpenVINO auto-detection — at startup, detect Intel iGPU (`detector.py`). If `--device auto`: check for Intel GPU via `openvino.runtime.Core().available_devices` (look for `"GPU"`), check if `openvino` is importable, auto-export model if `.pt` provided and `_openvino_model/` doesn't exist yet. Log device selection. If `--device openvino`: fail fast if not available. If `--device cpu`: skip detection entirely

### Phase 2: GUI + Directional Counting
18. Add optional live display with bounding boxes, track IDs, and running count overlay
19. Add `--headless` flag to disable GUI (auto-detection in Phase 1; flag remains as explicit override)
21. Windows console close event — `ctypes.windll.kernel32.SetConsoleCtrlHandler` to handle `CTRL_CLOSE_EVENT` (user clicking X on the console window). ~10 lines, Windows-only. Without this, 0-2 in-progress tracks are lost per console close

### Phase 3: Robustness
22. Camera disconnect/reconnect with capped exponential backoff (5s → 10s → 30s). On disconnect, flush all active tracks with `event_type=disconnect_flush` (duration capped at `lost_timeout_seconds`)
23. Daily file rotation (new CSV at midnight)

### Phase 4: Polish
24. Logging with `logging` module
25. Log file rotation — `RotatingFileHandler` (stdlib), 10 MB max, 3 backups (40 MB cap). Log growth is ~35 MB/year at INFO level
26. Config file support (optional `config.yaml` override)
27. README with setup instructions + pre-deployment model download step

---

## Dependencies

```
ultralytics>=8.4.0    # YOLO26 + ByteTrack
opencv-python>=4.8.0  # Camera capture + GUI display
numpy>=1.24.0         # Array operations (pulled by ultralytics)
```

Optional (for Intel iGPU acceleration):
```
openvino>=2024.0      # Intel Iris Xe / Arc GPU inference (~2-3x speedup)
```

No GPU required. Runs on CPU out of the box. Intel iGPU acceleration is auto-detected if `openvino` is installed.

---

## Android (Future)

Not in scope for initial build, but the architecture supports it later:

- The detection + tracking logic is pure Python, portable
- Android deployment options: Kivy, BeeWare, or export YOLO26 to TFLite and build a native Android app (YOLO26's DFL removal makes TFLite export cleaner)
- The CSV storage module works on any filesystem

---

## Known Limitations

- **Midnight file rotation**: At midnight, in-progress tracks may produce ±1-2 person counting drift. A synthetic "flush all tracked people" fix would create double-counting artifacts. This is accepted since midnight traffic at a building entrance is typically zero or near-zero. If precise day boundaries matter, join adjacent day files in post-processing.
- **Single-threaded capture**: Not a problem for USB webcam — people are visible for 1-5+ seconds at a doorway, providing plenty of frames even at low effective FPS. ByteTrack handles missing detections. Revisit only if RTSP stalls are observed in production.
- **No minimum duration filter**: Premature — risks filtering real people (fast walkers, edge-of-frame). YOLO26s at 0.5 confidence rarely false-detects. The `duration_seconds` column supports post-hoc filtering without losing raw data.

---

## Reviewed & Dismissed

Issues considered during planning and intentionally excluded:

| Issue | Rationale |
|---|---|
| CSV fsync on power loss | Risk accepted: losing ~1 row during power failure is acceptable for a building entrance counter |
| YOLO11 vs YOLO26 | YOLO26 chosen — faster CPU inference, NMS-free, better edge export support, same Ultralytics API |
| RTSP stream handling | Phase 1 is USB webcam only; RTSP is future scope |
| Thread safety for GUI | YOLO26s keeps single-threaded GUI responsive |
| Dependency pinning | Loose pinning acceptable for now |
| DST in daily rotation | `time.monotonic()` for durations and `date.today()` for rotation are both DST-safe |
| counter.py responsibilities | 150 LOC orchestrator in a 6-file project is fine |
| Missing `__init__.py` | Flat scripts with `python main.py` work without it |
| Stale track OOM | Not credible at doorway traffic rates; proposed fix degrades accuracy |
| Track ID uniqueness | ByteTrack IDs are unique per session; document-only, no code change needed |
| Disk exhaustion from CSV | ~42 MB/year at high traffic — negligible |
