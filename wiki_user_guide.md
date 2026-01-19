# User Guide

Astro Catalogue Viewer is a desktop app for organizing and browsing astrophotography catalogs with your own images. It combines a fast thumbnail grid, filters, metadata, and notes so you can track capture progress and plan future targets.

## Install
- Download the latest build from https://astro-catalogue-viewer.com/ or the GitHub releases page.
- Launch the app and allow file access when prompted by your OS.

## First Launch Setup
1) Open Settings.
2) Set your observer location (Latitude, Longitude, Elevation). Use Pick on Map to choose visually.
3) Choose where your images live:
   - Master Image Folder (optional): one folder containing all images.
   - Per-catalog folders: use if you store Messier, NGC, Caldwell, etc. separately.
4) Set an Archive Image Folder for duplicates and archived files.
5) Click Save.

## Image Naming (Required)
Filenames must include the standard object ID anywhere in the name. Examples:
- M31_Andromeda_Galaxy.jpg
- NGC7000 North America Nebula.tif
- C14 Double Cluster.jpg

The app scans filenames and matches objects automatically.

## Browsing the Catalogs
- Use the Catalog dropdown to switch catalogs.
- Use Search to find by object ID or name.
- Use Object Type to filter (Galaxy, Nebula, Open Cluster, etc.).
- Use Status:
  - Captured: objects with local images
  - Missing: objects with no local images
  - Suggested: best-visibility suggestions based on your location and season

## Thumbnails and Zoom
- Use the Zoom slider to change thumbnail size.
- Enable Wiki thumbnails to preview missing objects (cached locally).
- If a wiki image is wrong, use Settings -> Clear thumbnail cache and reopen the catalog.

## Object Detail View
- Click a thumbnail to open the details panel.
- Use Fit to Window and mouse wheel/trackpad to zoom and pan.
- Use Set as thumbnail to choose your best image for the grid.
- Use Archive image to move the current image to your archive folder.

## Notes
- Object notes: add observing notes, processing notes, or acquisition plans.
- Image notes: attach equipment and conditions to a specific image (for example: camera, scope, filters, exposure, Bortle class, seeing, transparency).

Notes are stored in the catalog metadata and stay with the object or image.

## Duplicate Scan (Exact Matches)
- Open Settings -> Duplicate Scan -> Scan.
- The scan uses SHA-256 to find exact duplicates.
- You can move duplicates to the Archive Image Folder (non-destructive to your main library).
- A report link appears after the scan to review duplicates.
- If you set a Master Image Folder, the app can auto-sort images into catalog folders during a duplicate scan.

## Cleanup
- Use Settings -> Clean invalid entries to remove stale references to missing files.

## Location Map
- Pick on Map opens a local map in your browser.
- Click to set coordinates or use the location button to auto-detect.

## Where Settings Are Saved
Settings live in your OS config folder:
- macOS: ~/Library/Application Support/AstroCatalogueViewer/config.json
- Windows: %APPDATA%\\AstroCatalogueViewer\\config.json
- Linux: ~/.config/AstroCatalogueViewer/config.json

## Troubleshooting
- Images not showing: confirm filenames include the correct object ID and the folder paths are set in Settings.
- Wrong thumbnails: clear thumbnail cache and refresh.
- Duplicates still visible after archive: ensure the archive folder is not inside a scanned folder.
- Suggested list empty: set observer location and ensure RA/Dec metadata exists for the catalog.
