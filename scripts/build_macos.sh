#!/usr/bin/env bash
set -euo pipefail

APP_NAME="Astro Catalogue Viewer"
ZIP_NAME="AstroCatalogueViewer-macOS.zip"

python3 -m pip install --upgrade pyinstaller
python3 scripts/strip_metadata_notes.py

python3 -m PyInstaller --clean --noconfirm spec/AstroCatalogueViewer-macos.spec

ditto -c -k --sequesterRsrc --keepParent \
  "dist/$APP_NAME.app" \
  "$ZIP_NAME"

echo "Created $ZIP_NAME"
