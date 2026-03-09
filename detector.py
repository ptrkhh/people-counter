"""YOLO26 person detection wrapper with OpenVINO auto-detection."""

import logging
import os
import shutil
import sys
import tempfile
import time

from ultralytics import YOLO

from config import (
    DEVICE_CPU,
    DEVICE_OPENVINO,
    YOLO_PERSON_CLASS_ID,
)

logger = logging.getLogger(__name__)


def detect_intel_gpu_available():
    """Check if an Intel iGPU is available via OpenVINO.

    Returns True if openvino is installed AND a GPU device is found.
    """
    try:
        from openvino import Core

        available_devices = Core().available_devices
        logger.info("OpenVINO available devices: %s", available_devices)
        for d in available_devices:
            if d.startswith("GPU"):
                return d
        return None
    except ImportError:
        return None
    except Exception as error:
        logger.warning("OpenVINO device check failed: %s", error)
        return None


def is_openvino_installed():
    """Check if the openvino package is importable."""
    try:
        import openvino  # noqa: F401

        return True
    except ImportError:
        return False


def get_openvino_model_path(model_path):
    """Return the path to the OpenVINO exported model directory.

    For 'yolo26s.pt', the exported directory is 'yolo26s_openvino_model/'.
    """
    base_name = os.path.splitext(model_path)[0]
    return base_name + "_openvino_model"


def export_model_to_openvino(model):
    """Export a YOLO .pt model to OpenVINO format.

    Returns the path to the exported model directory, or None on failure.
    """
    try:
        if getattr(model.model, "end2end", False):
            logger.info("Disabling end2end for OpenVINO export (NMS handled in Python)")
            model.model.end2end = False
        logger.info("Exporting model to OpenVINO format (one-time operation)...")
        exported_path = model.export(format="openvino")
        logger.info("OpenVINO export complete: %s", exported_path)
        return exported_path
    except Exception as error:
        logger.warning("OpenVINO export failed, falling back to CPU: %s", error)
        return None


def _resolve_model_path(model_name):
    """Resolve model path, checking PyInstaller bundle directory if frozen."""
    if os.path.isfile(model_name):
        return model_name
    if getattr(sys, "frozen", False):
        bundled_path = os.path.join(sys._MEIPASS, model_name)
        if os.path.isfile(bundled_path):
            logger.info("Using bundled model: %s", bundled_path)
            return bundled_path
    return model_name


def resolve_device_and_model(model_name, device_preference):
    """Determine the inference device and load the appropriate model.

    Args:
        model_name: Name or path to the YOLO model file (e.g. 'yolo26s.pt').
        device_preference: One of 'auto', 'cpu', or 'openvino'.

    Returns:
        A loaded YOLO model ready for inference.
    """
    model_path = _resolve_model_path(model_name)

    if device_preference == DEVICE_CPU:
        logger.info("Device set to CPU — skipping iGPU detection")
        return YOLO(model_path)

    if device_preference == DEVICE_OPENVINO:
        return _load_openvino_model_or_fail(model_path)

    # device_preference == "auto"
    return _auto_detect_and_load(model_path)


def _load_openvino_model_or_fail(model_name):
    """Load model with OpenVINO. Raise if OpenVINO is unavailable."""
    if not is_openvino_installed():
        raise RuntimeError(
            "Device set to 'openvino' but openvino is not installed. "
            "Install it with: pip install openvino"
        )

    gpu_device = detect_intel_gpu_available()
    if not gpu_device:
        logger.warning("Device set to 'openvino' but no Intel GPU detected. OpenVINO will use CPU fallback.")
    return _load_or_export_openvino(model_name, gpu_device)


