"""CSV writer with immediate flush for power-loss safety.

Writes one row per person-departure event. File is rotated daily
(new file at midnight). Uses append mode so partial writes survive
power failures.
"""

import csv
import io
import logging
import os
from datetime import date, datetime

from config import (
    CSV_HEADER_FIELDS,
    DEFAULT_OUTPUT_DIRECTORY,
    MAX_CONSECUTIVE_WRITE_FAILURES,
)

logger = logging.getLogger(__name__)


class EventStorage:
    """Appends person count events to daily CSV files.

    Each day gets a new file: people_count_YYYY-MM-DD.csv
    Rows are flushed immediately after writing.
    """

    def __init__(self, output_directory=DEFAULT_OUTPUT_DIRECTORY):
        self.output_directory = output_directory
        self.current_date = None
        self.file_handle = None
        self.csv_writer = None
        self.consecutive_write_failures = 0

    def ensure_output_directory_exists(self):
        """Create the output directory if it doesn't exist.

        Also verifies the directory is writable by doing a test write.
        Raises OSError if the directory cannot be created or written to.
        """
        os.makedirs(self.output_directory, exist_ok=True)

        test_file_path = os.path.join(self.output_directory, ".write_test")
        try:
            with open(test_file_path, "w") as test_file:
                test_file.write("test")
            os.remove(test_file_path)
        except OSError as error:
            raise OSError(
                f"Output directory is not writable: {self.output_directory}"
            ) from error

    def get_csv_file_path(self, for_date):
        """Return the CSV file path for a given date."""
        file_name = f"people_count_{for_date.isoformat()}.csv"
        return os.path.join(self.output_directory, file_name)

    def _open_file_for_date(self, today):
        """Open (or rotate to) the CSV file for the given date."""
        if self.file_handle is not None:
            self.file_handle.close()
            self.file_handle = None
            self.csv_writer = None

        file_path = self.get_csv_file_path(today)

        self.file_handle = open(file_path, mode="a", newline="", encoding="utf-8")

        # Write header if the file is empty. Using file.tell() == 0 after
        # opening in append mode avoids a TOCTOU race with exists()/getsize().
        if self.file_handle.tell() == 0:
            self.csv_writer = csv.writer(self.file_handle)
            self.csv_writer.writerow(CSV_HEADER_FIELDS)
            self.file_handle.flush()
        else:
            self.csv_writer = csv.writer(self.file_handle)

        self.current_date = today
        logger.info("CSV file opened: %s", file_path)

    def _ensure_correct_file(self):
        """Rotate to a new file if the date has changed."""
        today = date.today()
        if self.current_date != today or self.file_handle is None:
            self._open_file_for_date(today)

    def write_event(self, track_id, duration_seconds, event_type):
        """Write a single person-departure event to the CSV file.

        Args:
            track_id: The ByteTrack track ID.
            duration_seconds: How long the person was visible (float).
            event_type: Why the track ended ('left', 'shutdown_flush', etc.).
        """
        try:
            self._ensure_correct_file()
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rounded_duration = round(duration_seconds, 1)
            row = [timestamp, track_id, rounded_duration, event_type]
            self.csv_writer.writerow(row)
            self.file_handle.flush()
            self.consecutive_write_failures = 0
        except Exception as error:
            self.consecutive_write_failures += 1
            logger.error(
                "CSV write failed (attempt %d): %s",
                self.consecutive_write_failures,
                error,
            )
            if self.consecutive_write_failures >= MAX_CONSECUTIVE_WRITE_FAILURES:
                logger.critical(
                    "CSV write has failed %d consecutive times. "
                    "Storage may be full or inaccessible.",
                    self.consecutive_write_failures,
                )

    def write_events(self, events):
        """Write multiple events at once.

        Args:
            events: List of (track_id, duration_seconds, event_type) tuples.
        """
        for track_id, duration_seconds, event_type in events:
            self.write_event(track_id, duration_seconds, event_type)

    def close(self):
        """Close the CSV file handle."""
        if self.file_handle is not None:
            try:
                self.file_handle.flush()
                self.file_handle.close()
            except Exception as error:
                logger.error("Failed to close CSV file: %s", error)
            finally:
                self.file_handle = None
                self.csv_writer = None
                self.current_date = None
