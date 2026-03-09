"""Orchestrator: capture frames -> detect persons -> track -> count -> save.

This module ties together the camera, detector, tracker, storage, and
optional GUI display into a single processing loop.
"""

import logging
import os
import sys
import time
from datetime import datetime

import cv2
import numpy as np

from config import (
    BOUNDING_BOX_COLOR,
    BOUNDING_BOX_THICKNESS,
    CAMERA_BUFFER_SIZE,
    DISPLAY_WINDOW_NAME,
    EVENT_TYPE_DISCONNECT_FLUSH,
    EVENT_TYPE_SHUTDOWN_FLUSH,
    HEARTBEAT_FILE_NAME,
    OVERLAY_BACKGROUND_COLOR,
    OVERLAY_FONT_SCALE,
    OVERLAY_TEXT_COLOR,
    TEXT_COLOR,
    TEXT_FONT_SCALE,
    TEXT_THICKNESS,
)
from detector import PersonDetector
from storage import EventStorage
from tracker import PersonTracker

logger = logging.getLogger(__name__)


def parse_camera_source(camera_argument):
    """Convert camera argument to int (device index) or string (URL/path).

    cv2.VideoCapture("1") opens a FILE named "1", not device 1.
    So we must convert numeric strings to int.
    """
    try:
        return int(camera_argument)
    except (ValueError, TypeError):
        return camera_argument


def detect_display_available():
    """Check if a graphical display is available.

    Returns True if a display server is likely available.
    """
    if sys.platform == "win32":
        # Windows almost always has a display when running interactively.
        # We'll catch the actual error on first imshow if not.
        return True

    # Linux: check for X11 or Wayland
    display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    return display is not None


def open_camera(camera_source, camera_open_timeout_milliseconds):
    """Open a camera/video source and configure it for low latency.

    Args:
        camera_source: Integer device index or string URL/path.
        camera_open_timeout_milliseconds: Timeout for opening the camera.

    Returns:
        An opened cv2.VideoCapture, or None if opening failed.
    """
    logger.info("Opening camera: %s", camera_source)

    if isinstance(camera_source, str):
        # Two-step open: set timeout BEFORE the blocking open() call.
        # The one-step VideoCapture(url) constructor calls open() internally,
        # so setting the timeout afterward has no effect on RTSP streams.
        capture = cv2.VideoCapture()
        capture.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, camera_open_timeout_milliseconds)
        capture.open(camera_source)
    else:
        capture = cv2.VideoCapture(camera_source)

    if not capture.isOpened():
        logger.error("Failed to open camera: %s", camera_source)
        return None

    # Process the most recent frame (reduce latency from buffering)
    capture.set(cv2.CAP_PROP_BUFFERSIZE, CAMERA_BUFFER_SIZE)

    logger.info("Camera opened successfully: %s", camera_source)
    return capture


def extract_track_ids_from_results(results):
    """Extract integer track_ids from YOLO tracking results.

    Args:
        results: List of YOLO Results objects.

    Returns:
        List of integer track_ids for detected persons.
    """
    track_ids = []

    if not results or results[0].boxes is None:
        return track_ids

    boxes = results[0].boxes
    if boxes.id is None:
        return track_ids

    for track_id in boxes.id:
        track_ids.append(int(track_id.item()))

    return track_ids


