"""Upload completed CSV files to Google Cloud Storage.

Scans for CSV files matching the configured pattern, skips today's file
(still being written), uploads the rest to GCS, and deletes each local
file after a successful upload.

Safety: If today's CSV is not found, the script aborts without uploading
anything. This prevents accidental uploads if the filename format changes.

Designed to run as a scheduled task (e.g. Windows Task Scheduler, hourly).

Configuration is read from gcs-uploader-config.json (next to the exe).
Logs are sent to Google Cloud Logging.
"""

import glob
import json
import logging
import os
import subprocess
import sys
from datetime import date

import google.cloud.logging
from google.cloud import storage
from google.oauth2 import service_account

CONFIG_FILENAME = "gcs-uploader-config.json"
CREDENTIALS_FILENAME = "gcs-uploader-credentials.json"
GCL_LOG_NAME = "gcs-uploader"
TASK_NAME = "GCS Uploader"

logger = logging.getLogger("gcs-uploader")


def get_base_path():
    """Return the directory where the executable (or script) lives."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def register_scheduled_task():
    """Add this exe to Windows Task Scheduler to run hourly, if not already registered.

    Only activates when running as a compiled exe. Skipped if the task already exists.
    Requires elevation (admin rights) to register under SYSTEM.
    """
    if not getattr(sys, "frozen", False):
        return

    check = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME],
        capture_output=True,
    )
    if check.returncode == 0:
        logger.info("Scheduled task '%s' already exists, skipping registration.", TASK_NAME)
        return

    exe_path = sys.executable
    result = subprocess.run(
        [
            "schtasks", "/create",
            "/tn", TASK_NAME,
            "/tr", f'"{exe_path}"',
            "/sc", "HOURLY",
        ],
        capture_output=True,
    )
    if result.returncode == 0:
        logger.info("Registered Windows scheduled task '%s' (hourly, current user).", TASK_NAME)
    else:
        logger.warning(
            "Could not register scheduled task '%s': %s",
            TASK_NAME,
            result.stderr.decode(errors="replace").strip(),
        )


def setup_logging(credentials):
    """Configure logging to both console and Google Cloud Logging.

    Returns the GCL handler so it can be flushed before exit.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    gcl_client = google.cloud.logging.Client(credentials=credentials)
    gcl_handler = gcl_client.get_default_handler()
    gcl_handler.name = GCL_LOG_NAME
    logger.addHandler(gcl_handler)
    return gcl_handler


def load_config(base_path):
    """Load and validate gcs-uploader-config.json."""
    config_path = os.path.join(base_path, CONFIG_FILENAME)

    if not os.path.isfile(config_path):
        print(f"Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    required_keys = ["bucket_name", "csv_pattern"]
    for key in required_keys:
        if key not in config or not config[key]:
            print(f"Missing required config key: {key}")
            sys.exit(1)

    return config


def load_credentials(base_path):
    """Load GCP service account credentials."""
    creds_path = os.path.join(base_path, CREDENTIALS_FILENAME)
    if not os.path.isfile(creds_path):
        print(f"Credentials file not found: {creds_path}")
        sys.exit(1)
    return service_account.Credentials.from_service_account_file(creds_path)


def get_today_filename(csv_pattern):
    """Build today's expected CSV filename from the pattern.

    Replaces the wildcard in the pattern with today's date (YYYY-MM-DD).
    e.g. "people_count_*.csv" -> "people_count_2026-03-09.csv"
    """
    return csv_pattern.replace("*", date.today().isoformat())


def find_csvs(base_path, csv_pattern):
    """Return all CSV file paths matching the pattern."""
    pattern = os.path.join(base_path, csv_pattern)
    return sorted(glob.glob(pattern))


def upload_and_delete(csvs, bucket_name, credentials):
    """Upload each CSV to GCS and delete the local copy on success."""
    client = storage.Client(credentials=credentials)
    bucket = client.bucket(bucket_name)

    uploaded = 0
    failed = 0

    for csv_path in csvs:
        blob_name = os.path.basename(csv_path)
        blob = bucket.blob(blob_name)

        try:
            if blob.exists():
                logger.info("Already exists in bucket, skipping upload: %s", blob_name)
            else:
                blob.upload_from_filename(csv_path)
                logger.info("Uploaded: %s -> gs://%s/%s", blob_name, bucket_name, blob_name)
                uploaded += 1

            os.remove(csv_path)
            logger.info("Deleted local file: %s", blob_name)
        except Exception as exc:
            logger.error("Failed to upload %s: %s", blob_name, exc)
            failed += 1

    return uploaded, failed


def main():
    base_path = get_base_path()
    config = load_config(base_path)
    credentials = load_credentials(base_path)
    gcl_handler = setup_logging(credentials)

    register_scheduled_task()

    exit_code = 0
    try:
        csv_pattern = config["csv_pattern"]
        bucket_name = config["bucket_name"]

        logger.info("CSV dir:      %s", base_path)
        logger.info("CSV pattern:  %s", csv_pattern)
        logger.info("Bucket:       %s", bucket_name)

        all_csvs = find_csvs(base_path, csv_pattern)
        today_filename = get_today_filename(csv_pattern)
        today_path = os.path.join(base_path, today_filename)

        if not all_csvs:
            logger.warning("No CSV files found. Machine may be freshly installed.")
            return

        # Safety: today's CSV must exist. If not, filename format may have changed = We risk uploading today's file.
        if today_path not in all_csvs:
            file_list = [os.path.basename(f) for f in all_csvs]
            logger.error(
                "ABORT: Today's CSV not found (%s). "
                "The filename format may have changed. "
                "Files found matching pattern '%s': %s",
                today_filename,
                csv_pattern,
                file_list or "(none)",
            )
            exit_code = 1
            return

        uploadable = [f for f in all_csvs if f != today_path]

        if not uploadable:
            logger.info("No CSV files to upload (only today's file exists).")
            return

        logger.info("Found %d file(s) to upload.", len(uploadable))
        uploaded, failed = upload_and_delete(uploadable, bucket_name, credentials)

        logger.info("Done. Uploaded: %d, Failed: %d", uploaded, failed)
        if failed > 0:
            exit_code = 1
    finally:
        gcl_handler.flush()
        gcl_handler.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
