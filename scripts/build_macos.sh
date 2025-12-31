#!/usr/bin/env bash
set -euo pipefail

APP_NAME="Astro Catalogue Viewer"
ZIP_NAME="AstroCatalogueViewer-macOS.zip"

python3 -m pip install --upgrade pyinstaller

python3 -m PyInstaller \
  --clean \
  --noconfirm \
  --windowed \
  --name "$APP_NAME" \
  --icon "build_assets/ACV.icns" \
  --add-data "data:data" \
  "app/main.py"

ditto -c -k --sequesterRsrc --keepParent \
  "dist/$APP_NAME.app" \
  "$ZIP_NAME"

echo "Created $ZIP_NAME"
