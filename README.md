# MyTranscribe 2.8.1

A small macOS app that transcribes what your Mac is playing, or what you say into the microphone, using OpenAI Whisper on your own machine.

## Features

- **Mac / Mic** — transcribe system audio (ScreenCaptureKit) or the microphone.
- **Capture** — text appears in 10-second chunks while it listens; click again to stop.
- **Speakers** — optional speaker labels.
- **Floating** — a small panel that stays in front of other apps and never takes focus from them.
- **Type at cursor** — types each chunk as keystrokes wherever the cursor is in the frontmost app. The clipboard is not used.
- **Spoken line breaks** — say "new line" or "new paragraph"; they are typed as Shift-Return, so chat boxes don't send.
- Filters common Whisper hallucinations and collapses repeated phrases.
- Copy and Save the transcript.

## Requirements

- macOS 13 or later, Apple silicon (arm64)

The app is self-contained (about 1.2 GB): Python, PyObjC, PyTorch, Whisper and the models are inside it. Nothing else needs to be installed, and nothing is downloaded.

## Build

Needs the python.org Python 3.12 and the two models in `models/` (Whisper `base.en.pt` and the SpeechBrain ECAPA speaker model in `ecapa/`). They are not in the repository: `base.en.pt` is what `whisper.load_model("base.en")` downloads, and the ECAPA model is [speechbrain/spkrec-ecapa-voxceleb](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb).

```
/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -m venv venv
./venv/bin/pip install "setuptools<81" wheel
./venv/bin/pip install --no-build-isolation -r requirements.txt
./build.sh
```

Replace `SIGN_ID` in `build.sh` with your own signing identity.

## Permissions

- **Screen & System Audio Recording** — for Mac audio
- **Microphone** — for Mic
- **Device Control and Data Access** (Accessibility) — for Type at cursor

## Issues

Report problems at https://github.com/spurious-cox/MyTranscribe/issues

## License

MIT — see [LICENSE](LICENSE).
