param(
  [string]$Python = "python",
  [string]$Name = "Astro Catalogue Viewer",
  [string]$ZipName = "AstroCatalogueViewer-Windows.zip"
)

$ErrorActionPreference = "Stop"

& $Python -m pip install --upgrade pyinstaller

& $Python -m PyInstaller `
  --clean `
  --noconfirm `
  --windowed `
  --name $Name `
  --icon "build_assets/ACV.ico" `
  --add-data "data;data" `
  --add-data "images;images" `
  "app/main.py"

if (Test-Path $ZipName) { Remove-Item $ZipName }
Compress-Archive -Path "dist/$Name/*" -DestinationPath $ZipName
Write-Host "Created $ZipName"
