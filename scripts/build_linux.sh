#!/usr/bin/env bash
set -euo pipefail

ZIP_NAME="AstroCatalogueViewer-Linux.zip"

python3 -m pip install --upgrade pyinstaller

python3 -m PyInstaller --clean --noconfirm AstroCatalogueViewer-linux.spec

if [ -f "$ZIP_NAME" ]; then
  rm "$ZIP_NAME"
fi

cd dist
zip -r "../$ZIP_NAME" "Astro Catalogue Viewer"
echo "Created $ZIP_NAME"
