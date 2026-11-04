#!/bin/zsh
# Build, sign and install MyTranscribe.app — v2.0.0
#
#   ./build.sh                build, sign, install to /Applications
#   ./build.sh --no-install   build and sign into dist/ only
#
# The app is self-contained: py2app copies Python 3.12, PyObjC, torch,
# whisper and models/ into it (about 1.2 GB). First time only:
#   /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -m venv venv
#   ./venv/bin/pip install "setuptools<81" wheel
#   ./venv/bin/pip install --no-build-isolation -r requirements.txt
#
# Fix-ups after py2app:
#   * py2app renames torch's dylibs to @executable_path/... install names.
#     torchaudio's libraries ask for @rpath/libc10.dylib and carry no rpath,
#     so they only load if torch's libraries keep their @rpath names.
#   * Signing: `codesign --deep` skips the .so files under Resources/lib and
#     the extension-less Mach-O at Contents/MacOS/python; `file` decides what
#     is a Mach-O. --options runtime (hardened runtime) is required for
#     notarization; the entitlements let CPython, numba and the mic work
#     under it. The identity is chosen by SHA-1 hash.
set -e
cd "${0:A:h}"

SIGN_ID="4208ABA3EC12F24C1F09C7BB624EFF68B44259DB"
APP="dist/MyTranscribe.app"
SITE="venv/lib/python3.12/site-packages"

if ! security find-identity -p codesigning | grep -q "$SIGN_ID"; then
    echo "error: signing identity $SIGN_ID not in keychain" >&2
    exit 1
fi
[[ -d models ]] || { echo "error: models/ is missing (base.en.pt and ecapa/)" >&2; exit 1; }

echo "==> building"
# ruamel is a namespace package; py2app's import scan cannot find it
# without an __init__.py.
touch "$SITE/ruamel/__init__.py"
# PyObjC ships debug-symbol bundles; they would land in the zip, unsignable.
find "$SITE" -name '*.dSYM' -prune -exec rm -rf {} +
rm -rf build dist
./venv/bin/python setup.py py2app >/dev/null

echo "==> restoring torch library names"
for f in $APP/Contents/Resources/lib/python3.12/torch/lib/*.dylib; do
    [[ $(otool -D "$f" | tail -1) == @executable_path/* ]] &&
        install_name_tool -id "@rpath/${f:t}" "$f" 2>/dev/null
done

echo "==> checking nothing signable is hidden inside the zip"
ZIP=$(echo $APP/Contents/Resources/lib/python3*.zip)
HIDDEN=$(./venv/bin/python -c "import zipfile,sys;print(len([n for n in zipfile.ZipFile(sys.argv[1]).namelist() if n.endswith(('.dylib','.so'))]))" "$ZIP")
[[ "$HIDDEN" == 0 ]] || { echo "error: $HIDDEN binaries inside $ZIP cannot be signed" >&2; exit 1; }

echo "==> signing inner binaries with the hardened runtime"
find "$APP" -type f -print0 | while IFS= read -r -d $'\0' f; do
    if file -b "$f" | grep -q 'Mach-O'; then
        codesign --force --timestamp --options runtime --sign "$SIGN_ID" "$f" 2>/dev/null
    fi
done

echo "==> sealing nested frameworks, then the app"
find "$APP" -name '*.framework' -print0 \
    | xargs -0 -n1 -I{} codesign --force --timestamp --options runtime --sign "$SIGN_ID" {} 2>/dev/null || true
codesign --force --timestamp --options runtime \
    --entitlements mytranscribe.entitlements --sign "$SIGN_ID" "$APP"
codesign --verify --deep --strict "$APP"
echo "    size: $(du -sh $APP | cut -f1)"

if [[ "$1" != "--no-install" ]]; then
    echo "==> installing to /Applications"
    pkill -x MyTranscribe 2>/dev/null || true
    rm -rf /Applications/MyTranscribe.app
    ditto "$APP" /Applications/MyTranscribe.app
    xattr -dr com.apple.quarantine /Applications/MyTranscribe.app 2>/dev/null || true
    codesign -dv /Applications/MyTranscribe.app 2>&1 | grep -E "Identifier=|TeamIdentifier="
    plutil -extract CFBundleShortVersionString raw /Applications/MyTranscribe.app/Contents/Info.plist
fi
