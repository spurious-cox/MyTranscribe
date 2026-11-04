#!/bin/zsh
# Notarize MyTranscribe.app and wrap it in a distributable DMG — v1.0.0
#
# The app is about 1.2 GB, so each notarization upload takes a while.
#
# Run ./build.sh first; this takes dist/MyTranscribe.app as it finds it.
#
# Both the app and the DMG are notarized and stapled: the app because that
# is what gets dragged out of the DMG, the DMG because that is what gets
# downloaded. Notarization uses the "PixProNotary" keychain profile, so no
# password lives here. To recreate the profile:
#   xcrun notarytool store-credentials "PixProNotary" \
#       --apple-id <appleid> --team-id RUDN8D7ZN9
set -e
cd "${0:A:h}"

SIGN_ID="4208ABA3EC12F24C1F09C7BB624EFF68B44259DB"
PROFILE="PixProNotary"
APP="dist/MyTranscribe.app"

[[ -d "$APP" ]] || { echo "error: $APP not found — run ./build.sh first" >&2; exit 1; }

VERSION=$(plutil -extract CFBundleShortVersionString raw "$APP/Contents/Info.plist")
DMG="dist/MyTranscribe-${VERSION}.dmg"

xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1 \
    || { echo "error: no notary profile '$PROFILE' in keychain" >&2; exit 1; }

echo "==> notarizing the app (MyTranscribe $VERSION)"
rm -f dist/MyTranscribe_notarize.zip
ditto -c -k --keepParent "$APP" dist/MyTranscribe_notarize.zip
xcrun notarytool submit dist/MyTranscribe_notarize.zip --keychain-profile "$PROFILE" --wait
xcrun stapler staple "$APP"

echo "==> building the disk image"
rm -rf dist/dmg "$DMG"
mkdir -p dist/dmg
cp -R "$APP" dist/dmg/
ln -s /Applications dist/dmg/Applications
cat > "dist/dmg/READ ME FIRST.txt" <<READMEEOF
MyTranscribe $VERSION

INSTALLING
    Drag MyTranscribe onto the Applications folder beside it.
    Needs macOS 14 or later on Apple silicon.

PERMISSIONS
    macOS asks for each the first time it is needed. To change them, see
    System Settings > Privacy & Security:

        Screen & System Audio Recording   to transcribe Mac audio
        Microphone                        to transcribe your voice
        Device Control and Data Access    for Type at cursor
                                          (called Accessibility before macOS 27)

https://github.com/spurious-cox/MyTranscribe
READMEEOF
for attempt in 1 2 3 4 5; do
    if hdiutil create -volname "MyTranscribe" -srcfolder dist/dmg -ov \
            -format UDZO "$DMG" >/dev/null 2>dist/hdiutil.err; then
        break
    fi
    echo "    hdiutil attempt $attempt failed: $(tr -d '\n' < dist/hdiutil.err)"
    [[ $attempt == 5 ]] && { echo "error: could not build the disk image" >&2; exit 1; }
    sleep 5
done
rm -rf dist/dmg dist/hdiutil.err

echo "==> signing and notarizing the disk image"
codesign --force --timestamp --sign "$SIGN_ID" "$DMG"
xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait
xcrun stapler staple "$DMG"

echo "==> installing the stapled app to /Applications"
pkill -x MyTranscribe 2>/dev/null || true
rm -rf /Applications/MyTranscribe.app
cp -R "$APP" /Applications/
xattr -dr com.apple.quarantine /Applications/MyTranscribe.app 2>/dev/null || true

echo "==> updating the Homebrew cask"
CASK="$(brew --repository 2>/dev/null)/Library/Taps/spurious-cox/homebrew-tap/Casks/mytranscribe.rb"
if [[ -f "$CASK" ]]; then
    SHA=$(shasum -a 256 "$DMG" | cut -d" " -f1)
    /usr/bin/sed -i "" \
        -e "s/^  version \".*\"/  version \"$VERSION\"/" \
        -e "s/^  sha256 \".*\"/  sha256 \"$SHA\"/" "$CASK"
    echo "    $CASK -> $VERSION ($SHA)"
else
    echo "    no cask at $CASK — skipped"
fi

echo "==> results"
echo "    dmg:      $DMG  ($(du -h "$DMG" | cut -f1))"
echo "    stapled:  app $(xcrun stapler validate "$APP" >/dev/null 2>&1 && echo YES || echo no), dmg $(xcrun stapler validate "$DMG" >/dev/null 2>&1 && echo YES || echo no)"
spctl -a -t open --context context:primary-signature -v "$DMG" 2>&1 | sed 's/^/    gatekeeper: /'
