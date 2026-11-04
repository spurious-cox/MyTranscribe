# MyTranscribe 2.7.0

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
- Xcode's Python 3, with PyObjC, `torch` and `openai-whisper` installed

## Build

`build.sh` compiles a small launcher into `/Applications/MyTranscribe.app` that runs `mytranscribe.py` from this folder. Replace the `codesign` identity in `build.sh` with your own, then:

```
./build.sh
```

## Permissions

- **Screen & System Audio Recording** — for Mac audio
- **Microphone** — for Mic
- **Device Control and Data Access** (Accessibility) — for Type at cursor

## Issues

Report problems at https://github.com/spurious-cox/MyTranscribe/issues

## License

Copyright © 2026 Timothy McCoy. All rights reserved.
