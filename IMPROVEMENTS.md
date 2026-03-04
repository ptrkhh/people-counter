# People Counter - Plan Audit & Improvements

---

## Issue #1: No Entry vs Exit Direction Detection

The system is designed to "count people walking in" (PLAN.md line 5), but the counting logic counts **every person who appears and disappears** from the camera view, regardless of direction. A person walking *out* of the building is counted identically to someone walking *in*.

**Impact**: The count could be up to 2x the actual entry count if people also exit through the same entrance.

### Proposed Fix
- **Option A**: **Tripwire line** — Define a virtual line across the entrance. Count only when a track's centroid crosses the line in the "inward" direction. This is the standard approach in commercial people counters. Requires knowing the camera orientation (which side is "inside").
  - *Pros*: Accurate directional counting, can count both entries and exits separately, well-established technique
  - *Cons*: Requires user to configure the line position and direction (but can default to horizontal center with a `--line-y` and `--direction` CLI arg)

- **Option B**: **Dual-zone counting** — Divide the frame into an "outside" zone and "inside" zone. Count a person as "entered" only if their track moves from outside zone to inside zone.
  - *Pros*: More robust than a single line (requires sustained movement across zones, less sensitive to jitter)
  - *Cons*: More complex to configure, larger dead zone

- **Option C**: **Accept bidirectional counting** — Rename the metric to "total person appearances" instead of "entries" and document it as a known approximation.
  - *Pros*: Zero implementation cost, simplest approach
  - *Cons*: Doesn't solve the core requirement ("count people walking in")

### Recommended Fix
**Option A** (tripwire line). It's the industry standard, adds ~30 lines of code to `tracker.py`, and can be made optional (default to current behavior when no line is configured). The CSV can gain a `direction` column (`in`/`out`) for richer data. Configuration via `--line-y 0.6 --in-direction down` CLI args.

---

## Issue #2: Occlusion Causes Double-Counting

When a person is temporarily occluded by another person (e.g., two people passing each other at the door), ByteTrack may lose the track. After occlusion ends, the person reappears with a **new track_id** and gets counted again.

The `lost_timeout_seconds = 2.0s` makes this worse — if someone is hidden behind another person for >2 seconds, they're flushed as "left" and then immediately re-detected as a new person.

**Impact**: Overcounting in busy entrances where people frequently occlude each other.

### Proposed Fix
- **Option A**: **Increase `lost_timeout_seconds`** to 3-5 seconds. Gives ByteTrack more time to re-associate tracks after occlusion.
  - *Pros*: Simple config change, no code changes
  - *Cons*: Delays all "left" events, slower real-time count updates

- **Option B**: **Re-identification buffer** — When a new track appears, check if it spatially overlaps with a recently-lost track (within a configurable spatial threshold and time window). If so, merge them into the same logical person.
  - *Pros*: Directly addresses the problem, doesn't delay normal counting
  - *Cons*: Added complexity (~40 lines), needs spatial proximity threshold tuning

- **Option C**: **Use ByteTrack's built-in `track_buffer`** — Ultralytics' ByteTrack has a `track_buffer` parameter (frames to keep lost tracks). Increase it from the default to retain lost tracks longer before ID reassignment.
  - *Pros*: Uses existing infrastructure, single parameter change
  - *Cons*: Frame-count-based (not time-based), behavior varies with FPS

### Recommended Fix
**Option C** combined with **Option A**. First, tune ByteTrack's `track_buffer` to a higher value (e.g., 60-90 frames), which lets the tracker internally handle short occlusions. Then set `lost_timeout_seconds` to 3.0s as a secondary safety net. This requires no new code — just parameter tuning.

---

## Issue #3: No Graceful Shutdown Flush

The plan specifies flushing active tracks on **camera disconnect** (PLAN.md line 80), but does **not** specify the same behavior on **graceful shutdown** (Ctrl+C, SIGTERM, service stop). When the process terminates, all in-progress tracks are silently lost — no CSV row is ever written for people currently visible.

**Impact**: On every restart/shutdown, 0-N people currently being tracked are lost from the count. For a long-running service that gets restarted for updates, this adds up.

### Proposed Fix
- **Option A**: **Flush all active tracks on shutdown** — In the signal handler / `finally` block, iterate all active tracks and write them to CSV with their current duration, same as camera disconnect behavior.
  - *Pros*: Consistent with camera disconnect behavior, minimal code, no data loss
  - *Cons*: Duration for flushed tracks is a lower bound (person may have stayed longer after capture stopped)

