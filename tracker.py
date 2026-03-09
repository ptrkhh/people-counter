"""ByteTrack tracking integration + entered/left counting logic.

Each detected person gets a persistent track_id from ByteTrack.
When a track_id is first seen, it is recorded as "entered".
When a track_id has not been seen for `lost_timeout_seconds`, it is
considered "left" and triggers a count event.
"""

import logging
import time

from config import EVENT_TYPE_LEFT

logger = logging.getLogger(__name__)


class TrackedPerson:
    """Holds state for a single tracked person."""

    def __init__(self, track_id, first_seen_time):
        self.track_id = track_id
        self.first_seen_time = first_seen_time
        self.last_seen_time = first_seen_time

    def update_last_seen(self, timestamp):
        """Update the last time this person was detected."""
        self.last_seen_time = timestamp

    def duration_seconds(self):
        """Return how long this person has been visible."""
        return self.last_seen_time - self.first_seen_time


class PersonTracker:
    """Manages person tracking state and generates count events.

    Receives track_ids from YOLO/ByteTrack each frame, and determines
    when a person has "left" (not seen for lost_timeout_seconds).
    """

    def __init__(self, lost_timeout_seconds):
        self.lost_timeout_seconds = lost_timeout_seconds
        self.active_tracks = {}  # track_id -> TrackedPerson
        self.total_count = 0

    def update(self, track_ids):
        """Process track_ids from the current frame.

        Args:
            track_ids: List of integer track_ids detected in this frame.

        Returns:
            List of (track_id, duration_seconds, event_type) tuples for
            persons who left since the last update.
        """
        current_time = time.monotonic()
        departed_events = []

        # Update existing tracks and add new ones
        for track_id in track_ids:
            if track_id in self.active_tracks:
                self.active_tracks[track_id].update_last_seen(current_time)
            else:
                self.active_tracks[track_id] = TrackedPerson(
                    track_id=track_id,
                    first_seen_time=current_time,
                )
                logger.debug("New person entered: track_id=%d", track_id)

        # Check for tracks that have timed out
        expired_track_ids = []
        for track_id, person in self.active_tracks.items():
            time_since_last_seen = current_time - person.last_seen_time
            if time_since_last_seen >= self.lost_timeout_seconds:
                expired_track_ids.append(track_id)
                duration = person.duration_seconds()
                departed_events.append((track_id, duration, EVENT_TYPE_LEFT))
                self.total_count += 1
                logger.debug(
                    "Person left: track_id=%d, duration=%.1fs, total_count=%d",
                    track_id,
                    duration,
                    self.total_count,
                )

        for track_id in expired_track_ids:
            del self.active_tracks[track_id]

        return departed_events

    def flush_all_tracks(self, event_type):
        """Force-flush all active tracks (e.g. on shutdown or disconnect).

        Args:
            event_type: The event_type string to record (e.g. 'shutdown_flush').

        Returns:
            List of (track_id, duration_seconds, event_type) tuples.
        """
        current_time = time.monotonic()
        flushed_events = []

        for track_id, person in self.active_tracks.items():
            # Use the time the person was actually visible (last_seen - first_seen),
            # not time since first_seen. The person may have left before the
            # disconnect/shutdown was detected.
            duration = person.duration_seconds()
            flushed_events.append((track_id, duration, event_type))
            self.total_count += 1
            logger.info(
                "Flushed track: track_id=%d, duration=%.1fs, event_type=%s",
                track_id,
                duration,
                event_type,
            )

        self.active_tracks.clear()
        return flushed_events

    def get_active_track_count(self):
        """Return the number of currently tracked persons."""
        return len(self.active_tracks)
