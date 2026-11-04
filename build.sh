#!/bin/zsh
# Build and install /Applications/MyTranscribe.app — v1.1.0
# A thin launcher bundle (compiled launcher.c, so permissions are granted to
# MyTranscribe itself): the app runs mytranscribe.py from this folder with
# the Xcode Python (which has PyObjC, torch and whisper). Bundling those would
# cost over 1 GB, so the bundle just points at the source.
set -e
cd "${0:A:h}"
SRC="$PWD"
APP="/Applications/MyTranscribe.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp MyTranscribe.icns "$APP/Contents/Resources/"
clang -O1 -o "$APP/Contents/MacOS/MyTranscribe" launcher.c \
    -DSCRIPT="\"$SRC/mytranscribe.py\"" \
    -DPYTHON='"/Applications/Xcode.app/Contents/Developer/usr/bin/python3"'
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>MyTranscribe</string>
<key>CFBundleDisplayName</key><string>MyTranscribe</string>
<key>CFBundleIdentifier</key><string>com.timmccoy.mytranscribe</string>
<key>CFBundleExecutable</key><string>MyTranscribe</string>
<key>CFBundleIconFile</key><string>MyTranscribe</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>2.7.0</string>
<key>CFBundleShortVersionString</key><string>2.7.0</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSMicrophoneUsageDescription</key><string>MyTranscribe listens to your voice when Mic is chosen so it can transcribe what you say.</string>
<key>NSScreenCaptureUsageDescription</key><string>MyTranscribe listens to the sound this Mac is playing so it can transcribe it.</string>
</dict></plist>
PLIST
codesign --force --sign 4208ABA3EC12F24C1F09C7BB624EFF68B44259DB --identifier com.timmccoy.mytranscribe "$APP"
touch "$APP"
echo "Installed $APP"
