#!/bin/bash
set -euo pipefail

echo "=== Video Processor Build ==="
echo ""

# ── Check prerequisites ──

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found"; exit 1; }

if command -v ffmpeg >/dev/null 2>&1; then
    echo "✓ ffmpeg found: $(which ffmpeg)"
else
    echo "⚠ ffmpeg not found — it won't be bundled in the .app"
    echo "  Install with: brew install ffmpeg"
    echo ""
fi

python3 -c "import py2app" 2>/dev/null || {
    echo "Installing py2app..."
    pip3 install py2app
}
echo "✓ py2app available"

# ── Optional: generate icon from icon.png ──

if [ -f "icon.png" ] && ! [ -f "icon.icns" ]; then
    echo "Generating app icon from icon.png..."
    ICONSET="icon.iconset"
    mkdir -p "$ICONSET"
    for SIZE in 16 32 64 128 256 512; do
        sips -z $SIZE $SIZE "icon.png" --out "$ICONSET/icon_${SIZE}x${SIZE}.png" >/dev/null 2>&1
    done
    for SIZE in 16 32 128 256 512; do
        DOUBLE=$((SIZE * 2))
        sips -z $DOUBLE $DOUBLE "icon.png" --out "$ICONSET/icon_${SIZE}x${SIZE}@2x.png" >/dev/null 2>&1
    done
    iconutil -c icns "$ICONSET" -o icon.icns 2>/dev/null && echo "✓ icon.icns created" || echo "⚠ icon generation failed — using default"
    rm -rf "$ICONSET"
fi

# ── Clean previous builds ──

echo ""
echo "Cleaning previous builds..."
rm -rf build dist .eggs

# ── Build ──

echo "Building .app bundle..."
python3 setup.py py2app 2>&1 | tail -5

# ── Code sign bundled binaries (required on macOS Sonoma+) ──

APP_PATH="dist/Video Processor.app"
if [ -d "$APP_PATH" ]; then
    # Strip Dropbox extended attributes (they break codesign)
    echo "Stripping extended attributes..."
    xattr -cr "$APP_PATH"

    # Re-copy OpenSSL libs from Homebrew (py2app corrupts their signatures)
    FRAMEWORKS="$APP_PATH/Contents/Frameworks"
    for lib in libssl.3.dylib libcrypto.3.dylib; do
        src="/opt/homebrew/opt/openssl@3/lib/$lib"
        if [ -f "$src" ] && [ -f "$FRAMEWORKS/$lib" ]; then
            cp "$src" "$FRAMEWORKS/$lib"
            echo "  re-copied: $lib (from Homebrew)"
        fi
    done

    echo "Signing bundled libraries and binaries..."

    # Sign all dylibs and .so files in Frameworks
    SIGN_FAILED=0
    find "$FRAMEWORKS" -type f \( -name "*.dylib" -o -name "*.so" \) 2>/dev/null | while read -r lib; do
        if codesign --force --sign - "$lib" 2>&1; then
            echo "  signed: $(basename "$lib")"
        else
            echo "  FAILED: $(basename "$lib")"
        fi
    done

    # Sign bundled ffmpeg/ffprobe
    for tool in ffmpeg ffprobe; do
        if [ -f "$APP_PATH/Contents/MacOS/$tool" ]; then
            codesign --force --sign - "$APP_PATH/Contents/MacOS/$tool" 2>&1 && echo "  signed: $tool" || echo "  FAILED: $tool"
        fi
    done

    # Sign the overall .app bundle
    codesign --force --sign - "$APP_PATH" 2>&1 && echo "  signed: Video Processor.app" || echo "  FAILED: Video Processor.app"

    echo ""
    echo "═══════════════════════════════════════"
    echo "  SUCCESS: $APP_PATH"
    echo "  Size: $(du -sh "$APP_PATH" | cut -f1)"
    echo ""
    echo "  To test:    open \"$APP_PATH\""
    echo "  To install: drag to /Applications"
    echo "═══════════════════════════════════════"
else
    echo ""
    echo "ERROR: Build failed — .app not found"
    exit 1
fi