- **Option B**: **Write a state file on shutdown** — Serialize active tracks to a JSON file on shutdown. On next startup, reload and continue tracking.
  - *Pros*: Theoretically no data loss, seamless restart
  - *Cons*: Over-engineered — people move between shutdown and startup, tracks won't match

### Recommended Fix
**Option A**. Add active track flushing to the `try/finally` cleanup block alongside camera release and file close. The plan already implements this for camera disconnect — it should be applied uniformly to all shutdown paths. Add a note in the CSV or log that these are "flush-on-shutdown" events.

---

## Issue #4: Camera Reconnect Causes Double-Counting

When the camera disconnects (PLAN.md line 80), all active tracks are flushed as "left" events. When the camera reconnects, people who were still physically present get new track_ids and are counted again.

**Impact**: Every camera glitch (USB hiccup, RTSP timeout) for a busy entrance adds N phantom entries, where N = number of people visible at disconnect time.

### Proposed Fix
- **Option A**: **Suppress counting briefly after reconnect** — After camera reconnect, wait `lost_timeout_seconds` before starting to track/count. This lets ByteTrack establish tracks without immediately counting them as new entries.
  - *Pros*: Simple, prevents most double-counts
  - *Cons*: Misses people who enter during the suppression window

- **Option B**: **Mark reconnect-flush events** — Add a `reason` column to the CSV (`left`, `disconnect_flush`, `shutdown_flush`). Don't change counting behavior, but allow post-processing to reconcile reconnect artifacts.
  - *Pros*: Preserves raw data, enables accurate post-processing, zero counting logic changes
  - *Cons*: Doesn't fix real-time count; requires post-processing

- **Option C**: **Short reconnect grace period** — If the camera reconnects within N seconds, don't flush the old tracks but instead attempt to reconcile them with new detections based on spatial proximity.
  - *Pros*: Most accurate
  - *Cons*: Complex, fragile, adds significant state management

### Recommended Fix
**Option B**. Adding a `reason` column is low-cost (change one parameter in the CSV write call) and makes the data self-documenting. The real-time count will have brief inaccuracies during reconnects, but post-processing can filter or reconcile `disconnect_flush` rows. This also benefits Issue #3 (shutdown flushes get their own reason tag).

---

## Issue #5: Stale Track Memory Growth

The plan describes tracks being created when a person enters and removed when they "leave" (`lost_timeout_seconds`). However, there is no mention of a **maximum track lifetime**. If ByteTrack assigns a persistent track_id to a static object falsely detected as a person (e.g., a coat on a rack, a poster), that track lives in memory forever, never triggering a "left" event.

Over hours or days, accumulated stale tracks consume increasing memory and may degrade tracker performance.

**Impact**: Memory leak in a long-running service. Potential OOM crash after days of continuous operation.

### Proposed Fix
- **Option A**: **Maximum track lifetime** — Forcibly flush any track that has been active for longer than `max_track_lifetime` (e.g., 300 seconds / 5 minutes). No person realistically stays visible at a doorway for 5 minutes.
  - *Pros*: Simple, bounds memory usage, catches stuck false detections
  - *Cons*: Could miscount a person who genuinely stands in the doorway for >5 min (edge case)

- **Option B**: **Periodic stale track audit** — Every 60 seconds (alongside the existing heartbeat), check for tracks older than a threshold and log a warning. Only flush after a longer timeout (e.g., 30 minutes).
  - *Pros*: Less aggressive, lower risk of miscounting
  - *Cons*: Stale tracks persist longer in memory

### Recommended Fix
**Option A** with a configurable `max_track_lifetime` (default 300s). A person standing in a doorway for 5 minutes is extremely unusual. Add it as a config parameter so it can be adjusted. Log a WARNING when a track is force-flushed so operators can tune the threshold.

---

## Issue #6: No Frame Drop Strategy Under Load

The plan is single-threaded: capture frame, detect, track, count, display, repeat. If YOLO26s detection takes longer than the frame interval (e.g., 200ms on slow hardware when frames arrive at 30fps), the frame buffer fills up. OpenCV's `VideoCapture` has a default internal buffer of ~5 frames. Once full, frames queue up and the system processes increasingly stale video — the live view falls behind reality.

**Impact**: On slow hardware, the system silently falls behind real-time. People may have left the frame by the time their "old" frame is processed, causing tracking artifacts.

### Proposed Fix
- **Option A**: **Set buffer size to 1** — `cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)`. This ensures each `cap.read()` returns the most recent frame, dropping intermediate frames.
  - *Pros*: One-line fix, always processes current reality
  - *Cons*: Not all backends honor `CAP_PROP_BUFFERSIZE`; may not work on all platforms

