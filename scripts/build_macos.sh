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
  --add-data "data:data" \
  --add-data "images:images" \
  "app/main.py"

ditto -c -k --sequesterRsrc --keepParent \
  "dist/$APP_NAME.app" \
  "$ZIP_NAME"

echo "Created $ZIP_NAME"
