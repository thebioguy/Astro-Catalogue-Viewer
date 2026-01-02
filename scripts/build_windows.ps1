param(
  [string]$Python = "python",
  [string]$Name = "Astro Catalogue Viewer",
  [string]$ZipName = "AstroCatalogueViewer-Windows.zip"
)

$ErrorActionPreference = "Stop"

& $Python -m pip install --upgrade pyinstaller
& $Python -m pip install --upgrade -r requirements.txt

& $Python -m PyInstaller --clean --noconfirm spec/AstroCatalogueViewer-windows.spec

if (Test-Path $ZipName) { Remove-Item $ZipName }
Compress-Archive -Path "dist/$Name/*" -DestinationPath $ZipName
Write-Host "Created $ZipName"
