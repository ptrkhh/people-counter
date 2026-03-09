# AGPL License Notice:
# This application uses Ultralytics YOLO which is licensed under AGPL-3.0.
# If you add a network API (e.g. REST, WebSocket) that exposes this
# application's functionality, AGPL requires you to make the complete
# source code available to users who interact with it over the network.

"""Entry point for the people counter application.

Usage:
    python main.py                    # GUI mode, default webcam
    python main.py --headless         # No GUI
    python main.py --camera 1         # Different camera index
    python main.py --camera rtsp://.. # RTSP stream
    python main.py --model yolo26n.pt # Lighter model
    python main.py --device cpu       # Force CPU inference
"""

import argparse
import json
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler

from config import (
    LOG_FILE_NAME,
    VALID_DEVICES,
)
from counter import PeopleCounter, parse_camera_source
from detector import PersonDetector
from storage import EventStorage
from tracker import PersonTracker

CONFIG_FILENAME = "people-counter-config.json"

REQUIRED_CONFIG_KEYS = [
    "camera",
    "model",
    "device",
    "confidence",
    "output",
    "headless",
    "log_level",
    "lost_timeout",
    "track_buffer",
    "track_high_thresh",
    "track_low_thresh",
    "new_track_thresh",
    "match_thresh",
    "fuse_score",
    "status_log_interval_seconds",
    "reconnect_delay_initial_seconds",
    "reconnect_delay_max_seconds",
    "max_consecutive_read_failures",
    "max_consecutive_detection_failures",
    "max_consecutive_write_failures",
    "camera_open_timeout_milliseconds",
    "log_max_bytes",
    "log_backup_count",
]

logger = logging.getLogger("people_counter")


