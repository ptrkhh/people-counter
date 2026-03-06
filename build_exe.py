"""Build a standalone .exe using PyInstaller.

Usage:
    pip install pyinstaller
    python build_exe.py              # Bundle default model (yolo26s.pt)
    python build_exe.py --all-models # Bundle all .pt models found
    python build_exe.py --onedir     # Folder mode (faster startup, easier debugging)
"""

import argparse
import glob
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="Build people-counter.exe")
    parser.add_argument(
        "--all-models",
        action="store_true",
        help="Bundle all .pt model files (default: only yolo26s.pt)",
    )
    parser.add_argument(
        "--onedir",
        action="store_true",
        help="Build as a folder instead of a single .exe (faster startup)",
    )
    args = parser.parse_args()

    # Determine which models to bundle
    if args.all_models:
        models = glob.glob("*.pt")
        if not models:
            print("ERROR: No .pt model files found in current directory")
            sys.exit(1)
        print(f"Bundling models: {', '.join(models)}")
    else:
        models = ["yolo26s.pt"]
        print(f"Bundling default model: {models[0]}")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "people-counter",
        "--collect-all", "ultralytics",
        "--collect-all", "openvino",
        "--noconfirm",
    ]

    if not args.onedir:
        cmd.append("--onefile")

    for model in models:
        cmd.extend(["--add-data", f"{model};."])

    cmd.append("main.py")

    print(f"\nRunning: {' '.join(cmd)}\n")
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
