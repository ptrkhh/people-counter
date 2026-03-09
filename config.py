"""Internal constants for the people counter."""

# --- Camera ---
CAMERA_BUFFER_SIZE = 1

# --- Model ---
YOLO_PERSON_CLASS_ID = 0

# --- Inference Device ---
DEVICE_CPU = "cpu"
DEVICE_OPENVINO = "openvino"
VALID_DEVICES = ("auto", DEVICE_CPU, DEVICE_OPENVINO)

# --- Storage ---
CSV_HEADER_FIELDS = ["timestamp", "track_id", "duration_seconds", "event_type"]

# Event types written to CSV
EVENT_TYPE_LEFT = "left"
EVENT_TYPE_DISCONNECT_FLUSH = "disconnect_flush"
EVENT_TYPE_SHUTDOWN_FLUSH = "shutdown_flush"
EVENT_TYPE_EXPIRED = "expired"

# --- Logging / Heartbeat ---
HEARTBEAT_FILE_NAME = "heartbeat.txt"
LOG_FILE_NAME = "people_counter.log"

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