def _auto_detect_and_load(model_name):
    """Auto-detect Intel iGPU and load the best available model."""
    intel_gpu_found = detect_intel_gpu_available()
    openvino_installed = is_openvino_installed()

    if intel_gpu_found and openvino_installed:
        logger.info(
            "Intel iGPU detected, OpenVINO available — "
            "using GPU-accelerated inference"
        )
        model = _load_or_export_openvino(model_name, intel_gpu_found)
        if model is not None:
            return model
        logger.warning("OpenVINO load failed, falling back to CPU")
        return YOLO(model_name)

    if intel_gpu_found and not openvino_installed:
        logger.info(
            "Intel iGPU detected but openvino not installed — "
            "pip install openvino for ~2-3x speedup"
        )

    if not intel_gpu_found:
        logger.info("No Intel iGPU detected — using CPU inference")

    return YOLO(model_name)


def _load_or_export_openvino(model_name, gpu_device=None):
    """Load existing OpenVINO model or export from .pt file.

    Returns the loaded model, or None if export/load fails.
    """
    openvino_path = get_openvino_model_path(model_name)

    if os.path.isdir(openvino_path):
        if any(f.endswith(".xml") for f in os.listdir(openvino_path)):
            logger.info("Loading existing OpenVINO model from: %s", openvino_path)
            try:
                model = YOLO(openvino_path)
                if gpu_device:
                    model.overrides["device"] = f"intel:{gpu_device}"
                return model
            except Exception as error:
                logger.warning("Failed to load OpenVINO model: %s", error)
                return None
        else:
            logger.warning("OpenVINO dir exists but has no .xml file, re-exporting: %s", openvino_path)
            shutil.rmtree(openvino_path)

    # Need to export from .pt first
    base_model = YOLO(model_name)
    exported_path = export_model_to_openvino(base_model)
    if exported_path is None:
        return None

    try:
        model = YOLO(exported_path)
        if gpu_device:
            model.overrides["device"] = f"intel:{gpu_device}"
        return model
    except Exception as error:
        logger.warning("Failed to load exported OpenVINO model: %s", error)
        return None


class PersonDetector:
    """Wraps a YOLO model to detect only people in video frames."""

    def __init__(self, model_name, confidence_threshold, device,
                 track_high_thresh, track_low_thresh, new_track_thresh,
                 track_buffer, match_thresh, fuse_score):
        self.confidence_threshold = confidence_threshold
        self.model = resolve_device_and_model(model_name, device)
        self.last_detection_duration_seconds = 0.0
        self._tracker_config_path = self._write_tracker_config(
            track_high_thresh, track_low_thresh, new_track_thresh,
            track_buffer, match_thresh, fuse_score,
        )

    def _write_tracker_config(self, track_high_thresh, track_low_thresh,
                              new_track_thresh, track_buffer, match_thresh,
                              fuse_score):
        """Write a ByteTrack YAML config file with the given parameters.

        Returns the path to the written config file.
        """
        config_content = (
            f"tracker_type: bytetrack\n"
            f"track_high_thresh: {track_high_thresh}\n"
            f"track_low_thresh: {track_low_thresh}\n"
            f"new_track_thresh: {new_track_thresh}\n"
            f"track_buffer: {track_buffer}\n"
            f"match_thresh: {match_thresh}\n"
            f"fuse_score: {str(fuse_score).lower()}\n"
        )

        config_path = os.path.join(
            tempfile.gettempdir(), "people_counter_bytetrack.yaml"
        )
        with open(config_path, "w") as config_file:
            config_file.write(config_content)

        logger.debug("Tracker config written to: %s", config_path)
        return config_path

    def detect_persons(self, frame):
        """Run person detection on a single video frame.

        Args:
            frame: A numpy array (BGR image from OpenCV).

        Returns:
            YOLO Results object filtered to person-class detections only,
            with tracking enabled (ByteTrack).
        """
        start_time = time.monotonic()

        results = self.model.track(
            source=frame,
            classes=[YOLO_PERSON_CLASS_ID],
            conf=self.confidence_threshold,
            persist=True,
            verbose=False,
            tracker=self._tracker_config_path,
        )

        end_time = time.monotonic()
        self.last_detection_duration_seconds = end_time - start_time

        return results
