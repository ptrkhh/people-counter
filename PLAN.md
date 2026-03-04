# People Counter - Implementation Plan

## Overview

Cross-platform (Windows + Linux) Python application that uses a webcam at a building entrance to count people walking in. Counts are saved to a local file immediately so no data is lost on power failure.

---

## Tech Stack

| Component | Choice | Reason |
|---|---|---|
| Language | Python 3.10+ | Best CV/ML ecosystem, cross-platform |
| Detection | **YOLO26s** (Ultralytics) | Best CPU speed + accuracy ratio for person detection. NMS-free inference reduces latency. Fallback to YOLO26n on slow hardware |
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
3. When a `track_id` has not been seen for `lost_timeout_seconds` (default 2.0s, using `time.monotonic()`) → consider the person as "left the camera"
4. On "left" event → increment count, write a row to CSV with timestamp
5. A person standing still continuously keeps the same `track_id` → counted only once when they eventually leave

### Handling edge cases

| Scenario | Behavior |
|---|---|
| Person walks in and out twice | Counted as 2 (tracker assigns new ID after leaving) |
| Person stands still all day | Counted as 1 (same track_id persists) |
| Two people enter simultaneously | Counted as 2 (separate track_ids) |
| Power loss | All previously flushed CSV rows are safe |
| Camera disconnect | Logs error, reconnects with capped exponential backoff (5s → 10s → 30s). Flushes all active tracks as "left" events |

---

## CSV Format

File: `people_count_YYYY-MM-DD.csv` (one file per day, auto-rotated at midnight)

```csv
timestamp,track_id,duration_seconds
2026-03-04 09:15:23,1,3.2
2026-03-04 09:15:45,2,1.8
2026-03-04 09:17:02,3,12.5
```

- **timestamp**: When the person left the camera view
- **track_id**: Internal tracker ID (useful for debugging, not for identification)
- **duration_seconds**: How long the person was visible (helps filter false detections — very short durations likely aren't real people)

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
```

---

## Configuration Defaults

| Setting | Default | Description |
|---|---|---|
| `camera` | `0` | Webcam index or RTSP URL |
| `model` | `yolo26s.pt` | YOLO model file (auto-downloads on first run) |
| `confidence` | `0.5` | Minimum detection confidence (validated: 0.0 < x <= 1.0) |
| `lost_timeout_seconds` | `2.0` | Seconds before a person is considered "left" (time-based, not frame-count, so behavior is consistent regardless of FPS) |
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

### Phase 2: GUI Mode
14. Add optional live display with bounding boxes, track IDs, and running count overlay
15. Add `--headless` flag to disable GUI

### Phase 3: Robustness
16. Camera disconnect/reconnect with capped exponential backoff (5s → 10s → 30s). On disconnect, flush all active tracks as "left" events (duration capped at `lost_timeout_seconds`)
17. Daily file rotation (new CSV at midnight)

### Phase 4: Polish
18. Logging with `logging` module
19. Config file support (optional `config.yaml` override)
20. README with setup instructions + pre-deployment model download step

---

## Dependencies

```
ultralytics>=8.4.0    # YOLO26 + ByteTrack
opencv-python>=4.8.0  # Camera capture + GUI display
numpy>=1.24.0         # Array operations (pulled by ultralytics)
```

No GPU required. Runs on CPU out of the box.

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