def draw_detections_on_frame(frame, results, total_count, active_track_count):
    """Draw bounding boxes, track IDs, and count overlay on the frame.

    Args:
        frame: The video frame (numpy array, modified in-place).
        results: YOLO Results object.
        total_count: Running total of people counted.
        active_track_count: Number of currently tracked people.
    """
    if results and results[0].boxes is not None:
        boxes = results[0].boxes

        box_coordinates = boxes.xyxy.cpu().numpy() if boxes.xyxy is not None else []
        box_ids = boxes.id.cpu().numpy() if boxes.id is not None else []

        for index in range(len(box_coordinates)):
            x1, y1, x2, y2 = box_coordinates[index].astype(int)
            cv2.rectangle(
                frame,
                (x1, y1),
                (x2, y2),
                BOUNDING_BOX_COLOR,
                BOUNDING_BOX_THICKNESS,
            )

            if index < len(box_ids):
                label = f"ID: {int(box_ids[index])}"
                cv2.putText(
                    frame,
                    label,
                    (x1, y1 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    TEXT_FONT_SCALE,
                    TEXT_COLOR,
                    TEXT_THICKNESS,
                )

    # Draw count overlay in top-left corner
    overlay_text = f"Count: {total_count} | Active: {active_track_count}"
    text_size = cv2.getTextSize(
        overlay_text, cv2.FONT_HERSHEY_SIMPLEX, OVERLAY_FONT_SCALE, TEXT_THICKNESS
    )[0]
    overlay_padding = 10
    cv2.rectangle(
        frame,
        (0, 0),
        (text_size[0] + overlay_padding * 2, text_size[1] + overlay_padding * 2),
        OVERLAY_BACKGROUND_COLOR,
        cv2.FILLED,
    )
    cv2.putText(
        frame,
        overlay_text,
        (overlay_padding, text_size[1] + overlay_padding),
        cv2.FONT_HERSHEY_SIMPLEX,
        OVERLAY_FONT_SCALE,
        OVERLAY_TEXT_COLOR,
        TEXT_THICKNESS,
    )


def write_heartbeat(output_directory):
    """Write current timestamp to heartbeat file."""
    heartbeat_path = os.path.join(output_directory, HEARTBEAT_FILE_NAME)
    try:
        with open(heartbeat_path, "w") as heartbeat_file:
            heartbeat_file.write(datetime.now().isoformat())
    except Exception as error:
        logger.warning("Failed to write heartbeat: %s", error)


class PeopleCounter:
    """Main orchestrator that runs the capture-detect-track-count loop."""

    def __init__(self, camera_source, detector, tracker, storage,
                 headless, output_directory,
                 camera_open_timeout_milliseconds,
                 max_consecutive_read_failures,
                 max_consecutive_detection_failures,
                 reconnect_delay_initial_seconds,
                 reconnect_delay_max_seconds,
                 status_log_interval_seconds):
        self.camera_source = camera_source
        self.detector = detector
        self.tracker = tracker
        self.storage = storage
        self.headless = headless
        self.output_directory = output_directory
        self.camera_open_timeout_milliseconds = camera_open_timeout_milliseconds
        self.max_consecutive_read_failures = max_consecutive_read_failures
        self.max_consecutive_detection_failures = max_consecutive_detection_failures
        self.reconnect_delay_initial_seconds = reconnect_delay_initial_seconds
        self.reconnect_delay_max_seconds = reconnect_delay_max_seconds
        self.status_log_interval_seconds = status_log_interval_seconds

        self.is_running = False
        self.capture = None
        self.frame_count = 0
        self.last_status_log_time = 0.0
        self.consecutive_detection_failures = 0
        self.display_available = not headless and detect_display_available()
        self.display_failed = False

        if headless:
            logger.info("Running in headless mode (no GUI)")
        elif not self.display_available:
            logger.info("No display detected, running in headless mode")

    def request_stop(self):
        """Signal the main loop to stop after the current frame."""
        self.is_running = False

    def run(self):
        """Run the main processing loop.

        Handles camera disconnects with exponential backoff reconnection.
        Flushes all active tracks on shutdown.
        """
        logger.warning(
            "Starting people counter. Note: any in-progress tracks "
            "from a prior crash are not recoverable."
        )

        self.capture = open_camera(
            self.camera_source, self.camera_open_timeout_milliseconds
        )
        if self.capture is None:
            raise RuntimeError(
                f"Cannot open camera: {self.camera_source}. "
                "Check the device index or URL."
            )

        self.is_running = True
        self.last_status_log_time = time.monotonic()

        try:
            self._process_frames()
        finally:
            self._shutdown()

    def _process_frames(self):
        """Main frame processing loop with reconnection logic."""
        reconnect_delay = self.reconnect_delay_initial_seconds
        consecutive_read_failures = 0

        while self.is_running:
            # Discard buffered frame, read the freshest one
            self.capture.grab()
            success, frame = self.capture.read()

            if not success:
                consecutive_read_failures += 1
                if consecutive_read_failures >= self.max_consecutive_read_failures:
                    logger.error(
                        "Camera read failed %d consecutive times — attempting reconnect",
                        consecutive_read_failures,
                    )
                    reconnect_delay = self._handle_disconnect(reconnect_delay)
                    consecutive_read_failures = 0
                    if self.capture is None:
                        break
                continue

            consecutive_read_failures = 0
            # Reset reconnect delay on successful read
            reconnect_delay = self.reconnect_delay_initial_seconds

            self._process_single_frame(frame)

            if self._should_show_gui():
                should_quit = self._update_display(frame)
                if should_quit:
                    break

    def _process_single_frame(self, frame):
        """Run detection, tracking, and storage for one frame."""
        try:
            results = self.detector.detect_persons(frame)
        except Exception as error:
            self.consecutive_detection_failures += 1
            if self.consecutive_detection_failures >= self.max_consecutive_detection_failures:
                logger.critical(
                    "Detection has failed %d consecutive times: %s",
                    self.consecutive_detection_failures,
                    error,
                )
            else:
                logger.warning("Detection failed, skipping frame: %s", error)
            return

        self.consecutive_detection_failures = 0
        track_ids = extract_track_ids_from_results(results)
        departed_events = self.tracker.update(track_ids)

        if departed_events:
            self.storage.write_events(departed_events)

        self.frame_count += 1
        self._maybe_log_status()

        # Store results for GUI drawing
        self._last_results = results

    def _should_show_gui(self):
        """Return True if the GUI display should be updated."""
        return self.display_available and not self.display_failed

    def _update_display(self, frame):
        """Draw detections on frame and show in window.

        Returns True if the user pressed 'q' to quit.
        """
        try:
            results = getattr(self, "_last_results", None)
            draw_detections_on_frame(
                frame,
                results,
                self.tracker.total_count,
                self.tracker.get_active_track_count(),
            )
            cv2.imshow(DISPLAY_WINDOW_NAME, frame)

            key_pressed = cv2.waitKey(1) & 0xFF
            if key_pressed == ord("q"):
                logger.info("Quit key pressed")
                return True
        except Exception as error:
            logger.warning("Display failed, switching to headless: %s", error)
            self.display_failed = True

        return False

    def _handle_disconnect(self, current_delay):
        """Handle camera disconnect: flush tracks and attempt reconnection.

        Updates self.capture in place. Returns the next reconnect delay.
        Sets self.capture to None if stopped before reconnecting.
        """
        # Flush active tracks since we don't know when they actually left
        flushed_events = self.tracker.flush_all_tracks(EVENT_TYPE_DISCONNECT_FLUSH)
        if flushed_events:
            self.storage.write_events(flushed_events)

        self.capture.release()
        self.capture = None

        while self.is_running:
            logger.info(
                "Reconnecting to camera in %d seconds...", current_delay
            )

            # Sleep in short intervals so shutdown signals are handled promptly.
            # Worst-case shutdown delay is 0.5s instead of up to 30s.
            SLEEP_CHECK_INTERVAL_SECONDS = 0.5
            for _ in range(int(current_delay / SLEEP_CHECK_INTERVAL_SECONDS)):
                if not self.is_running:
                    return current_delay
                time.sleep(SLEEP_CHECK_INTERVAL_SECONDS)

            if not self.is_running:
                return current_delay

            self.capture = open_camera(
                self.camera_source, self.camera_open_timeout_milliseconds
            )
            if self.capture is not None:
                logger.info("Camera reconnected successfully")
                return self.reconnect_delay_initial_seconds

            # Exponential backoff with cap
            current_delay = min(current_delay * 2, self.reconnect_delay_max_seconds)

        return current_delay

    def _maybe_log_status(self):
        """Log periodic status (FPS, count, heartbeat) every interval."""
        current_time = time.monotonic()
        elapsed_since_last_log = current_time - self.last_status_log_time

        if elapsed_since_last_log < self.status_log_interval_seconds:
            return

        effective_fps = self.frame_count / elapsed_since_last_log
        detection_latency = self.detector.last_detection_duration_seconds

        logger.info(
            "Status: frames=%d, total_count=%d, active_tracks=%d, "
            "fps=%.1f, detection_latency=%.3fs",
            self.frame_count,
            self.tracker.total_count,
            self.tracker.get_active_track_count(),
            effective_fps,
            detection_latency,
        )

        write_heartbeat(self.output_directory)

        self.frame_count = 0
        self.last_status_log_time = current_time

    def _shutdown(self):
        """Clean up on shutdown: flush tracks, release camera, close windows."""
        logger.info("Shutting down...")

        # Flush all remaining active tracks
        flushed_events = self.tracker.flush_all_tracks(EVENT_TYPE_SHUTDOWN_FLUSH)
        if flushed_events:
            self.storage.write_events(flushed_events)
            logger.info("Flushed %d active tracks on shutdown", len(flushed_events))

        self.storage.close()

        if self.capture is not None:
            self.capture.release()
            self.capture = None

        if self._should_show_gui():
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

        logger.info(
            "Shutdown complete. Total people counted: %d",
            self.tracker.total_count,
        )
