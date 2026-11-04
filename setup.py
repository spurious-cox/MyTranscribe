"""py2app build for MyTranscribe.app — v1.0.0

    ./venv/bin/python setup.py py2app

The venv is built on the python.org Python 3.12. py2app copies that
interpreter, PyObjC, torch, whisper and the models into the app, so nothing
else has to be installed. Apple silicon only: torch ships no Intel wheels.
"""

import re
import sys
from pathlib import Path

from setuptools import setup

# torch and scipy have modules deep enough to overflow the default limit
# while py2app scans their imports.
sys.setrecursionlimit(20000)

APP = ["mytranscribe.py"]
# models/ holds Whisper base.en and the ECAPA speaker model, so the app never
# downloads anything. icon.png is the Dock icon the script sets at launch.
DATA_FILES = [("", ["icon.png", "models"])]


def app_version():
    """The single source of truth: VERSION in mytranscribe.py."""
    source = Path(__file__).with_name("mytranscribe.py").read_text()
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', source, re.MULTILINE)
    if not match:
        raise SystemExit("setup.py: VERSION not found in mytranscribe.py")
    return match.group(1)


VERSION = app_version()

OPTIONS = {
    "argv_emulation": False,
    "arch": "arm64",
    # Copied whole, as folders: these load data files and native libraries
    # by path, which a zipped package cannot provide.
    "packages": ["torch", "torchaudio", "whisper", "tiktoken",
                 "speechbrain", "hyperpyyaml", "numpy", "scipy", "numba",
                 "llvmlite", "regex", "sentencepiece", "huggingface_hub",
                 "tqdm", "more_itertools", "_soundfile_data"],
    "excludes": ["tkinter", "test", "pydoc_data",
                 "matplotlib", "IPython", "pytest"],
    "iconfile": "MyTranscribe.icns",
    "plist": {
        "CFBundleName": "MyTranscribe",
        "CFBundleDisplayName": "MyTranscribe",
        "CFBundleIdentifier": "com.timmccoy.mytranscribe",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription":
            "MyTranscribe listens to your voice when Mic is chosen so it can transcribe what you say.",
        "NSScreenCaptureUsageDescription":
            "MyTranscribe listens to the sound this Mac is playing so it can transcribe it.",
        "NSHumanReadableCopyright": "© 2026 Tim McCoy. MIT License.",
    },
}

setup(
    app=APP,
    name="MyTranscribe",
    version=VERSION,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
