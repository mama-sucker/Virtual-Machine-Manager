#!/usr/bin/env bash
set -euo pipefail

# ── AppImage build script for VM Lab ──
# Prerequisites:
#   pip install pyinstaller
#   sudo apt install linuxdeploy-plugin-appimage
#   wget -O linuxdeploy https://github.com/linuxdeploy/linuxdeploy/releases/latest/download/linuxdeploy-x86_64.AppImage
#   chmod +x linuxdeploy

APP_NAME="vm-lab"
ICON="assets/icon.png"
ICON_256="assets/icon-256.png"
DESKTOP="vm-lab.desktop"
BUILD_DIR="build"
DIST_DIR="dist"

echo "=== Building AppImage for $APP_NAME ==="

# 1. Create a 256x256 icon (one of the valid sizes for AppImage)
convert "$ICON" -resize "256x256" "$ICON_256"

# 2. Clean previous builds
rm -rf "$BUILD_DIR" "$DIST_DIR"

# 2. Build with PyInstaller
echo "[1/3] Running PyInstaller..."
pyinstaller \
    --name "$APP_NAME" \
    --onefile \
    --windowed \
    --add-data "assets/icon.png:assets" \
    --add-data "vm-lab.desktop:." \
    --icon "$ICON" \
    --noconfirm \
    main.py

# 3. Create AppImage with linuxdeploy
echo "[2/3] Creating AppImage with linuxdeploy..."

# Download linuxdeploy if not present
if [ ! -f "linuxdeploy" ]; then
    echo "  Downloading linuxdeploy..."
    wget -q -O linuxdeploy \
        "https://github.com/linuxdeploy/linuxdeploy/releases/latest/download/linuxdeploy-x86_64.AppImage"
    chmod +x linuxdeploy
fi

# Run linuxdeploy to wrap the PyInstaller output
./linuxdeploy \
    --appdir "AppDir" \
    --executable "$DIST_DIR/$APP_NAME" \
    --desktop-file "$DESKTOP" \
    --icon-file "$ICON_256" \
    --icon-filename "$APP_NAME" \
    --output appimage

echo "[3/3] Done!"
echo ""
echo "Your AppImage is ready:"
ls -lh "$APP_NAME"*.AppImage 2>/dev/null || ls -lh *.AppImage 2>/dev/null || echo "  (check current directory)"
echo ""
echo "To run it:"
echo "  chmod +x $APP_NAME"*.AppImage
echo "  ./$APP_NAME"*.AppImage
