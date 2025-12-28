@echo off
setlocal
set PYTHON=python
set APPNAME=Astro Catalogue Viewer
set ZIPNAME=AstroCatalogueViewer-Windows.zip

%PYTHON% -m pip install --upgrade pyinstaller || exit /b 1
%PYTHON% -m pip install --upgrade -r requirements.txt || exit /b 1

%PYTHON% -m PyInstaller --clean --noconfirm --windowed --name "%APPNAME%" ^
  --icon "build_assets\\ACV.ico" ^
  --add-data "data;data" ^
  --add-data "images;images" ^
  --collect-all "PySide6" ^
  "app\\main.py"

if exist "%ZIPNAME%" del "%ZIPNAME%"
powershell -NoProfile -Command "Compress-Archive -Path \"dist\\%APPNAME%\\*\" -DestinationPath \"%ZIPNAME%\""
echo Created %ZIPNAME%
endlocal
