"""
py2app build configuration for Video Batch Processor.

Usage:
    python3 setup.py py2app        # full standalone .app
    python3 setup.py py2app -A     # alias mode (dev/testing — fast, links to source)
"""

from setuptools import setup
import shutil
import os

APP = ["video_processor.py"]
APP_NAME = "Video Processor"

# Bundle ffmpeg/ffprobe inside the .app if available at build time
DATA_FILES: list[tuple[str, list[str]]] = []
binaries = []
for tool in ("ffmpeg", "ffprobe"):
    path = shutil.which(tool)
    if path:
        binaries.append(path)
if binaries:
    DATA_FILES.append(("../MacOS", binaries))

# Check for optional app icon
icon_file = "icon.icns" if os.path.isfile("icon.icns") else None

OPTIONS = {
    "argv_emulation": False,           # MUST be False on macOS Sonoma+
    "emulate_shell_environment": True,  # inherit user PATH for ffmpeg
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": "com.benkeogh.videoprocessor",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "NSRequiresAquaSystemAppearance": False,  # dark mode support
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
    },
}

if icon_file:
    OPTIONS["iconfile"] = icon_file

setup(
    name=APP_NAME,
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
