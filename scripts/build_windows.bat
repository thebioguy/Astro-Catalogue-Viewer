@echo off
setlocal
set PYTHON=python
set APPNAME=Astro Catalogue Viewer

%PYTHON% -m pip install --upgrade pyinstaller || exit /b 1

%PYTHON% -m PyInstaller --clean --noconfirm --windowed --name "%APPNAME%" ^
  --icon "build_assets\\ACV.ico" ^
  --add-data "data;data" ^
  --add-data "images;images" ^
  "app\\main.py"
endlocal
