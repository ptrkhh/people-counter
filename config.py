"""Configuration defaults and constants for the people counter."""

import os

# --- Camera ---
DEFAULT_CAMERA_INDEX = 0
CAMERA_BUFFER_SIZE = 1
CAMERA_OPEN_TIMEOUT_MILLISECONDS = 5000

# --- Model ---
DEFAULT_MODEL_NAME = "yolo26s.pt"
DEFAULT_CONFIDENCE_THRESHOLD = 0.6
YOLO_PERSON_CLASS_ID = 0

# --- Inference Device ---
DEFAULT_DEVICE = "auto"
DEVICE_CPU = "cpu"
DEVICE_OPENVINO = "openvino"
VALID_DEVICES = (DEFAULT_DEVICE, DEVICE_CPU, DEVICE_OPENVINO)

# --- Tracking ---
LOST_TIMEOUT_SECONDS = 3.0
BYTETRACK_TRACK_BUFFER = 90

# --- Storage ---
DEFAULT_OUTPUT_DIRECTORY = os.path.join(".", "output")
CSV_HEADER_FIELDS = ["timestamp", "track_id", "duration_seconds", "event_type"]

# Event types written to CSV
EVENT_TYPE_LEFT = "left"
EVENT_TYPE_DISCONNECT_FLUSH = "disconnect_flush"
EVENT_TYPE_SHUTDOWN_FLUSH = "shutdown_flush"
EVENT_TYPE_EXPIRED = "expired"

# --- Detection Error Handling ---
MAX_CONSECUTIVE_DETECTION_FAILURES = 10

# --- CSV Error Handling ---
MAX_CONSECUTIVE_WRITE_FAILURES = 10

# --- Logging / Heartbeat ---
DEFAULT_LOG_LEVEL = "INFO"
STATUS_LOG_INTERVAL_SECONDS = 60
HEARTBEAT_FILE_NAME = "heartbeat.txt"

# --- Camera Read Failures ---
MAX_CONSECUTIVE_READ_FAILURES = 10

# --- Camera Reconnect ---
RECONNECT_DELAY_INITIAL_SECONDS = 5
RECONNECT_DELAY_MAX_SECONDS = 30

# --- GUI ---
DISPLAY_WINDOW_NAME = "People Counter"
BOUNDING_BOX_COLOR = (0, 255, 0)
BOUNDING_BOX_THICKNESS = 2
TEXT_COLOR = (0, 255, 0)
TEXT_FONT_SCALE = 0.6
TEXT_THICKNESS = 2
OVERLAY_BACKGROUND_COLOR = (0, 0, 0)
OVERLAY_TEXT_COLOR = (255, 255, 255)
OVERLAY_FONT_SCALE = 0.8

# --- Log File Rotation ---
LOG_FILE_NAME = "people_counter.log"
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 3
