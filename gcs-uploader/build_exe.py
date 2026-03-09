"""Build gcs-uploader.exe using PyInstaller.

Usage:
    pip install -r requirements.txt
    python build_exe.py
    python build_exe.py --onedir   # Folder mode (faster startup)
"""

import argparse
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="Build gcs-uploader.exe")
    parser.add_argument(
        "--onedir",
        action="store_true",
        help="Build as a folder instead of a single .exe",
    )
    args = parser.parse_args()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "gcs-uploader",
        "--noconfirm",
    ]

    if not args.onedir:
        cmd.append("--onefile")

    cmd.append("upload.py")

    print(f"Running: {' '.join(cmd)}\n")
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