- **Option B**: **Threaded frame grabber** — Dedicated thread calls `cap.grab()` continuously, main thread calls `cap.retrieve()` to get the latest frame. This is the standard OpenCV pattern for processing latest-frame-only.
  - *Pros*: Guaranteed to work across all backends, well-established pattern
  - *Cons*: Adds threading complexity (contradicts single-threaded design)

- **Option C**: **Skip-read pattern** — Before processing, drain the buffer by calling `cap.grab()` N times without `retrieve()`, then read the final frame.
  - *Pros*: No threading, works on all backends
  - *Cons*: Wastes CPU on grab calls, N is somewhat arbitrary

### Recommended Fix
**Option A** as the primary approach (it's a one-liner and works on most USB webcam backends). Add **Option C** as a fallback: if `CAP_PROP_BUFFERSIZE` isn't supported, do a single extra `cap.grab()` before each `cap.read()` to skip one buffered frame. This keeps the system closer to real-time without threading.

---

## Issue #7: Log File Rotation Missing

The plan specifies logging to file (PLAN.md line 155: "Log to file (not just stdout) so native crashes leave a trail") but does not mention log rotation. A long-running service writing INFO-level logs (frame counts, people counts every 60s, all detection events) can produce hundreds of MB of logs over weeks.

**Impact**: Disk space exhaustion on embedded or low-storage deployments.

### Proposed Fix
- **Option A**: **`RotatingFileHandler`** — Use Python's built-in `logging.handlers.RotatingFileHandler` with a max size (e.g., 10 MB) and backup count (e.g., 3 files).
  - *Pros*: Built into Python stdlib, no external dependencies, bounded disk usage
  - *Cons*: Log files may be split mid-event

- **Option B**: **`TimedRotatingFileHandler`** — Rotate logs daily, matching the CSV daily rotation pattern.
  - *Pros*: Aligns with CSV daily rotation, easy to correlate logs with data
  - *Cons*: Doesn't bound file size (a single busy day could produce a large log)

### Recommended Fix
**Option A** (`RotatingFileHandler`). Size-based rotation gives a hard guarantee on disk usage. Default to 10 MB max with 3 backups (40 MB max total). This is important for the deployment scenario (entrance camera, possibly on modest hardware).

---

## Issue #8: No Headless Auto-Detection on Display-less Systems

The plan requires `--headless` to disable GUI (PLAN.md line 137). On headless Linux servers or systems without a display server (common for deployment), forgetting `--headless` causes `cv2.imshow()` to crash with an opaque error.

**Impact**: Confusing crash on first deployment to production/headless hardware.

### Proposed Fix
- **Option A**: **Auto-detect display availability** — Check for `DISPLAY` env var on Linux / display availability on Windows before enabling GUI. If no display is available, auto-switch to headless with a log WARNING.
  - *Pros*: Just works, no user confusion
  - *Cons*: ~5 lines of platform-specific code

- **Option B**: **Catch the imshow exception** — Wrap the first `imshow()` call in a try/except. If it fails, disable GUI and log a warning.
  - *Pros*: Cross-platform, no env var sniffing
  - *Cons*: First-frame latency from exception, slightly hacky

### Recommended Fix
**Option A**. Check for display availability at startup. On Linux, check `os.environ.get('DISPLAY')` or `os.environ.get('WAYLAND_DISPLAY')`. On Windows, displays are almost always available, but wrap in a try/except as a fallback. Print a clear log message: "No display detected, running in headless mode."

---

## Issue #9: CSV Lacks Event Type Column

The plan's CSV format is `timestamp,track_id,duration_seconds`. This doesn't distinguish between:
- Normal "person left the frame" events
- Disconnect-flush events (Issue #4)
- Shutdown-flush events (Issue #3)
- Force-expired stale tracks (Issue #5)

Without this, post-processing cannot filter or reconcile different event types.

### Proposed Fix
- **Option A**: **Add an `event_type` column** — Values: `left`, `disconnect_flush`, `shutdown_flush`, `expired`. CSV becomes: `timestamp,track_id,duration_seconds,event_type`.
  - *Pros*: Self-documenting data, enables accurate post-processing, backward-compatible (new column appended)
  - *Cons*: Slightly wider CSV rows

- **Option B**: **Separate log file for non-normal events** — Keep CSV clean, write flush/expired events to a separate file.
  - *Pros*: Clean primary data
  - *Cons*: Harder to correlate, two files to manage

### Recommended Fix
**Option A**. A single extra column is trivial and makes the data self-documenting. Default value `left` for normal events. This directly supports Issues #3, #4, and #5.

---

## Issue #10: Track ID Non-Uniqueness in CSV

ByteTrack may recycle track IDs (they're sequential integers that reset on tracker reinitialization, camera reconnect, or process restart). The CSV stores `track_id` but there's no guarantee of uniqueness across a day's file. Two different people could have `track_id=1` in the same CSV.

**Impact**: Post-processing that groups by `track_id` will incorrectly merge different people's events.

### Proposed Fix
- **Option A**: **Add a session counter** — Prepend a session identifier (e.g., process start timestamp or monotonic counter) to track IDs, making them globally unique: `session_track_id` = `"S1_42"`.
  - *Pros*: Guaranteed uniqueness, traceable to session
  - *Cons*: More complex ID format

- **Option B**: **Add a `session_id` column** — Keep track_id numeric but add a column identifying the session/run. Composite key `(session_id, track_id)` is unique.
  - *Pros*: Clean separation, track_id stays simple
  - *Cons*: Extra column

- **Option C**: **Document non-uniqueness** — Note in README that `track_id` is for debugging only and is not unique across restarts/reconnects. Since each row has a timestamp, uniqueness can be inferred from `(timestamp, track_id)`.
  - *Pros*: Zero implementation cost
  - *Cons*: Shifts burden to users

### Recommended Fix
**Option C**. The plan already states track_id is "useful for debugging, not for identification." Since each row has a unique timestamp and `track_id` is only meaningful within a continuous tracking session, adding a column for uniqueness is over-engineering. Document the non-uniqueness clearly.

---

## Issue #11: Disk Space Exhaustion Has No Mitigation

The system writes CSV data and log files indefinitely. On embedded or low-storage deployments, the disk will eventually fill. When disk is full, CSV writes fail, and the consecutive-failure counter triggers CRITICAL logging — but logging itself may also fail.

**Impact**: Silent data loss when disk fills up, with no recovery path.

### Proposed Fix
- **Option A**: **Periodic disk space check** — Every 60 seconds (alongside heartbeat), check available disk space. Log WARNING below 500 MB, CRITICAL below 100 MB.
  - *Pros*: Early warning, uses `shutil.disk_usage()` (stdlib)
  - *Cons*: Doesn't fix the problem, just warns

- **Option B**: **Auto-delete old CSV files** — Delete CSV files older than N days (configurable, default 90).
  - *Pros*: Self-managing disk usage
  - *Cons*: Data loss without user consent, dangerous default

- **Option C**: **Option A + documentation** — Warn on low disk space and document recommended disk cleanup in README (e.g., cron job to archive/delete old CSVs).
  - *Pros*: Early warning without risk of auto-deleting user data
  - *Cons*: Requires manual intervention

### Recommended Fix
**Option C**. Add a disk space check to the heartbeat loop (2 lines of code with `shutil.disk_usage()`). Log warnings at thresholds. Document the recommended archival strategy in the README. Auto-deletion is too risky as a default.

---

## Issue #12: No FPS / Performance Metrics Logging

The plan logs "frame count + people count every 60 seconds" (PLAN.md line 154) but not **processing FPS** or detection latency. Without this, operators can't tell if the system is keeping up with real-time video or falling behind.

**Impact**: Silent performance degradation is invisible without FPS metrics.

### Proposed Fix
- **Option A**: **Log effective FPS and mean detection time** — Every 60 seconds, log: `"Processed 847 frames in 60s (14.1 fps), avg detection: 68ms, people count: 23"`.
  - *Pros*: Operators can immediately see if system is falling behind, minimal overhead
  - *Cons*: Slightly more verbose log lines

- **Option B**: **Add `--stats` flag for detailed performance output** — Optional verbose performance stats to a separate file.
  - *Pros*: Clean default logs, detailed stats when needed
  - *Cons*: Extra flag and file to manage

### Recommended Fix
**Option A**. Extend the existing 60-second status log to include FPS and detection latency. The data is already available (frame count / elapsed time = FPS, `time.monotonic()` before/after detection = latency). Zero additional dependencies, ~5 lines of code.

---

## Issue #13: `os.fsync()` Dismissed Too Quickly

The plan dismisses `fsync` (PLAN.md line 211) with "Risk accepted: losing ~1 row during power failure is acceptable." However, `flush()` only moves data from Python's buffer to the OS kernel buffer — it does **not** guarantee data reaches disk. On a hard power cut, the OS write-back cache can hold multiple seconds of flushed-but-not-synced data. This means potentially losing **many** rows, not just one.

**Impact**: On sudden power loss, data loss could be 5-30+ seconds of events, not ~1 row as stated.

### Proposed Fix
- **Option A**: **Add `os.fsync(file.fileno())` after `flush()`** — Forces the OS to write data to disk.
  - *Pros*: True power-loss safety, ~1 line of code, guarantees at-most-1-row loss
  - *Cons*: Performance cost (~1-5ms per sync on HDD, negligible on SSD). At doorway traffic levels (1 event every few seconds), this is imperceptible

- **Option B**: **Periodic fsync** — `fsync` every N seconds instead of every write, balancing safety and performance.
  - *Pros*: Lower I/O overhead on high-traffic scenarios
  - *Cons*: Still a window for multi-row loss

### Recommended Fix
**Option A**. The performance cost of `os.fsync()` is negligible at doorway traffic rates (a few events per minute). One extra line after `flush()` changes the guarantee from "maybe lose multiple rows" to "lose at most 1 row," which matches the plan's stated assumption.

---

## Issue #14: Windows Console Close Event Not Handled

The plan handles `SIGINT` (all platforms), `SIGTERM` (Linux), and `SIGBREAK` (Windows) (PLAN.md line 151). However, on Windows, **closing the console window** (clicking the X button) sends `CTRL_CLOSE_EVENT`, which is **not** `SIGBREAK` or `SIGINT`. Python's `signal` module cannot catch `CTRL_CLOSE_EVENT` — it requires `win32api.SetConsoleCtrlHandler()` or the `ctypes` equivalent.

Without handling this, closing the terminal window kills the process immediately with no cleanup — no track flushing, no file closing.

**Impact**: On Windows, the most natural way to stop the program (closing the window) causes unclean shutdown and data loss.

### Proposed Fix
- **Option A**: **`SetConsoleCtrlHandler` via ctypes** — Register a handler for `CTRL_CLOSE_EVENT` using `ctypes.windll.kernel32.SetConsoleCtrlHandler`. Set a shutdown flag and return `True` to delay termination (Windows gives ~5 seconds for cleanup).
  - *Pros*: Handles the most common Windows shutdown path, no extra dependencies (ctypes is stdlib)
  - *Cons*: Windows-specific code, ~10 lines

- **Option B**: **`atexit` handler** — Register cleanup via `atexit.register()`. Works for some termination scenarios but NOT for `CTRL_CLOSE_EVENT` (process is force-killed after timeout).
  - *Pros*: Cross-platform, simple
  - *Cons*: Unreliable for console close events

### Recommended Fix
**Option A**. Use `ctypes` to register a console control handler on Windows. This is the correct way to handle window-close events and complements the existing `SIGBREAK` handler. Wrap in `if sys.platform == 'win32'` to keep it platform-specific.

---

## Issue #15: RTSP Timeout Configuration Incomplete

The plan mentions `cv2.CAP_PROP_OPEN_TIMEOUT_MSEC` for RTSP (PLAN.md line 152) but doesn't address **read timeouts**. An RTSP stream can connect successfully but then stall mid-stream (network hiccup, camera freeze). Without a read timeout, `cap.read()` blocks indefinitely, freezing the entire application.

**Impact**: The application hangs silently on RTSP stream stalls instead of triggering the reconnect logic.

### Proposed Fix
- **Option A**: **Set `CAP_PROP_READ_TIMEOUT_MSEC`** — Configure OpenCV's read timeout (e.g., 10000ms). After timeout, `cap.read()` returns `False`, triggering the existing reconnect logic.
  - *Pros*: Simple, uses existing OpenCV API, integrates with planned reconnect logic
  - *Cons*: `CAP_PROP_READ_TIMEOUT_MSEC` availability varies by OpenCV build/backend

- **Option B**: **Threaded reader with timeout** — Read frames in a thread, use a `threading.Event` with timeout in the main thread.
  - *Pros*: Guaranteed to work across all backends
  - *Cons*: Adds threading (contradicts single-threaded design)

### Recommended Fix
**Option A**. Set `CAP_PROP_READ_TIMEOUT_MSEC` alongside the existing `CAP_PROP_OPEN_TIMEOUT_MSEC`. Add a fallback: if `cap.read()` hasn't returned a frame in 15 seconds (tracked via `time.monotonic()`), force-trigger the reconnect path. The plan already calls RTSP "future scope," so this is a low-priority but important-when-needed fix.

---