def get_base_path():
    """Return the directory where the executable (or script) lives."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_config_file():
    """Load and validate people-counter-config.json.

    The config file must exist next to the exe/script and contain all
    required keys.  Prints an error and exits if validation fails.
    """
    config_path = os.path.join(get_base_path(), CONFIG_FILENAME)
    if not os.path.isfile(config_path):
        print(f"ERROR: Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    missing = [k for k in REQUIRED_CONFIG_KEYS if k not in config]
    if missing:
        print(
            f"ERROR: Missing required config key(s) in {CONFIG_FILENAME}: "
            f"{', '.join(missing)}"
        )
        sys.exit(1)

    # Validate device
    if config["device"] not in VALID_DEVICES:
        print(
            f"ERROR: Invalid device '{config['device']}' in {CONFIG_FILENAME}. "
            f"Valid options: {', '.join(VALID_DEVICES)}"
        )
        sys.exit(1)

    # Validate confidence
    try:
        conf = float(config["confidence"])
    except (TypeError, ValueError):
        print(
            f"ERROR: Invalid confidence value in {CONFIG_FILENAME}: "
            f"{config['confidence']}"
        )
        sys.exit(1)
    if not (0.0 < conf <= 1.0):
        print(
            f"ERROR: confidence must be between 0.0 (exclusive) and 1.0 "
            f"(inclusive), got {conf}"
        )
        sys.exit(1)

    # Validate log_level
    valid_log_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    if config["log_level"] not in valid_log_levels:
        print(
            f"ERROR: Invalid log_level '{config['log_level']}' in "
            f"{CONFIG_FILENAME}. Valid options: {', '.join(valid_log_levels)}"
        )
        sys.exit(1)

    return config


def validate_confidence(value):
    """Argparse type validator: confidence must be in (0.0, 1.0]."""
    try:
        float_value = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid confidence value: {value}")

    if not (0.0 < float_value <= 1.0):
        raise argparse.ArgumentTypeError(
            f"Confidence must be between 0.0 (exclusive) and 1.0 (inclusive), "
            f"got {float_value}"
        )
    return float_value


def build_argument_parser():
    """Create and return the CLI argument parser.

    All defaults come from the JSON config file via set_defaults().
    CLI arguments override config file values.
    """
    parser = argparse.ArgumentParser(
        description="People counter using YOLO26 + ByteTrack. "
        "Counts people passing through a camera view and logs events to CSV."
    )

    parser.add_argument(
        "--camera",
        help="Camera device index (integer) or RTSP/video URL.",
    )
    parser.add_argument(
        "--model",
        help="YOLO model file name.",
    )
    parser.add_argument(
        "--device",
        choices=VALID_DEVICES,
        help="Inference device: 'auto' (detect Intel iGPU), 'cpu', or 'openvino'.",
    )
    parser.add_argument(
        "--confidence",
        type=validate_confidence,
        help="Minimum detection confidence (0.0 < x <= 1.0).",
    )
    parser.add_argument(
        "--output",
        help="Output directory for CSV files.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without GUI display.",
    )
    parser.add_argument(
        "--lost-timeout",
        type=float,
        help="Seconds before a disappeared person's ID is forgotten.",
    )
    parser.add_argument(
        "--track-buffer",
        type=int,
        help="Frames ByteTrack remembers a lost track before assigning a new ID.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity.",
    )

    return parser


def setup_logging(log_level, output_directory, log_max_bytes, log_backup_count):
    """Configure logging to both console and rotating file."""
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level))

    log_format = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_format)
    root_logger.addHandler(console_handler)

    # Rotating file handler
    log_file_path = os.path.join(output_directory, LOG_FILE_NAME)
    try:
        file_handler = RotatingFileHandler(
            log_file_path,
            maxBytes=log_max_bytes,
            backupCount=log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(log_format)
        root_logger.addHandler(file_handler)
    except Exception as error:
        # Log to console only if file logging fails
        root_logger.warning("Could not set up file logging: %s", error)


def register_signal_handlers(people_counter):
    """Register OS signal handlers for graceful shutdown.

    SIGINT: All platforms (Ctrl+C)
    SIGTERM: Linux only
    SIGBREAK: Windows only (Ctrl+Break)
    Also handles Windows console close event via SetConsoleCtrlHandler.
    """

    def handle_shutdown_signal(signal_number, stack_frame):
        people_counter.request_stop()

    signal.signal(signal.SIGINT, handle_shutdown_signal)

    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, handle_shutdown_signal)
    else:
        # Windows: handle Ctrl+Break
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, handle_shutdown_signal)

        # Windows: handle console close event (user clicking X on console)
        try:
            import ctypes

            CTRL_CLOSE_EVENT = 2

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)
            def console_ctrl_handler(event_type):
                if event_type == CTRL_CLOSE_EVENT:
                    logger.info("Console close event detected, requesting shutdown...")
                    people_counter.request_stop()
                    return True
                return False

            # Store reference to prevent garbage collection — the only other
            # reference is held by the Windows kernel via SetConsoleCtrlHandler,
            # which Python's GC cannot see.
            people_counter._console_ctrl_handler = console_ctrl_handler
            ctypes.windll.kernel32.SetConsoleCtrlHandler(console_ctrl_handler, True)
        except Exception as error:
            logger.debug("Could not set Windows console handler: %s", error)


def main():
    """Parse arguments and run the people counter.

    Settings are resolved in order of precedence (highest first):
    1. CLI arguments
    2. people-counter-config.json (next to the exe/script)

    The config file is mandatory and must contain all required keys.
    """
    config = load_config_file()

    parser = build_argument_parser()

    # Apply config file values as defaults (CLI args still override)
    parser.set_defaults(
        camera=str(config["camera"]),
        model=config["model"],
        device=config["device"],
        confidence=float(config["confidence"]),
        output=config["output"],
        headless=config["headless"],
        log_level=config["log_level"],
        lost_timeout=float(config["lost_timeout"]),
        track_buffer=int(config["track_buffer"]),
    )

    args = parser.parse_args()

    # Create output directory and set up logging
    os.makedirs(args.output, exist_ok=True)
    setup_logging(
        args.log_level, args.output,
        int(config["log_max_bytes"]), int(config["log_backup_count"]),
    )

    logger.info("People Counter starting")
    logger.info(
        "Config: camera=%s, model=%s, device=%s, confidence=%s, output=%s, "
        "headless=%s, lost_timeout=%s, track_buffer=%s",
        args.camera,
        args.model,
        args.device,
        args.confidence,
        args.output,
        args.headless,
        args.lost_timeout,
        args.track_buffer,
    )

    # Initialize components
    camera_source = parse_camera_source(args.camera)

    storage = EventStorage(
        output_directory=args.output,
        max_consecutive_write_failures=int(
            config["max_consecutive_write_failures"]
        ),
    )
    storage.ensure_output_directory_exists()

    detector = PersonDetector(
        model_name=args.model,
        confidence_threshold=args.confidence,
        device=args.device,
        track_high_thresh=float(config["track_high_thresh"]),
        track_low_thresh=float(config["track_low_thresh"]),
        new_track_thresh=float(config["new_track_thresh"]),
        track_buffer=args.track_buffer,
        match_thresh=float(config["match_thresh"]),
        fuse_score=config["fuse_score"],
    )

    tracker = PersonTracker(lost_timeout_seconds=args.lost_timeout)

    people_counter = PeopleCounter(
        camera_source=camera_source,
        detector=detector,
        tracker=tracker,
        storage=storage,
        headless=args.headless,
        output_directory=args.output,
        camera_open_timeout_milliseconds=int(
            config["camera_open_timeout_milliseconds"]
        ),
        max_consecutive_read_failures=int(
            config["max_consecutive_read_failures"]
        ),
        max_consecutive_detection_failures=int(
            config["max_consecutive_detection_failures"]
        ),
        reconnect_delay_initial_seconds=int(
            config["reconnect_delay_initial_seconds"]
        ),
        reconnect_delay_max_seconds=int(
            config["reconnect_delay_max_seconds"]
        ),
        status_log_interval_seconds=int(
            config["status_log_interval_seconds"]
        ),
    )

    register_signal_handlers(people_counter)

    try:
        people_counter.run()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")
    except Exception as error:
        logger.critical("Fatal error: %s", error, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
