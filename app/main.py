from __future__ import annotations

import sys
import hashlib
import re
import subprocess
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import http.server
import json
import threading
from urllib.parse import urlparse, unquote
import datetime
import array
from dataclasses import replace

from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid

from catalog import DEFAULT_CONFIG, CatalogItem, collect_object_types, load_config, load_catalog_items, resolve_metadata_path, save_config, save_note, save_thumbnail, save_image_note
from catalog import PROJECT_ROOT
from image_cache import ThumbnailCache


APP_NAME = "Astro Catalogue Viewer"
ORG_NAME = "AstroCatalogueViewer"
UPDATE_REPO = "thebioguy/Astro-Catalogue-Viewer"
SUPPORTERS_URL = f"https://raw.githubusercontent.com/{UPDATE_REPO}/main/data/supporters.json"
APP_VERSION_FILE = "data/version.json"
DATA_VERSION_FILE = "data/data_version.json"
DATA_VERSION_URL = f"https://raw.githubusercontent.com/{UPDATE_REPO}/main/data/data_version.json"


def _extract_version(payload: object) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("version", "app_version", "tag"):
            value = payload.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None
    if isinstance(payload, str):
        text = payload.strip()
        return text or None
    return None


def _load_version_from_file(path: Path) -> Optional[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _extract_version(payload)


def _load_bundled_app_version() -> str:
    return _load_version_from_file(PROJECT_ROOT / APP_VERSION_FILE) or "Unknown"


def _load_bundled_data_version() -> str:
    return _load_version_from_file(PROJECT_ROOT / DATA_VERSION_FILE) or "Unknown"


APP_VERSION = _load_bundled_app_version()
DEFAULT_DATA_VERSION = _load_bundled_data_version()
SHUTDOWN_EVENT = threading.Event()


class ThumbnailSignals(QtCore.QObject):
    loaded = QtCore.Signal(str, QtGui.QImage)


class CatalogLoadSignals(QtCore.QObject):
    loaded = QtCore.Signal(list)


class MapFetchSignals(QtCore.QObject):
    loaded = QtCore.Signal(bytes)
    failed = QtCore.Signal()


class RemoteThumbnailSignals(QtCore.QObject):
    loaded = QtCore.Signal(str, QtGui.QImage)
    failed = QtCore.Signal(str)


class ImageLoadSignals(QtCore.QObject):
    loaded = QtCore.Signal(int, str, QtGui.QImage)
    failed = QtCore.Signal(int, str, str)


class UpdateSignals(QtCore.QObject):
    available = QtCore.Signal(str, str)
    up_to_date = QtCore.Signal(str)
    failed = QtCore.Signal(str)
    finished = QtCore.Signal()


class SupportersSignals(QtCore.QObject):
    loaded = QtCore.Signal(list)
    failed = QtCore.Signal(str)


class DataVersionSignals(QtCore.QObject):
    loaded = QtCore.Signal(str)
    failed = QtCore.Signal(str)


class DuplicateScanSignals(QtCore.QObject):
    finished = QtCore.Signal(str, str)


class DuplicateScanTask(QtCore.QRunnable):
    def __init__(
        self,
        config_path: Path,
        extensions: List[str],
        report_path: Path,
    ) -> None:
        super().__init__()
        self.config_path = config_path
        self.extensions = extensions
        self.report_path = report_path
        self.signals = DuplicateScanSignals()

    def run(self) -> None:
        if SHUTDOWN_EVENT.is_set():
            return
        error = ""
        try:
            sort_command = [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "sort_master_images.py"),
                "--config",
                str(self.config_path),
                "--extensions",
                ",".join(self.extensions),
            ]
            scan_command = [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "find_duplicate_images_by_catalog.py"),
                "--config",
                str(self.config_path),
                "--extensions",
                ",".join(self.extensions),
                "--output",
                str(self.report_path),
            ]
            result = subprocess.run(sort_command, check=False, capture_output=True, text=True)
            if result.returncode != 0:
                error = result.stderr.strip() or result.stdout.strip()
            if not error:
                result = subprocess.run(scan_command, check=False, capture_output=True, text=True)
                if result.returncode != 0:
                    error = result.stderr.strip() or result.stdout.strip()
        except Exception as exc:
            error = str(exc)
        if SHUTDOWN_EVENT.is_set() or not isValid(self.signals):
            return
        try:
            self.signals.finished.emit(str(self.report_path), error)
        except RuntimeError:
            return


class ThumbnailTask(QtCore.QRunnable):
    def __init__(self, item_key: str, image_path: Path, cache: ThumbnailCache) -> None:
        super().__init__()
        self.item_key = item_key
        self.image_path = image_path
        self.cache = cache
        self.signals = ThumbnailSignals()

    def run(self) -> None:
        if SHUTDOWN_EVENT.is_set():
            return
        image = self.cache.create_thumbnail(self.image_path)
        if image is None:
            return
        if not isValid(self.signals):
            return
        try:
            self.signals.loaded.emit(self.item_key, image)
        except RuntimeError:
            return


class WikiThumbnailTask(QtCore.QRunnable):
    def __init__(
        self,
        item_key: str,
        page_title: str,
        cache_path: Path,
        thumb_size: int,
        image_url: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.item_key = item_key
        self.page_title = page_title
        self.cache_path = cache_path
        self.thumb_size = thumb_size
        self.image_url = image_url
        self.signals = RemoteThumbnailSignals()

    def run(self) -> None:
        import urllib.parse

        if SHUTDOWN_EVENT.is_set():
            return
        if self.cache_path.exists():
            image = QtGui.QImage(str(self.cache_path))
            if not image.isNull():
                self._emit_loaded(image)
                return
            try:
                self.cache_path.unlink()
            except OSError:
                pass
        try:
            if self.image_url:
                data = self._fetch_bytes(self.image_url)
            else:
                title = urllib.parse.quote(self.page_title.replace(" ", "_"))
                summary_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
                summary_payload = self._fetch_bytes(summary_url)
                if SHUTDOWN_EVENT.is_set():
                    return
                payload = json.loads(summary_payload.decode("utf-8"))
                thumb = payload.get("thumbnail", {}).get("source") or payload.get("originalimage", {}).get("source")
                if not thumb:
                    self._emit_failed()
                    return
                if CatalogModel._is_bad_wiki_thumbnail(thumb):
                    self._emit_failed()
                    return
                data = self._fetch_bytes(thumb)
            if SHUTDOWN_EVENT.is_set():
                return
            image = QtGui.QImage.fromData(data)
            if image.isNull():
                self._emit_failed()
                return
            image = image.convertToFormat(QtGui.QImage.Format.Format_ARGB32)
            if self.page_title.strip().lower() == "saturn":
                image = self._center_square_crop(image)
            image = self._scale_to_square(image)
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(str(self.cache_path), "PNG")
            self._emit_loaded(image)
        except Exception:
            self._emit_failed()

    def _emit_loaded(self, image: QtGui.QImage) -> None:
        if SHUTDOWN_EVENT.is_set():
            return
        if not isValid(self.signals):
            return
        try:
            self.signals.loaded.emit(self.item_key, image)
        except RuntimeError:
            return

    def _emit_failed(self) -> None:
        if SHUTDOWN_EVENT.is_set():
            return
        if not isValid(self.signals):
            return
        try:
            self.signals.failed.emit(self.item_key)
        except RuntimeError:
            return

    def _scale_to_square(self, image: QtGui.QImage) -> QtGui.QImage:
        scaled = image.scaled(
            self.thumb_size,
            self.thumb_size,
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        canvas = QtGui.QImage(
            self.thumb_size,
            self.thumb_size,
            QtGui.QImage.Format.Format_ARGB32,
        )
        canvas.fill(QtGui.QColor("#1c1c1c"))
        painter = QtGui.QPainter(canvas)
        x = (self.thumb_size - scaled.width()) // 2
        y = (self.thumb_size - scaled.height()) // 2
        painter.drawImage(x, y, scaled)
        painter.end()
        return canvas

    @staticmethod
    def _center_square_crop(image: QtGui.QImage) -> QtGui.QImage:
        width = image.width()
        height = image.height()
        side = min(width, height)
        x = (width - side) // 2
        y = (height - side) // 2
        return image.copy(x, y, side, side)

    @staticmethod
    def _fetch_bytes(url: str) -> bytes:
        if SHUTDOWN_EVENT.is_set():
            return b""
        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            [
                "curl",
                "-sL",
                "--max-time",
                "8",
                "--retry",
                "3",
                "--retry-delay",
                "1",
                "-H",
                "User-Agent: AstroCatalogueViewer/1.0",
                url,
            ],
            check=True,
            capture_output=True,
            creationflags=creationflags,
        )
        return result.stdout


class ImageLoadTask(QtCore.QRunnable):
    def __init__(self, request_id: int, image_path: Path) -> None:
        super().__init__()
        self.request_id = request_id
        self.image_path = image_path
        self.signals = ImageLoadSignals()

    def run(self) -> None:
        if SHUTDOWN_EVENT.is_set():
            return
        image, error = _load_display_image(self.image_path)
        if image is None or image.isNull():
            if not isValid(self.signals):
                return
            try:
                self.signals.failed.emit(self.request_id, str(self.image_path), error or "Unable to load image.")
            except RuntimeError:
                return
            return
        if not isValid(self.signals):
            return
        try:
            self.signals.loaded.emit(self.request_id, str(self.image_path), image)
        except RuntimeError:
            return


class CatalogLoadTask(QtCore.QRunnable):
    def __init__(self, config: Dict) -> None:
        super().__init__()
        self.config = config
        self.signals = CatalogLoadSignals()

    def run(self) -> None:
        items = load_catalog_items(self.config)
        self.signals.loaded.emit(items)


class MapTileFetchTask(QtCore.QRunnable):
    def __init__(
        self,
        latitude: float,
        longitude: float,
        zoom: int,
        size: QtCore.QSize,
        tile_servers: List[str],
    ) -> None:
        super().__init__()
        self.latitude = latitude
        self.longitude = longitude
        self.zoom = zoom
        self.size = size
        self.tile_servers = tile_servers
        self.signals = MapFetchSignals()

    def run(self) -> None:
        import math
        import urllib.request

        tile_size = 256
        width = self.size.width()
        height = self.size.height()

        lat = max(-85.0511, min(85.0511, self.latitude))
        world = tile_size * (2**self.zoom)
        x = (self.longitude + 180.0) / 360.0 * world
        rad = math.radians(lat)
        y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * world

        x0 = x - width / 2
        y0 = y - height / 2
        x_start = int(math.floor(x0 / tile_size))
        x_end = int(math.floor((x0 + width - 1) / tile_size))
        y_start = int(math.floor(y0 / tile_size))
        y_end = int(math.floor((y0 + height - 1) / tile_size))

        image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtGui.QColor("#141414"))
        painter = QtGui.QPainter(image)

        tiles_fetched = 0
        max_tile = 2**self.zoom
        for ty in range(y_start, y_end + 1):
            if ty < 0 or ty >= max_tile:
                continue
            for tx in range(x_start, x_end + 1):
                tx_wrapped = tx % max_tile
                tile_data = None
                for base in self.tile_servers:
                    url = base.format(z=self.zoom, x=tx_wrapped, y=ty)
                    try:
                        request = urllib.request.Request(
                            url,
                            headers={"User-Agent": "AstroCatalogueViewer/1.0"},
                        )
                        with urllib.request.urlopen(request, timeout=6) as response:
                            tile_data = response.read()
                        if tile_data:
                            break
                    except Exception:
                        continue
                if not tile_data:
                    continue
                tile_img = QtGui.QImage.fromData(tile_data)
                if tile_img.isNull():
                    continue
                target_x = int(tx * tile_size - x0)
                target_y = int(ty * tile_size - y0)
                painter.drawImage(target_x, target_y, tile_img)
                tiles_fetched += 1

        painter.end()

        if tiles_fetched == 0:
            self.signals.failed.emit()
            return

        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        data = bytes(buffer.data())
        self.signals.loaded.emit(data)


class CatalogModel(QtCore.QAbstractListModel):
    wiki_thumbnail_loaded = QtCore.Signal(str, QtGui.QPixmap)
    _wiki_thumbnail_blocklist = {
        "Caldwell": {"C64"},
        "NGC": {"NGC146", "NGC771", "NGC1502"},
    }
    _wiki_thumbnail_refresh = {
        "Solar system": {"CHARIKLO", "SWIFT-TUTTLE"},
    }

    def __init__(self, items: List[CatalogItem], cache: ThumbnailCache, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self._items = items
        self._cache = cache
        self._loading = set()
        self._pixmaps: Dict[str, QtGui.QPixmap] = {}
        self._remote_pixmaps: Dict[str, QtGui.QPixmap] = {}
        self._remote_loading = set()
        self._remote_failed = set()
        self._wiki_refresh_done = set()
        self._wiki_enabled = False
        self._row_lookup = {item.unique_key: row for row, item in enumerate(items)}
        self._thread_pool = QtCore.QThreadPool.globalInstance()
        self._wiki_pool = QtCore.QThreadPool(self)
        self._wiki_pool.setMaxThreadCount(4)
        self._placeholder = self._create_placeholder()

    def rowCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._items)

    def data(self, index: QtCore.QModelIndex, role: int = QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self._items[index.row()]
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            return item.display_name
        if role == QtCore.Qt.ItemDataRole.ToolTipRole:
            return f"{item.catalog} | {item.object_type}"
        if role == QtCore.Qt.ItemDataRole.DecorationRole:
            if item.thumbnail_path is None:
                remote = self._remote_pixmaps.get(item.unique_key)
                if remote:
                    return remote
                if self._wiki_enabled:
                    self._queue_wiki_thumbnail(item)
                return self._placeholder
            cached = self._cache.get_thumbnail(item.thumbnail_path)
            if cached:
                return cached
            pixmap = self._pixmaps.get(item.unique_key)
            if pixmap:
                return pixmap
            self._queue_thumbnail(item)
            return self._placeholder
        if role == QtCore.Qt.ItemDataRole.UserRole:
            return item
        return None

    def _queue_thumbnail(self, item: CatalogItem) -> None:
        if item.thumbnail_path is None:
            return
        if item.unique_key in self._loading:
            return
        self._loading.add(item.unique_key)
        task = ThumbnailTask(item.unique_key, item.thumbnail_path, self._cache)
        task.signals.loaded.connect(self._on_thumbnail_loaded)
        self._thread_pool.start(task)

    def _queue_wiki_thumbnail(self, item: CatalogItem) -> None:
        if item.unique_key in self._remote_loading or item.unique_key in self._remote_failed:
            return
        if self._should_skip_wiki_thumbnail(item):
            self._remote_failed.add(item.unique_key)
            return
        title = self._wiki_title_for_item(item)
        image_url = item.wiki_thumbnail
        if image_url and self._is_bad_wiki_thumbnail(image_url):
            image_url = None
        if not title and not image_url:
            self._remote_failed.add(item.unique_key)
            return
        cache_key = title or item.object_id.replace(" ", "_")
        cache_path = self._wiki_cache_path(cache_key)
        self._maybe_refresh_wiki_thumbnail(item, cache_path)
        if cache_path.exists():
            image = QtGui.QImage(str(cache_path))
            if not image.isNull():
                pixmap = QtGui.QPixmap.fromImage(image)
                self._remote_pixmaps[item.unique_key] = pixmap
                row = self._row_lookup.get(item.unique_key)
                if row is not None:
                    index = self.index(row)
                    self.dataChanged.emit(index, index, [QtCore.Qt.ItemDataRole.DecorationRole])
                return
            try:
                cache_path.unlink()
            except OSError:
                pass
        self._remote_loading.add(item.unique_key)
        task = WikiThumbnailTask(item.unique_key, cache_key, cache_path, self._cache.thumb_size, image_url=image_url)
        task.signals.loaded.connect(self._on_wiki_thumbnail_loaded)
        task.signals.failed.connect(self._on_wiki_thumbnail_failed)
        self._wiki_pool.start(task)

    @staticmethod
    def _is_bad_wiki_thumbnail(url: str) -> bool:
        if not url:
            return False
        parsed = urlparse(url)
        name = Path(parsed.path).name.lower()
        bad_tokens = ("map", "chart", "finder", "locator", "diagram", "orbit", "orbital", "trajectory")
        return any(token in name for token in bad_tokens)

    def _should_skip_wiki_thumbnail(self, item: CatalogItem) -> bool:
        blocklist = self._wiki_thumbnail_blocklist.get(item.catalog)
        if not blocklist:
            return False
        normalized = item.object_id.replace(" ", "").upper()
        return normalized in blocklist

    def _maybe_refresh_wiki_thumbnail(self, item: CatalogItem, cache_path: Path) -> None:
        refresh_list = self._wiki_thumbnail_refresh.get(item.catalog)
        if not refresh_list:
            return
        normalized = item.object_id.replace(" ", "").upper()
        if normalized not in refresh_list:
            return
        if item.unique_key in self._wiki_refresh_done:
            return
        self._wiki_refresh_done.add(item.unique_key)
        if cache_path.exists():
            try:
                cache_path.unlink()
            except OSError:
                pass

    def _on_wiki_thumbnail_loaded(self, item_key: str, image: QtGui.QImage) -> None:
        pixmap = QtGui.QPixmap.fromImage(image)
        if pixmap.isNull():
            self._remote_failed.add(item_key)
            self._remote_loading.discard(item_key)
            return
        self._remote_pixmaps[item_key] = pixmap
        self.wiki_thumbnail_loaded.emit(item_key, pixmap)
        self._remote_loading.discard(item_key)
        row = self._row_lookup.get(item_key)
        if row is None:
            return
        index = self.index(row)
        self.dataChanged.emit(index, index, [QtCore.Qt.ItemDataRole.DecorationRole])

    def _on_wiki_thumbnail_failed(self, item_key: str) -> None:
        self._remote_loading.discard(item_key)
        self._remote_failed.add(item_key)

    def _wiki_title_for_item(self, item: CatalogItem) -> Optional[str]:
        link = item.external_link or ""
        if "wikipedia.org" not in link:
            return None
        parsed = urlparse(link)
        if not parsed.path.startswith("/wiki/"):
            return None
        title = parsed.path[len("/wiki/"):]
        if not title:
            return None
        return unquote(title)

    def _wiki_cache_path(self, title: str) -> Path:
        payload = f"{title}:{self._cache.thumb_size}"
        key = hashlib.sha1(payload.encode("utf-8")).hexdigest()
        return self._cache.cache_dir / f"wiki_{key}.png"

    def _on_thumbnail_loaded(self, item_key: str, image: QtGui.QImage) -> None:
        row = self._row_lookup.get(item_key)
        if row is None:
            return
        item = self._items[row]
        if item.thumbnail_path is None:
            return
        pixmap = self._cache.store_thumbnail_image(item.thumbnail_path, image)
        self._pixmaps[item_key] = pixmap
        self._loading.discard(item_key)
        index = self.index(row)
        self.dataChanged.emit(index, index, [QtCore.Qt.ItemDataRole.DecorationRole])

    def set_items(self, items: List[CatalogItem]) -> None:
        self.beginResetModel()
        self._items = items
        self._pixmaps.clear()
        self._remote_pixmaps.clear()
        self._remote_loading.clear()
        self._remote_failed.clear()
        self._loading.clear()
        self._wiki_refresh_done.clear()
        self._row_lookup = {item.unique_key: row for row, item in enumerate(items)}
        self.endResetModel()

    def update_cache(self, cache: ThumbnailCache) -> None:
        self._cache = cache
        self._pixmaps.clear()
        self._remote_pixmaps.clear()
        self._remote_loading.clear()
        self._remote_failed.clear()

    def set_wiki_thumbnails_enabled(self, enabled: bool) -> None:
        self._wiki_enabled = enabled
        if not enabled:
            self._remote_pixmaps.clear()
            self._remote_loading.clear()
            self._remote_failed.clear()
        self._loading.clear()
        if self._items:
            self.dataChanged.emit(self.index(0), self.index(len(self._items) - 1))

    def index_for_key(self, item_key: str) -> Optional[QtCore.QModelIndex]:
        row = self._row_lookup.get(item_key)
        if row is None:
            return None
        return self.index(row)

    def get_wiki_pixmap(self, item_key: str) -> Optional[QtGui.QPixmap]:
        return self._remote_pixmaps.get(item_key)

    def update_item_notes(self, item_key: str, notes: str) -> None:
        row = self._row_lookup.get(item_key)
        if row is None:
            return
        item = self._items[row]
        updated = CatalogItem(
            object_id=item.object_id,
            catalog=item.catalog,
            name=item.name,
            object_type=item.object_type,
            distance_ly=item.distance_ly,
            discoverer=item.discoverer,
            discovery_year=item.discovery_year,
            best_months=item.best_months,
            description=item.description,
            notes=notes,
            image_notes=item.image_notes,
            external_link=item.external_link,
            wiki_thumbnail=item.wiki_thumbnail,
            ra_hours=item.ra_hours,
            dec_deg=item.dec_deg,
            image_paths=item.image_paths,
            thumbnail_path=item.thumbnail_path,
        )
        self._items[row] = updated
        index = self.index(row)
        self.dataChanged.emit(index, index, [QtCore.Qt.ItemDataRole.DisplayRole])

    def update_item_image_note(self, item_key: str, image_name: str, notes: str) -> None:
        row = self._row_lookup.get(item_key)
        if row is None:
            return
        item = self._items[row]
        image_notes = dict(item.image_notes)
        if notes.strip():
            image_notes[image_name] = notes
        else:
            image_notes.pop(image_name, None)
        updated = CatalogItem(
            object_id=item.object_id,
            catalog=item.catalog,
            name=item.name,
            object_type=item.object_type,
            distance_ly=item.distance_ly,
            discoverer=item.discoverer,
            discovery_year=item.discovery_year,
            best_months=item.best_months,
            description=item.description,
            notes=item.notes,
            image_notes=image_notes,
            external_link=item.external_link,
            wiki_thumbnail=item.wiki_thumbnail,
            ra_hours=item.ra_hours,
            dec_deg=item.dec_deg,
            image_paths=item.image_paths,
            thumbnail_path=item.thumbnail_path,
        )
        self._items[row] = updated
        index = self.index(row)
        self.dataChanged.emit(index, index, [QtCore.Qt.ItemDataRole.DisplayRole])

    def update_item_thumbnail(self, item_key: str, thumbnail_name: str) -> None:
        row = self._row_lookup.get(item_key)
        if row is None:
            return
        item = self._items[row]
        thumbnail_path = next(
            (path for path in item.image_paths if path.name == thumbnail_name or path.stem == thumbnail_name),
            item.thumbnail_path,
        )
        updated = CatalogItem(
            object_id=item.object_id,
            catalog=item.catalog,
            name=item.name,
            object_type=item.object_type,
            distance_ly=item.distance_ly,
            discoverer=item.discoverer,
            discovery_year=item.discovery_year,
            best_months=item.best_months,
            description=item.description,
            notes=item.notes,
            image_notes=item.image_notes,
            external_link=item.external_link,
            wiki_thumbnail=item.wiki_thumbnail,
            ra_hours=item.ra_hours,
            dec_deg=item.dec_deg,
            image_paths=item.image_paths,
            thumbnail_path=thumbnail_path,
        )
        self._items[row] = updated
        self._pixmaps.pop(item_key, None)
        index = self.index(row)
        self.dataChanged.emit(index, index, [QtCore.Qt.ItemDataRole.DecorationRole])

    def _create_placeholder(self) -> QtGui.QPixmap:
        size = self._cache.thumb_size
        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(QtGui.QColor("#1c1c1c"))
        painter = QtGui.QPainter(pixmap)
        painter.setPen(QtGui.QColor("#2d2d2d"))
        painter.drawRect(0, 0, size - 1, size - 1)
        painter.end()
        return pixmap


class CatalogItemDelegate(QtWidgets.QStyledItemDelegate):
    def paint(self, painter: QtGui.QPainter, option: QtWidgets.QStyleOptionViewItem, index: QtCore.QModelIndex) -> None:
        painter.save()
        rect = option.rect
        icon = index.data(QtCore.Qt.ItemDataRole.DecorationRole)
        text = index.data(QtCore.Qt.ItemDataRole.DisplayRole) or ""
        metrics = option.fontMetrics
        text_height = metrics.height() + 6
        icon_rect = QtCore.QRect(rect.left() + 1, rect.top() + 1, rect.width() - 2, rect.height() - 2)
        text_rect = QtCore.QRect(rect.left() + 4, rect.bottom() - text_height - 2, rect.width() - 8, text_height)

        if isinstance(icon, QtGui.QPixmap):
            painter.drawPixmap(icon_rect, icon)
        else:
            painter.fillRect(icon_rect, QtGui.QColor("#1c1c1c"))
            pen = QtGui.QPen(QtGui.QColor("#3a3a3a"))
            painter.setPen(pen)
            painter.drawRect(icon_rect)
        if option.state & QtWidgets.QStyle.StateFlag.State_Selected:
            painter.setPen(QtGui.QPen(QtGui.QColor("#d94a4a"), 2))
            painter.drawRect(icon_rect.adjusted(1, 1, -1, -1))

        badge_size = 18
        margin = 4
        item: CatalogItem = index.data(QtCore.Qt.ItemDataRole.UserRole)
        if item:
            if len(item.image_paths) > 0:
                count_rect = QtCore.QRect(
                    icon_rect.left() + margin,
                    icon_rect.top() + margin,
                    badge_size + 6,
                    badge_size,
                )
                painter.fillRect(count_rect, QtGui.QColor(0, 0, 0, 160))
                painter.setPen(QtGui.QColor("#f2f2f2"))
                painter.drawRect(count_rect)
                painter.drawText(
                    count_rect,
                    QtCore.Qt.AlignmentFlag.AlignCenter,
                    str(len(item.image_paths)),
                )
            if item.notes or any(note for note in item.image_notes.values()):
                info_rect = QtCore.QRect(
                    icon_rect.right() - badge_size - margin,
                    icon_rect.top() + margin,
                    badge_size,
                    badge_size,
                )
                painter.setBrush(QtGui.QColor(0, 0, 0, 160))
                painter.setPen(QtGui.QColor("#f2f2f2"))
                painter.drawEllipse(info_rect)
                painter.drawText(
                    info_rect,
                    QtCore.Qt.AlignmentFlag.AlignCenter,
                    "i",
                )

        painter.fillRect(text_rect, QtGui.QColor(0, 0, 0, 160))
        painter.setPen(QtGui.QColor("#f2f2f2"))
        elided = metrics.elidedText(text, QtCore.Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, QtCore.Qt.AlignmentFlag.AlignCenter, elided)

        painter.restore()

    def sizeHint(self, option: QtWidgets.QStyleOptionViewItem, index: QtCore.QModelIndex) -> QtCore.QSize:
        size = index.data(QtCore.Qt.ItemDataRole.SizeHintRole)
        if isinstance(size, QtCore.QSize):
            return size
        return super().sizeHint(option, index)


class CatalogFilterProxy(QtCore.QSortFilterProxyModel):
    def __init__(self, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self.search_text = ""
        self.type_filter = ""
        self.catalog_filter = ""
        self.status_filter = ""
        self.setFilterCaseSensitivity(QtCore.Qt.CaseSensitivity.CaseInsensitive)

    def set_search_text(self, text: str) -> None:
        self.search_text = text.strip()
        self.invalidate()

    def set_type_filter(self, value: str) -> None:
        self.type_filter = value
        self.invalidate()

    def set_catalog_filter(self, value: str) -> None:
        self.catalog_filter = value
        self.invalidate()

    def set_status_filter(self, value: str) -> None:
        self.status_filter = value
        self.invalidate()

    def filterAcceptsRow(self, source_row: int, source_parent: QtCore.QModelIndex) -> bool:
        model = self.sourceModel()
        index = model.index(source_row, 0, source_parent)
        item: CatalogItem = model.data(index, QtCore.Qt.ItemDataRole.UserRole)
        if item is None:
            return False
        if self.catalog_filter and not self.search_text:
            if item.catalog != self.catalog_filter:
                return False
        if self.type_filter and item.object_type != self.type_filter:
            return False
        if self.status_filter:
            if self.status_filter == "Captured" and not item.image_paths:
                return False
            if self.status_filter == "Missing" and item.image_paths:
                return False
            if self.status_filter == "Suggested" and not self._is_suggested(item):
                return False
        if self.search_text:
            search = self.search_text.lower()
            if search not in item.object_id.lower() and search not in item.name.lower():
                return False
        return True

    def _is_suggested(self, item: CatalogItem) -> bool:
        if item.image_paths:
            return False
        if not item.best_months:
            return False
        month = datetime.datetime.now().strftime("%b")
        for idx in range(0, len(item.best_months), 3):
            if item.best_months[idx: idx + 3] == month:
                return True
        return False


class ImageView(QtWidgets.QGraphicsView):
    fullscreen_requested = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setScene(QtWidgets.QGraphicsScene(self))
        self._pixmap_item: Optional[QtWidgets.QGraphicsPixmapItem] = None
        self._zoom = 0
        self._pixmap: Optional[QtGui.QPixmap] = None

    def set_pixmap(self, pixmap: Optional[QtGui.QPixmap]) -> None:
        self.scene().clear()
        self._zoom = 0
        self._pixmap = pixmap if pixmap and not pixmap.isNull() else None
        if self._pixmap:
            self._pixmap_item = self.scene().addPixmap(self._pixmap)
            self.setSceneRect(pixmap.rect())
            self.fit_to_window()
        else:
            self._pixmap_item = None

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._pixmap_item and self._pixmap and self._zoom == 0:
            self.fit_to_window()

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        if self._pixmap_item is None:
            return
        angle = event.angleDelta().y()
        if angle > 0:
            factor = 1.15
            self._zoom += 1
        else:
            factor = 0.87
            self._zoom -= 1
        if self._zoom < -5:
            self._zoom = -5
            return
        if self._zoom > 120:
            self._zoom = 120
            return
        self.scale(factor, factor)

    def fit_to_window(self) -> None:
        if self._pixmap_item is None:
            return
        self.resetTransform()
        self.fitInView(self.sceneRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def zoom_actual(self) -> None:
        if self._pixmap_item is None:
            return
        self.resetTransform()
        self.centerOn(self._pixmap_item)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._pixmap_item is None:
            return
        self.fullscreen_requested.emit()
        event.accept()


def _tone_map_high_bit_image(image: QtGui.QImage) -> QtGui.QImage:
    fmt = image.format()
    if fmt == QtGui.QImage.Format.Format_Grayscale16:
        return _tone_map_grayscale16(image)
    if fmt in (
        QtGui.QImage.Format.Format_RGBX64,
        QtGui.QImage.Format.Format_RGBA64,
        QtGui.QImage.Format.Format_RGBA64_Premultiplied,
    ):
        return _tone_map_rgba64(image)
    if image.depth() > 32:
        converted = image.convertToFormat(QtGui.QImage.Format.Format_RGBA64)
        return _tone_map_rgba64(converted)
    return image


def _tone_map_grayscale16(image: QtGui.QImage) -> QtGui.QImage:
    width = image.width()
    height = image.height()
    buf = image.bits()
    if hasattr(buf, "setsize"):
        buf.setsize(image.sizeInBytes())
    data = array.array("H")
    data.frombytes(buf.tobytes())
    if not data:
        return image.convertToFormat(QtGui.QImage.Format.Format_Grayscale8)
    step = max(1, len(data) // 100000)
    sample = data[::step]
    min_val = min(sample)
    max_val = max(sample)
    if max_val == min_val:
        max_val = min_val + 1
    scale = 255.0 / (max_val - min_val)
    out = bytearray(len(data))
    for i, value in enumerate(data):
        mapped = int((value - min_val) * scale)
        if mapped < 0:
            mapped = 0
        elif mapped > 255:
            mapped = 255
        out[i] = mapped
    out_image = QtGui.QImage(out, width, height, width, QtGui.QImage.Format.Format_Grayscale8)
    return out_image.copy()


def _tone_map_rgba64(image: QtGui.QImage) -> QtGui.QImage:
    width = image.width()
    height = image.height()
    buf = image.bits()
    if hasattr(buf, "setsize"):
        buf.setsize(image.sizeInBytes())
    data = array.array("H")
    data.frombytes(buf.tobytes())
    if not data:
        return image.convertToFormat(QtGui.QImage.Format.Format_RGB888)
    step = max(1, (len(data) // 4) // 100000)
    if step < 1:
        step = 1
    min_val = 65535
    max_val = 0
    for i in range(0, len(data), 4 * step):
        r = data[i]
        g = data[i + 1]
        b = data[i + 2]
        if r < min_val:
            min_val = r
        if g < min_val:
            min_val = g
        if b < min_val:
            min_val = b
        if r > max_val:
            max_val = r
        if g > max_val:
            max_val = g
        if b > max_val:
            max_val = b
    if max_val == min_val:
        max_val = min_val + 1
    scale = 255.0 / (max_val - min_val)
    out = bytearray(width * height * 3)
    out_i = 0
    for i in range(0, len(data), 4):
        r = int((data[i] - min_val) * scale)
        g = int((data[i + 1] - min_val) * scale)
        b = int((data[i + 2] - min_val) * scale)
        if r < 0:
            r = 0
        elif r > 255:
            r = 255
        if g < 0:
            g = 0
        elif g > 255:
            g = 255
        if b < 0:
            b = 0
        elif b > 255:
            b = 255
        out[out_i] = r
        out[out_i + 1] = g
        out[out_i + 2] = b
        out_i += 3
    out_image = QtGui.QImage(out, width, height, width * 3, QtGui.QImage.Format.Format_RGB888)
    return out_image.copy()


def _load_tiff_with_tifffile(path: Path) -> Tuple[Optional[QtGui.QImage], Optional[str]]:
    try:
        import numpy as np
        import tifffile
    except Exception:
        return None, "TIFF support not available (missing tifffile)."

    try:
        with tifffile.TiffFile(str(path)) as tif:
            series = tif.series[0] if tif.series else None
            if series is not None:
                data = series.asarray()
                axes = getattr(series, "axes", None)
            elif tif.pages:
                page = tif.pages[0]
                data = page.asarray()
                axes = getattr(page, "axes", None)
            else:
                data = None
                axes = None
    except Exception:
        return None, "Unable to decode TIFF. Install imagecodecs for compressed TIFFs."

    if data is None:
        return None, "Unable to decode TIFF."
    data = np.asarray(data)
    if data.dtype.kind == "c":
        data = np.abs(data)
    data = _normalize_tiff_array(data, axes)
    if data is None:
        return None, "Unsupported TIFF layout."
    if data.ndim == 2:
        return _tone_map_numpy_to_qimage(data, "L"), None
    if data.ndim == 3 and data.shape[2] in (3, 4):
        mode = "RGB" if data.shape[2] == 3 else "RGBA"
        return _tone_map_numpy_to_qimage(data, mode), None
    return None, "Unsupported TIFF layout."


def _normalize_tiff_array(data, axes: Optional[str] = None) -> Optional["np.ndarray"]:
    import numpy as np

    if data is None:
        return None
    array_data = np.asarray(data)
    if array_data.size == 0:
        return None
    if axes and len(axes) == array_data.ndim:
        axes = axes.upper()
        sample_axes = [idx for idx, axis in enumerate(axes) if axis in ("S", "C")]
        if sample_axes:
            channel_axis = sample_axes[0]
            array_data = np.moveaxis(array_data, channel_axis, -1)
            axes = axes.replace(axes[channel_axis], "")
        # Drop non-image axes (e.g., Z/T) by taking the first frame.
        while array_data.ndim > 3:
            array_data = array_data[0]
        if array_data.ndim == 3 and array_data.shape[2] > 4:
            array_data = array_data[:, :, :3]
        if array_data.ndim == 2:
            return array_data
        if array_data.ndim == 3 and array_data.shape[2] in (1, 2):
            return array_data[:, :, 0]
        return array_data

    array_data = np.squeeze(array_data)
    if array_data.ndim == 2:
        return array_data

    channel_axis = _detect_channel_axis(array_data)
    if channel_axis is not None:
        array_data = np.moveaxis(array_data, channel_axis, -1)
        while array_data.ndim > 3:
            array_data = array_data[0]
        if array_data.ndim == 3:
            channels = array_data.shape[2]
            if channels == 1:
                return array_data[:, :, 0]
            if channels == 2:
                return array_data[:, :, 0]
            if channels > 4:
                array_data = array_data[:, :, :3]
        return array_data

    while array_data.ndim > 2:
        array_data = array_data[0]
    return array_data


def _detect_channel_axis(array_data) -> Optional[int]:
    if array_data.ndim < 3:
        return None
    shape = array_data.shape
    candidates = []
    for axis, size in enumerate(shape):
        if size in (3, 4):
            candidates.append(axis)
    if not candidates:
        for axis, size in enumerate(shape):
            if size in (1, 2):
                candidates.append(axis)
    if not candidates and array_data.ndim == 3:
        for axis, size in enumerate(shape):
            if size <= 8:
                other_dims = [shape[i] for i in range(array_data.ndim) if i != axis]
                if all(dim > 16 for dim in other_dims):
                    candidates.append(axis)
    if not candidates:
        return None
    if candidates[-1] == array_data.ndim - 1:
        return candidates[-1]
    if candidates[0] == 0:
        return candidates[0]
    return candidates[0]


def _tone_map_numpy_to_qimage(data, mode: str) -> Optional[QtGui.QImage]:
    import numpy as np

    if data.size == 0:
        return None
    array_data = np.asarray(data)
    array_data = np.nan_to_num(array_data, nan=0.0, posinf=0.0, neginf=0.0)
    if array_data.dtype.kind in ("f", "i", "u"):
        low = np.percentile(array_data, 1.0)
        high = np.percentile(array_data, 99.0)
        if high <= low:
            high = low + 1.0
        scaled = (array_data - low) * (255.0 / (high - low))
        scaled = np.clip(scaled, 0, 255).astype(np.uint8)
    else:
        scaled = array_data.astype(np.uint8)

    if mode == "L":
        height, width = scaled.shape
        stride = width
        image = QtGui.QImage(scaled.tobytes(), width, height, stride, QtGui.QImage.Format.Format_Grayscale8)
        return image.copy()
    if mode == "RGB":
        height, width, _ = scaled.shape
        stride = width * 3
        image = QtGui.QImage(scaled.tobytes(), width, height, stride, QtGui.QImage.Format.Format_RGB888)
        return image.copy()
    if mode == "RGBA":
        height, width, _ = scaled.shape
        stride = width * 4
        image = QtGui.QImage(scaled.tobytes(), width, height, stride, QtGui.QImage.Format.Format_RGBA8888)
        return image.copy()
    return None


def _load_display_image(path: Path) -> Tuple[Optional[QtGui.QImage], Optional[str]]:
    reader = QtGui.QImageReader(str(path))
    if reader.canRead():
        reader.setAutoTransform(True)
        image = reader.read()
        if not image.isNull():
            if image.depth() > 32 or image.format() in (
                QtGui.QImage.Format.Format_Grayscale16,
                QtGui.QImage.Format.Format_RGBX64,
                QtGui.QImage.Format.Format_RGBA64,
                QtGui.QImage.Format.Format_RGBA64_Premultiplied,
            ):
                image = _tone_map_high_bit_image(image)
            return image, None
    error = reader.errorString() if reader.error() != QtGui.QImageReader.ImageReaderError.UnknownError else None
    tif_image, tif_error = _load_tiff_with_tifffile(path)
    if tif_image is not None:
        return tif_image, None
    if tif_error:
        error = tif_error
    pil_image, pil_error = _load_tiff_with_pillow(path)
    if pil_image is not None:
        return pil_image, None
    if pil_error:
        error = pil_error
    fallback = QtGui.QImage(str(path))
    if not fallback.isNull():
        return fallback, None
    return None, error or "Unable to load image."


def _load_tiff_with_pillow(path: Path) -> Tuple[Optional[QtGui.QImage], Optional[str]]:
    try:
        import warnings
        from PIL import Image
    except Exception:
        return None, None
    try:
        Image.MAX_IMAGE_PIXELS = None
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        with Image.open(str(path)) as img:
            img = img.convert("RGB")
            width, height = img.size
            data = img.tobytes()
            image = QtGui.QImage(data, width, height, width * 3, QtGui.QImage.Format.Format_RGB888)
            return image.copy(), None
    except Exception:
        return None, "Pillow could not decode this TIFF."


class LightboxDialog(QtWidgets.QDialog):
    def __init__(self, pixmap: QtGui.QPixmap, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Image Preview")
        self.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self.image_view = ImageView()
        self.image_view.set_pixmap(pixmap)

        close_button = QtWidgets.QPushButton("Exit")
        close_button.clicked.connect(self.close)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.addWidget(self.image_view, stretch=1)

        button_row = QtWidgets.QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

        self.setStyleSheet(
            "QDialog { background: #0b0b0b; } QPushButton { background: #2c2c2c; border: 1px solid #3b3b3b; padding: 8px 16px; }"
        )

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.key() in (QtCore.Qt.Key.Key_Escape, QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        screen = None
        if self.parentWidget():
            screen = self.parentWidget().screen()
        if screen is None:
            screen = QtGui.QGuiApplication.primaryScreen()
        if screen:
            self.setGeometry(screen.geometry())
        super().showEvent(event)


class DetailPanel(QtWidgets.QWidget):
    thumbnail_selected = QtCore.Signal(str, str, str)
    archive_requested = QtCore.Signal(str)
    image_changed = QtCore.Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.image_view = ImageView()
        self.title = QtWidgets.QLabel("Select an object")
        self.title.setObjectName("detailTitle")
        self.metadata = QtWidgets.QLabel("")
        self.metadata.setWordWrap(True)
        self.metadata.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Fixed)
        self.metadata.setContentsMargins(0, 0, 0, 0)
        self.image_info = QtWidgets.QLabel("")
        self.image_info.setObjectName("imageInfo")
        self.image_info.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Fixed)
        self.image_info.setContentsMargins(0, 0, 0, 0)
        self.description = QtWidgets.QTextEdit()
        self.description.setReadOnly(True)
        self.description.setObjectName("descriptionBox")
        self.notes = QtWidgets.QTextEdit()
        self.notes.setObjectName("notesBox")
        self.notes.setPlaceholderText("Notes...")
        self.notes.setMinimumHeight(80)
        self.notes.setMaximumHeight(140)
        self.external_link = QtWidgets.QLabel("")
        self.external_link.setOpenExternalLinks(True)
        self.external_link.setObjectName("externalLink")
        self.external_link.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Fixed)
        self.external_link.setContentsMargins(0, 0, 0, 0)
        self.fit_button = QtWidgets.QPushButton("Fit to Window")
        self.fit_button.clicked.connect(self.image_view.fit_to_window)
        self.image_view.fullscreen_requested.connect(self._open_lightbox)
        self.image_view.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.image_view.customContextMenuRequested.connect(self._show_image_context_menu)
        self.prev_button = QtWidgets.QPushButton("◀")
        self.next_button = QtWidgets.QPushButton("▶")
        self.thumb_button = QtWidgets.QPushButton("Set as thumbnail")
        self.archive_button = QtWidgets.QPushButton("Archive image")
        self.prev_button.clicked.connect(self._show_prev_image)
        self.next_button.clicked.connect(self._show_next_image)
        self.thumb_button.clicked.connect(self._set_thumbnail)
        self.archive_button.clicked.connect(self._request_archive)
        self._current_item: Optional[CatalogItem] = None
        self._notes_block = False
        self._image_index = 0
        self._wiki_pixmap: Optional[QtGui.QPixmap] = None
        self._lightbox: Optional[LightboxDialog] = None
        self._image_load_id = 0
        self._image_thread_pool = QtCore.QThreadPool.globalInstance()
        self._image_cache: Dict[str, QtGui.QPixmap] = {}

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.title)
        fit_row = QtWidgets.QHBoxLayout()
        fit_row.addWidget(self.fit_button)
        fit_row.addStretch(1)
        layout.addLayout(fit_row)

        image_container = QtWidgets.QWidget()
        image_layout = QtWidgets.QVBoxLayout(image_container)
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.addWidget(self.image_view, stretch=1)

        left_widget = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        nav_row = QtWidgets.QHBoxLayout()
        nav_row.addWidget(self.prev_button)
        nav_row.addWidget(self.next_button)
        nav_row.addWidget(self.thumb_button)
        nav_row.addWidget(self.archive_button)
        nav_row.addStretch(1)
        left_layout.addLayout(nav_row)
        left_layout.addWidget(self.metadata)
        left_layout.addWidget(self.image_info)
        left_layout.addWidget(self.external_link)

        right_widget = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.description, stretch=2)
        right_layout.addWidget(self.notes, stretch=1)

        columns_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        columns_splitter.addWidget(left_widget)
        columns_splitter.addWidget(right_widget)
        columns_splitter.setStretchFactor(0, 1)
        columns_splitter.setStretchFactor(1, 3)
        columns_splitter.setChildrenCollapsible(False)
        columns_splitter.setHandleWidth(6)
        columns_splitter.setSizes([320, 960])
        main_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        main_splitter.addWidget(image_container)
        main_splitter.addWidget(columns_splitter)
        main_splitter.setStretchFactor(0, 2)
        main_splitter.setStretchFactor(1, 0)
        main_splitter.setChildrenCollapsible(False)
        main_splitter.setHandleWidth(6)
        main_splitter.setSizes([520, 200])
        self.splitter = main_splitter
        self._left_widget = left_widget
        self._main_splitter = main_splitter
        self._initial_detail_sized = False

        layout.addWidget(main_splitter, stretch=1)

    def update_item(self, item: Optional[CatalogItem]) -> None:
        self._current_item = item
        self._notes_block = True
        self._wiki_pixmap = None
        self._image_load_id += 1
        if item is None:
            self.title.setText("Select an object")
            self.metadata.setText("")
            self.description.setPlainText("")
            self.notes.setPlainText("")
            self.image_info.setText("")
            self.image_view.set_pixmap(None)
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
            self.thumb_button.setEnabled(False)
            self.archive_button.setEnabled(False)
            self._notes_block = False
            return
        self.title.setText(item.display_name)
        metadata_lines = [
            f"Catalog: {item.catalog}",
            f"Type: {item.object_type or 'Unknown'}",
        ]
        if item.distance_ly:
            metadata_lines.append(f"Distance: {item.distance_ly:,.0f} ly")
        if item.discoverer:
            label = f"Discoverer: {item.discoverer}"
            if item.discovery_year:
                label += f" ({item.discovery_year})"
            metadata_lines.append(label)
        if item.best_months:
            metadata_lines.append(
                f"Best visibility: {self._format_months(item.best_months)}"
            )
        self.metadata.setText("\n".join(metadata_lines))
        self.description.setPlainText(item.description or "")
        if item.external_link:
            self.external_link.setText(f'<a href="{item.external_link}">More info</a>')
            self.external_link.show()
        else:
            self.external_link.hide()
        self._image_index = 0
        if item.thumbnail_path and item.image_paths:
            try:
                self._image_index = item.image_paths.index(item.thumbnail_path)
            except ValueError:
                self._image_index = 0
        self._update_image_view()
        self._apply_notes_for_current_image()
        self._notes_block = False

    @staticmethod
    def _format_months(value: str) -> str:
        if not value:
            return ""
        months = [value[i:i + 3] for i in range(0, len(value), 3)]
        return " ".join(months)

    def connect_notes_changed(self, callback) -> None:
        self.notes.textChanged.connect(callback)

    def current_notes(self) -> str:
        return self.notes.toPlainText()

    def current_image_name(self) -> Optional[str]:
        if not self._current_item or not self._current_item.image_paths:
            return None
        paths = self._current_item.image_paths
        if not paths:
            return None
        index = max(0, min(self._image_index, len(paths) - 1))
        return paths[index].name

    def current_image_path(self) -> Optional[Path]:
        if not self._current_item or not self._current_item.image_paths:
            return None
        index = max(0, min(self._image_index, len(self._current_item.image_paths) - 1))
        return self._current_item.image_paths[index]

    def current_item(self) -> Optional[CatalogItem]:
        return self._current_item

    def notes_blocked(self) -> bool:
        return self._notes_block

    def _update_image_view(self) -> None:
        if not self._current_item or not self._current_item.image_paths:
            if self._wiki_pixmap and not self._wiki_pixmap.isNull():
                self.image_view.set_pixmap(self._wiki_pixmap)
                size_info = f"{self._wiki_pixmap.width()}x{self._wiki_pixmap.height()}"
                self.image_info.setText(f"Wikipedia preview (not captured) | {size_info}")
            else:
                self.image_view.set_pixmap(None)
                self.image_info.setText("No image available")
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
            self.thumb_button.setEnabled(False)
            self.archive_button.setEnabled(False)
            return
        paths = self._current_item.image_paths
        self._image_index = max(0, min(self._image_index, len(paths) - 1))
        path = paths[self._image_index]
        cache_key = str(path)
        cached = self._image_cache.get(cache_key)
        if cached and not cached.isNull():
            self.image_view.set_pixmap(cached)
            size_info = f"{cached.width()}x{cached.height()}"
            self.image_info.setText(
                f"Image {self._image_index + 1}/{len(paths)} | File: {path.name}"
                + (f" | {size_info}" if size_info else "")
            )
            self.prev_button.setEnabled(len(paths) > 1)
            self.next_button.setEnabled(len(paths) > 1)
            self.thumb_button.setEnabled(True)
            self.archive_button.setEnabled(True)
            return
        self.image_view.set_pixmap(None)
        self.image_info.setText(f"Loading image... | File: {path.name}")
        self.prev_button.setEnabled(len(paths) > 1)
        self.next_button.setEnabled(len(paths) > 1)
        self.thumb_button.setEnabled(True)
        self.archive_button.setEnabled(True)
        self._start_image_load(path)

    def _start_image_load(self, path: Path) -> None:
        self._image_load_id += 1
        request_id = self._image_load_id
        task = ImageLoadTask(request_id, path)
        task.signals.loaded.connect(self._on_image_loaded)
        task.signals.failed.connect(self._on_image_failed)
        self._image_thread_pool.start(task)

    def _on_image_loaded(self, request_id: int, path_value: str, image: QtGui.QImage) -> None:
        if request_id != self._image_load_id:
            return
        if not self._current_item or not self._current_item.image_paths:
            return
        current_path = self._current_item.image_paths[self._image_index]
        if str(current_path) != path_value:
            return
        pixmap = QtGui.QPixmap.fromImage(image)
        if pixmap.isNull():
            self._on_image_failed(request_id, path_value)
            return
        cache_key = str(current_path)
        self._image_cache[cache_key] = pixmap
        self.image_view.set_pixmap(pixmap)
        size_info = f"{pixmap.width()}x{pixmap.height()}"
        self.image_info.setText(
            f"Image {self._image_index + 1}/{len(self._current_item.image_paths)} | File: {current_path.name}"
            + (f" | {size_info}" if size_info else "")
        )
        self.thumb_button.setEnabled(True)
        self.archive_button.setEnabled(True)

    def _on_image_failed(self, request_id: int, path_value: str, message: str) -> None:
        if request_id != self._image_load_id:
            return
        self.image_view.set_pixmap(None)
        self.image_info.setText(message or "Unable to load image.")
        self.thumb_button.setEnabled(False)
        self.archive_button.setEnabled(False)

    def _apply_initial_sizes(self) -> None:
        if self._initial_detail_sized:
            return
        if not hasattr(self, "_left_widget") or not hasattr(self, "_main_splitter"):
            return
        total_height = max(self._main_splitter.size().height(), self.height())
        if total_height <= 0:
            QtCore.QTimer.singleShot(50, self._apply_initial_sizes)
            return
        detail_height = 200
        image_height = max(240, total_height - detail_height)
        self._main_splitter.setSizes([image_height, detail_height])
        self._initial_detail_sized = True

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self._apply_initial_sizes)

    def _show_prev_image(self) -> None:
        if not self._current_item or not self._current_item.image_paths:
            return
        self._image_index = (self._image_index - 1) % len(self._current_item.image_paths)
        self._update_image_view()
        self._apply_notes_for_current_image()
        current_name = self.current_image_name() or ""
        self.image_changed.emit(current_name)

    def _show_next_image(self) -> None:
        if not self._current_item or not self._current_item.image_paths:
            return
        self._image_index = (self._image_index + 1) % len(self._current_item.image_paths)
        self._update_image_view()
        self._apply_notes_for_current_image()
        current_name = self.current_image_name() or ""
        self.image_changed.emit(current_name)

    def _set_thumbnail(self) -> None:
        if not self._current_item or not self._current_item.image_paths:
            return
        path = self._current_item.image_paths[self._image_index]
        self.thumbnail_selected.emit(self._current_item.catalog, self._current_item.object_id, path.name)

    def set_wiki_pixmap(self, pixmap: Optional[QtGui.QPixmap]) -> None:
        self._wiki_pixmap = pixmap if pixmap and not pixmap.isNull() else None
        self._update_image_view()

    def _request_archive(self) -> None:
        if not self._current_item or not self._current_item.image_paths:
            return
        path = self._current_item.image_paths[self._image_index]
        self.archive_requested.emit(str(path))

    def _show_image_context_menu(self, position: QtCore.QPoint) -> None:
        path = self.current_image_path()
        menu = QtWidgets.QMenu(self)
        if path is None:
            empty = QtGui.QAction("No image file selected", menu)
            empty.setEnabled(False)
            menu.addAction(empty)
            menu.exec(self.image_view.mapToGlobal(position))
            return
        path_action = QtGui.QAction(str(path), menu)
        path_action.setEnabled(False)
        menu.addAction(path_action)
        menu.addSeparator()
        open_action = QtGui.QAction("Open containing folder", menu)
        open_action.triggered.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path.parent))))
        menu.addAction(open_action)
        copy_action = QtGui.QAction("Copy file path", menu)
        copy_action.triggered.connect(lambda: QtWidgets.QApplication.clipboard().setText(str(path)))
        menu.addAction(copy_action)
        menu.exec(self.image_view.mapToGlobal(position))

    def set_current_image_by_name(self, image_name: str) -> None:
        if not self._current_item or not self._current_item.image_paths:
            return
        for index, path in enumerate(self._current_item.image_paths):
            if path.name == image_name:
                if index != self._image_index:
                    self._image_index = index
                    self._update_image_view()
                    self._apply_notes_for_current_image()
                return

    def update_current_item_notes(self, image_name: Optional[str], notes: Optional[str], object_notes: Optional[str] = None) -> None:
        if not self._current_item:
            return
        image_notes = dict(self._current_item.image_notes)
        if image_name:
            if notes and notes.strip():
                image_notes[image_name] = notes
            else:
                image_notes.pop(image_name, None)
        updated_notes = self._current_item.notes if object_notes is None else object_notes
        self._current_item = replace(
            self._current_item,
            notes=updated_notes,
            image_notes=image_notes,
        )

    def _apply_notes_for_current_image(self) -> None:
        self._notes_block = True
        note_text = ""
        if self._current_item:
            image_name = self.current_image_name()
            if image_name:
                if self._current_item.image_notes:
                    note_text = self._current_item.image_notes.get(image_name, "")
                else:
                    note_text = self._current_item.notes or ""
            else:
                note_text = self._current_item.notes or ""
        self.notes.setPlainText(note_text)
        self._notes_block = False

    def _open_lightbox(self) -> None:
        pixmap = self.image_view._pixmap
        if pixmap is None or pixmap.isNull():
            return
        if self._lightbox and self._lightbox.isVisible():
            return
        dialog = LightboxDialog(pixmap, self)
        dialog.finished.connect(lambda _result: self._clear_lightbox())
        self._lightbox = dialog
        QtCore.QTimer.singleShot(0, dialog.show)

    def _clear_lightbox(self) -> None:
        self._lightbox = None


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)

        self.config_path = config_path
        self.config = load_config(self.config_path)
        self._data_version = self._load_cached_data_version()
        self._ensure_user_metadata_files()
        if not self.config.get("cleanup_invalid_image_only_entries_done", False):
            self._cleanup_invalid_image_only_entries()
            self.config["cleanup_invalid_image_only_entries_done"] = True
            save_config(self.config_path, self.config)
        if not self.config_path.exists():
            save_config(self.config_path, self.config)
        self._saved_state = self.config.get("ui_state", {})
        if not self._saved_state:
            self._saved_state = {
                "filters": {"catalog": "Messier"},
                "search": "",
            }
        self._saved_state_applied = False

        cache_dir = self._cache_dir()
        thumb_size = self.config.get("thumb_size", 240)
        self.thumbnail_cache = ThumbnailCache(cache_dir, thumb_size)

        self.items: List[CatalogItem] = []
        self.model = CatalogModel(self.items, self.thumbnail_cache, self)
        self.proxy = CatalogFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self._auto_fit_enabled = True
        self._suppress_auto_fit = True
        self._thread_pool = QtCore.QThreadPool.globalInstance()
        self._catalog_pool = QtCore.QThreadPool(self)
        self._catalog_pool.setMaxThreadCount(1)
        self._loading = False
        self._pending_reload = False
        self._pending_config: Optional[Dict] = None
        self._preview_active = False
        self._auto_fit_timer = QtCore.QTimer(self)
        self._auto_fit_timer.setSingleShot(True)
        self._auto_fit_timer.setInterval(150)
        self._auto_fit_timer.timeout.connect(self._auto_fit_thumbnails)
        self._zoom_timer = QtCore.QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.setInterval(120)
        self._zoom_timer.timeout.connect(self._apply_zoom)
        self._pending_zoom = self.thumbnail_cache.thumb_size
        self._notes_timer = QtCore.QTimer(self)
        self._notes_timer.setSingleShot(True)
        self._notes_timer.setInterval(600)
        self._notes_timer.timeout.connect(self._flush_notes)
        self._pending_notes: Dict[str, Tuple[str, str, Optional[str], str]] = {}
        self._pending_selection_key: Optional[str] = None
        self._pending_image_name: Optional[str] = None
        self._about_dialog: Optional[AboutDialog] = None
        self._update_status = "Not checked"
        self._latest_version: Optional[str] = None
        self._update_url: Optional[str] = None
        self._update_tasks: List[UpdateCheckTask] = []
        self._data_version_task: Optional[DataVersionFetchTask] = None
        self._closing = False
        self._compact_toolbar = False
        self._syncing_compact = False
        self._toolbar_full_width = 0

        self._build_ui()
        self._start_data_version_fetch()
        self._apply_dark_theme()
        self._apply_saved_window_state()
        self._update_toolbar_compact_mode()
        self._update_filters()
        self._start_catalog_load()
        if self.config.get("auto_check_updates", True):
            QtCore.QTimer.singleShot(250, self._check_updates_silent)

    def _cache_dir(self) -> Path:
        location = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.CacheLocation)
        return Path(location)

    def _load_cached_data_version(self) -> str:
        cached = str(self.config.get("data_version_override") or "").strip()
        if not cached:
            legacy = str(self.config.get("app_version_override") or "").strip()
            if legacy:
                self.config["data_version_override"] = legacy
                self.config.pop("app_version_override", None)
                save_config(self.config_path, self.config)
                cached = legacy
        return cached or DEFAULT_DATA_VERSION

    def _ensure_user_metadata_files(self) -> None:
        location = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppConfigLocation)
        if not location:
            return
        metadata_dir = Path(location) / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        updated = False
        is_frozen = hasattr(sys, "_MEIPASS")
        for catalog in self.config.get("catalogs", []):
            meta_value = catalog.get("metadata_file")
            if not meta_value:
                continue
            meta_path = Path(meta_value)
            if not meta_path.is_absolute():
                meta_path = (PROJECT_ROOT / meta_path).resolve()
            source_path = self._bundled_metadata_path(catalog.get("name", ""))
            if not source_path:
                source_path = meta_path
            if is_frozen:
                target = metadata_dir / meta_path.name
                if not target.exists() and meta_path.exists():
                    try:
                        shutil.copy2(meta_path, target)
                        updated = True
                    except OSError:
                        continue
                if source_path and target.exists():
                    if self._merge_metadata_updates(source_path, target, catalog.get("name", "")):
                        updated = True
                if metadata_dir not in meta_path.parents:
                    catalog["metadata_file"] = str(target)
                    updated = True
            elif metadata_dir in meta_path.parents:
                if not meta_path.exists() and source_path and source_path.exists():
                    try:
                        shutil.copy2(source_path, meta_path)
                        updated = True
                    except OSError:
                        continue
                if source_path and meta_path.exists():
                    if self._merge_metadata_updates(source_path, meta_path, catalog.get("name", "")):
                        updated = True
        if updated:
            save_config(self.config_path, self.config)

    def _bundled_metadata_path(self, catalog_name: str) -> Optional[Path]:
        name = (catalog_name or "").strip().lower()
        for catalog in DEFAULT_CONFIG.get("catalogs", []):
            if (catalog.get("name") or "").strip().lower() == name:
                meta_value = catalog.get("metadata_file")
                if not meta_value:
                    return None
                return (PROJECT_ROOT / meta_value).resolve()
        return None

    def _merge_metadata_updates(self, source_path: Path, target_path: Path, catalog_name: str) -> bool:
        try:
            source_data = json.loads(source_path.read_text(encoding="utf-8"))
            target_data = json.loads(target_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        source_entries = source_data.get(catalog_name, {})
        target_entries = target_data.get(catalog_name, {})
        if not isinstance(source_entries, dict) or not isinstance(target_entries, dict):
            return False
        updated = False
        fields = {
            "name",
            "type",
            "distance_ly",
            "discoverer",
            "discovery_year",
            "best_months",
            "description",
            "external_link",
            "ra_hours",
            "dec_deg",
            "wiki_thumbnail",
        }
        force_fields = {"description", "external_link", "wiki_thumbnail"}
        for object_id, source_meta in source_entries.items():
            if not isinstance(source_meta, dict):
                continue
            target_meta = target_entries.get(object_id)
            if not isinstance(target_meta, dict):
                target_entries[object_id] = dict(source_meta)
                updated = True
                continue
            for field in fields:
                if field in source_meta and field in force_fields:
                    value = source_meta.get(field)
                    if target_meta.get(field) != value:
                        target_meta[field] = value
                        updated = True
                    continue
                value = source_meta.get(field)
                if value is None or value == "":
                    continue
                if target_meta.get(field) != value:
                    target_meta[field] = value
                    updated = True
        for object_id in list(target_entries.keys()):
            if object_id not in source_entries:
                continue
        if updated:
            target_data[catalog_name] = target_entries
            try:
                target_path.write_text(json.dumps(target_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            except OSError:
                return False
        return updated

    def _cleanup_invalid_image_only_entries(self) -> None:
        catalog_rules = {
            "Messier": re.compile(r"^M\\d+$", re.IGNORECASE),
            "Caldwell": re.compile(r"^C\\d+$", re.IGNORECASE),
            "NGC": re.compile(r"^NGC\\d+$", re.IGNORECASE),
            "IC": re.compile(r"^IC\\d+$", re.IGNORECASE),
        }
        updated = False
        for catalog in self.config.get("catalogs", []):
            name = catalog.get("name")
            rule = catalog_rules.get(name)
            if rule is None:
                continue
            metadata_value = catalog.get("metadata_file")
            if not metadata_value:
                continue
            metadata_path = Path(metadata_value)
            if not metadata_path.is_absolute():
                metadata_path = (PROJECT_ROOT / metadata_path).resolve()
            if not metadata_path.exists():
                continue
            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            catalog_data = data.get(name, {})
            if not isinstance(catalog_data, dict):
                continue
            to_remove = []
            for object_id, entry in catalog_data.items():
                if not isinstance(entry, dict):
                    continue
                if rule.match(str(object_id)):
                    continue
                if entry.get("description") or entry.get("type") or entry.get("distance_ly"):
                    continue
                if entry.get("notes") or entry.get("image_notes") or entry.get("thumbnail"):
                    to_remove.append(object_id)
            if not to_remove:
                continue
            for object_id in to_remove:
                catalog_data.pop(object_id, None)
            data[name] = catalog_data
            try:
                metadata_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                updated = True
            except OSError:
                continue
        if updated:
            save_config(self.config_path, self.config)

    def clear_thumbnail_cache(self) -> bool:
        try:
            self.thumbnail_cache.clear()
            self.model.update_cache(self.thumbnail_cache)
            self._refresh_catalog()
            return True
        except Exception:
            return False

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(0)

        self.toolbar_container = QtWidgets.QWidget()
        toolbar = QtWidgets.QHBoxLayout(self.toolbar_container)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(10)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search by object ID or name")
        self.search.textChanged.connect(self._on_search_changed)
        self.search.setMinimumWidth(280)
        self.search.setMaximumWidth(700)
        self.search.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self._search_min_width = self.search.minimumWidth()
        self._search_max_width = self.search.maximumWidth()
        self.catalog_title = QtWidgets.QLabel("")
        self.catalog_title.setObjectName("catalogTitle")
        self.catalog_title.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
        self.catalog_title.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.catalog_count = QtWidgets.QLabel("")
        self.catalog_count.setObjectName("catalogSummary")
        self.catalog_count.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
        self.catalog_count.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.catalog_summary_container = QtWidgets.QWidget()
        summary_layout = QtWidgets.QVBoxLayout(self.catalog_summary_container)
        summary_layout.setContentsMargins(8, 0, 8, 0)
        summary_layout.setSpacing(0)
        summary_layout.addWidget(self.catalog_title)
        summary_layout.addWidget(self.catalog_count)

        self.controls_container = QtWidgets.QWidget()
        controls_layout = QtWidgets.QHBoxLayout(self.controls_container)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(10)

        self.catalog_filter = QtWidgets.QComboBox()
        self.catalog_filter.currentTextChanged.connect(self._on_catalog_changed)
        self.catalog_filter.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.catalog_filter.setMinimumContentsLength(12)

        self.type_filter = QtWidgets.QComboBox()
        self.type_filter.currentTextChanged.connect(self._on_type_changed)
        self.type_filter.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.type_filter.setMinimumContentsLength(18)

        self.status_filter = QtWidgets.QComboBox()
        self.status_filter.currentTextChanged.connect(self._on_status_changed)
        self.status_filter.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.status_filter.setMinimumContentsLength(12)

        self.zoom_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(80, 360)
        self.zoom_slider.setValue(self.thumbnail_cache.thumb_size)
        self.zoom_slider.valueChanged.connect(self._on_zoom_changed)

        self.wiki_thumbs = QtWidgets.QCheckBox("Wiki thumbnails")
        self.wiki_thumbs.setChecked(bool(self.config.get("use_wiki_thumbnails", False)))
        self.wiki_thumbs.toggled.connect(self._on_wiki_thumbs_toggled)
        self.refresh_button = QtWidgets.QPushButton("Refresh")
        self.refresh_button.clicked.connect(self._refresh_catalog)
        self.settings_button = QtWidgets.QPushButton("Settings")
        self.settings_button.clicked.connect(self._open_settings)
        self.about_button = QtWidgets.QPushButton("About")
        self.about_button.clicked.connect(self._open_about)

        self.compact_filters_container = QtWidgets.QWidget()
        compact_filters_layout = QtWidgets.QHBoxLayout(self.compact_filters_container)
        compact_filters_layout.setContentsMargins(0, 0, 0, 0)
        compact_filters_layout.setSpacing(8)

        self.compact_toolbar_catalog_button = QtWidgets.QToolButton()
        self.compact_toolbar_catalog_button.setText("Catalogue")
        self.compact_toolbar_catalog_button.setToolTip("Catalogue")
        self.compact_toolbar_catalog_button.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.compact_toolbar_catalog_button.setProperty("compactPill", True)
        self.compact_toolbar_catalog_menu = QtWidgets.QMenu(self)
        self.compact_toolbar_catalog_button.setMenu(self.compact_toolbar_catalog_menu)

        self.compact_toolbar_type_button = QtWidgets.QToolButton()
        self.compact_toolbar_type_button.setText("Object Type")
        self.compact_toolbar_type_button.setToolTip("Object Type")
        self.compact_toolbar_type_button.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.compact_toolbar_type_button.setProperty("compactPill", True)
        self.compact_toolbar_type_menu = QtWidgets.QMenu(self)
        self.compact_toolbar_type_button.setMenu(self.compact_toolbar_type_menu)

        self.compact_toolbar_status_button = QtWidgets.QToolButton()
        self.compact_toolbar_status_button.setText("Status")
        self.compact_toolbar_status_button.setToolTip("Status")
        self.compact_toolbar_status_button.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.compact_toolbar_status_button.setProperty("compactPill", True)
        self.compact_toolbar_status_menu = QtWidgets.QMenu(self)
        self.compact_toolbar_status_button.setMenu(self.compact_toolbar_status_menu)

        pill_label = "Object Type"
        pill_width = QtGui.QFontMetrics(self.compact_toolbar_type_button.font()).horizontalAdvance(
            pill_label
        ) + 32
        for button in (
            self.compact_toolbar_catalog_button,
            self.compact_toolbar_type_button,
            self.compact_toolbar_status_button,
        ):
            button.setFixedWidth(pill_width)
            button.setFixedHeight(32)

        compact_filters_layout.addWidget(self.compact_toolbar_catalog_button)
        compact_filters_layout.addWidget(self.compact_toolbar_type_button)
        compact_filters_layout.addWidget(self.compact_toolbar_status_button)
        self.compact_filters_container.setVisible(False)

        self.toolbar_right_container = QtWidgets.QWidget()
        right_layout = QtWidgets.QHBoxLayout(self.toolbar_right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        toolbar.addWidget(self.search)
        toolbar.addStretch(1)
        toolbar.addWidget(self.catalog_summary_container)
        toolbar.addStretch(1)
        self.catalog_label = QtWidgets.QLabel("Catalog")
        self.type_label = QtWidgets.QLabel("Object Type")
        self.status_filter_label = QtWidgets.QLabel("Status")
        controls_layout.addWidget(self.catalog_label)
        controls_layout.addWidget(self.catalog_filter)
        controls_layout.addSpacing(6)
        controls_layout.addWidget(self.type_label)
        controls_layout.addWidget(self.type_filter)
        controls_layout.addSpacing(6)
        controls_layout.addWidget(self.status_filter_label)
        controls_layout.addWidget(self.status_filter)
        controls_layout.addWidget(self.settings_button)
        controls_layout.addWidget(self.about_button)
        right_layout.addWidget(self.controls_container)
        right_layout.addWidget(self.compact_filters_container)
        toolbar.addWidget(self.toolbar_right_container)

        self.grid_controls_container = QtWidgets.QWidget()
        grid_controls_layout = QtWidgets.QHBoxLayout(self.grid_controls_container)
        grid_controls_layout.setContentsMargins(0, 0, 0, 0)
        grid_controls_layout.setSpacing(10)
        self.zoom_label = QtWidgets.QLabel("Zoom")
        grid_controls_layout.addWidget(self.zoom_label)
        grid_controls_layout.addWidget(self.zoom_slider)
        grid_controls_layout.addWidget(self.wiki_thumbs)
        grid_controls_layout.addWidget(self.refresh_button)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setObjectName("statusLabel")

        layout.addWidget(self.toolbar_container)
        layout.addSpacing(3)
        layout.addWidget(self.grid_controls_container)
        layout.addSpacing(6)
        layout.addWidget(self.status_label)
        layout.addSpacing(6)

        self.grid = QtWidgets.QListView()
        self.grid.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(0)
        self.grid.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._update_grid_metrics(self.thumbnail_cache.thumb_size)
        self.grid.setItemDelegate(CatalogItemDelegate(self.grid))
        self.grid.setStyleSheet(
            "QListView::item { margin: 0px; padding: 0px; border: 1px solid #3a3a3a; }"
        )
        self.grid.setModel(self.proxy)
        self.grid.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.grid.viewport().installEventFilter(self)

        self.detail = DetailPanel()
        self.detail.connect_notes_changed(self._on_notes_changed)
        self.detail.thumbnail_selected.connect(self._on_thumbnail_selected)
        self.detail.image_changed.connect(self._on_image_changed)
        self.detail.archive_requested.connect(self._on_archive_requested)
        self.model.wiki_thumbnail_loaded.connect(self._on_wiki_thumbnail_loaded)

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(self.grid)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)
        splitter.splitterMoved.connect(self._schedule_auto_fit)
        self.splitter = splitter

        layout.addWidget(splitter, stretch=1)

        footer = QtWidgets.QHBoxLayout()
        support = QtWidgets.QLabel(
            'Support development: <a href="https://buymeacoffee.com/PaulSpinelli">buymeacoffee.com/PaulSpinelli</a> | '
            '<a href="https://www.paypal.com/donate/?hosted_button_id=9GDUBHS78MH52">paypal.com/donate</a>'
        )
        support.setOpenExternalLinks(True)
        support.setObjectName("supportLink")
        footer.addWidget(support)
        footer.addStretch(1)
        layout.addLayout(footer)
        self.setCentralWidget(central)
        self._toolbar_full_width = self.toolbar_container.sizeHint().width()
        self._sync_grid_controls_width()

    def _apply_dark_theme(self) -> None:
        self.setStyleSheet(
            """
            QWidget { background: #141414; color: #e5e5e5; font-family: 'Avenir Next', 'Helvetica Neue', Arial; }
            QLineEdit, QComboBox, QTextEdit { background: #1d1d1d; border: 1px solid #333; padding: 6px; }
            QToolButton[compactPill="true"] { background: #212121; border: 1px solid #3b3b3b; border-radius: 16px; padding: 4px 22px 4px 12px; }
            QToolButton[compactPill="true"]:hover { background: #2a2a2a; }
            QToolButton[compactPill="true"]::menu-indicator { image: none; }
            QListView { background: #101010; border: 1px solid #2a2a2a; }
            QSplitter::handle { background: #1f1f1f; }
            QSplitter::handle:horizontal { width: 6px; }
            QSplitter::handle:vertical { height: 6px; }
            QLabel#detailTitle { font-size: 20px; font-weight: 600; }
            QLabel#catalogTitle { font-size: 18px; font-weight: 600; color: #d9a441; }
            QLabel#welcomeTitle { font-size: 20px; font-weight: 600; }
            QLabel#aboutTitle { font-size: 22px; font-weight: 600; }
            QLabel#aboutVersion { color: #bcbcbc; }
            QLabel#aboutSectionTitle { font-size: 16px; font-weight: 600; }
            QLabel#aboutUpdateStatus a { color: #d9a441; text-decoration: none; }
            QTextBrowser#welcomeBody { background: #101010; border: 1px solid #2a2a2a; }
            QLabel#statusLabel { color: #d9a441; padding: 4px 0; }
            QLabel#coordLabel { color: #bcbcbc; padding: 4px 0; }
            QLabel#supportLink { color: #bcbcbc; }
            QLabel#supportLink a { color: #d9a441; text-decoration: none; }
            QLabel#externalLink a { color: #8ab4f8; text-decoration: none; }
            QTextEdit#descriptionBox { background: #0f0f0f; }
            QTextEdit#notesBox { background: #101417; }
            QMenu { background: #1b1b1b; border: 1px solid #333; }
            QMenu::item { padding: 6px 14px; }
            QMenu::item:selected { background: #2f2f2f; color: #ffffff; }
            QPushButton { background: #2c2c2c; border: 1px solid #3b3b3b; padding: 6px 12px; }
            QPushButton:hover { background: #3a3a3a; }
            QSlider::groove:horizontal { height: 6px; background: #2a2a2a; }
            QSlider::handle:horizontal { width: 14px; background: #d9a441; margin: -4px 0; border-radius: 7px; }
            """
        )

    def _sync_compact_combo_items(self, source: QtWidgets.QComboBox, target: QtWidgets.QComboBox) -> None:
        target.blockSignals(True)
        target.clear()
        for index in range(source.count()):
            target.addItem(source.itemText(index))
        target.setCurrentText(source.currentText())
        target.blockSignals(False)

    def _sync_compact_toolbar_menu(
        self,
        source: QtWidgets.QComboBox,
        menu: QtWidgets.QMenu,
        current_value: str,
        handler: Callable[[str], None],
    ) -> None:
        menu.clear()
        for index in range(source.count()):
            text = source.itemText(index)
            action = QtGui.QAction(text, menu)
            action.setCheckable(True)
            action.setChecked(text == current_value)
            action.triggered.connect(lambda _checked=False, value=text: handler(value))
            menu.addAction(action)

    def _sync_compact_filters(self) -> None:
        if self._syncing_compact:
            return
        if hasattr(self, "compact_catalog_filter"):
            self._sync_compact_combo_items(self.catalog_filter, self.compact_catalog_filter)
            self._sync_compact_combo_items(self.type_filter, self.compact_type_filter)
            self._sync_compact_combo_items(self.status_filter, self.compact_status_filter)
        if hasattr(self, "compact_toolbar_catalog_button"):
            self._sync_compact_toolbar_menu(
                self.catalog_filter,
                self.compact_toolbar_catalog_menu,
                self.catalog_filter.currentText(),
                self._on_compact_toolbar_catalog_changed,
            )
            self._sync_compact_toolbar_menu(
                self.type_filter,
                self.compact_toolbar_type_menu,
                self.type_filter.currentText(),
                self._on_compact_toolbar_type_changed,
            )
            self._sync_compact_toolbar_menu(
                self.status_filter,
                self.compact_toolbar_status_menu,
                self.status_filter.currentText(),
                self._on_compact_toolbar_status_changed,
            )
        self._sync_compact_state()
        self._sync_compact_toolbar_spacing()

    def _sync_compact_state(self) -> None:
        if not hasattr(self, "compact_zoom_slider"):
            return
        self.compact_zoom_slider.blockSignals(True)
        self.compact_zoom_slider.setValue(self.zoom_slider.value())
        self.compact_zoom_slider.blockSignals(False)
        self.compact_wiki_thumbs.blockSignals(True)
        self.compact_wiki_thumbs.setChecked(self.wiki_thumbs.isChecked())
        self.compact_wiki_thumbs.blockSignals(False)

    def _set_toolbar_compact(self, compact: bool) -> None:
        if compact == self._compact_toolbar:
            return
        self._compact_toolbar = compact
        self.controls_container.setVisible(not compact)
        if hasattr(self, "compact_filters_container"):
            self.compact_filters_container.setVisible(compact)
        if compact:
            self._sync_compact_filters()

    def _sync_compact_toolbar_spacing(self) -> None:
        if not hasattr(self, "toolbar_right_container"):
            return
        if self._compact_toolbar:
            search_width = self.search.width() or self.search.sizeHint().width()
            right_width = self.toolbar_right_container.sizeHint().width()
            target_width = min(search_width, right_width) if right_width else search_width
            if target_width:
                self.search.setMinimumWidth(target_width)
                self.search.setMaximumWidth(target_width)
                self.toolbar_right_container.setFixedWidth(target_width)
        else:
            self.search.setMinimumWidth(self._search_min_width)
            self.search.setMaximumWidth(self._search_max_width)
            self.toolbar_right_container.setMinimumWidth(0)
            self.toolbar_right_container.setMaximumWidth(16777215)

    def _update_toolbar_compact_mode(self) -> None:
        if not self._toolbar_full_width:
            self._toolbar_full_width = self.toolbar_container.sizeHint().width()
        if self.toolbar_container.width() == 0:
            return
        should_compact = self.toolbar_container.width() < self._toolbar_full_width
        self._set_toolbar_compact(should_compact)
        self._sync_compact_toolbar_spacing()

    def _apply_saved_window_state(self) -> None:
        state = self._saved_state or {}
        size = state.get("window_size")
        if isinstance(size, list) and len(size) == 2:
            self.resize(int(size[0]), int(size[1]))
        else:
            self.resize(1400, 900)
        splitter_sizes = state.get("splitter_sizes")
        if isinstance(splitter_sizes, list) and splitter_sizes:
            self.splitter.setSizes([int(value) for value in splitter_sizes])

    def _update_filters(self) -> None:
        catalogs = {item.catalog for item in self.items}
        configured = {c.get("name") for c in self.config.get("catalogs", []) if c.get("name")}
        catalogs = sorted(catalogs | configured)
        current_catalog = self.catalog_filter.currentText() if self.catalog_filter.count() else ""
        self.catalog_filter.blockSignals(True)
        self.catalog_filter.clear()
        self.catalog_filter.addItem("All")
        self.catalog_filter.addItems([self._catalog_display_name(name) for name in catalogs])
        if current_catalog:
            self.catalog_filter.setCurrentText(current_catalog)
        self.catalog_filter.blockSignals(False)
        self.catalog_filter.view().setMinimumWidth(160)

        self._update_type_filter(current_catalog)

        current_status = self.status_filter.currentText() if self.status_filter.count() else ""
        self.status_filter.blockSignals(True)
        self.status_filter.clear()
        self.status_filter.addItem("All")
        self.status_filter.addItems(["Captured", "Missing", "Suggested"])
        if current_status:
            self.status_filter.setCurrentText(current_status)
        self.status_filter.blockSignals(False)
        self.status_filter.view().setMinimumWidth(160)
        self._update_catalog_summary()
        self._sync_compact_filters()

    def _refresh_catalog(self) -> None:
        self.config = load_config(self.config_path)
        if self._zoom_timer.isActive():
            self._zoom_timer.stop()
        self._start_catalog_load()

    def _on_selection_changed(self) -> None:
        self._flush_notes()
        indexes = self.grid.selectionModel().selectedIndexes()
        if not indexes:
            self.detail.update_item(None)
            return
        source_index = self.proxy.mapToSource(indexes[0])
        item = self.model.data(source_index, QtCore.Qt.ItemDataRole.UserRole)
        self.detail.update_item(item)
        if item and not item.image_paths:
            pixmap = self.model.get_wiki_pixmap(item.unique_key)
            if pixmap:
                self.detail.set_wiki_pixmap(pixmap)
        if item:
            self._notes_timer.start()

    def _on_image_changed(self, _image_name: str) -> None:
        self._flush_notes()

    def _on_catalog_changed(self, value: str) -> None:
        if value == "All":
            self.proxy.set_catalog_filter("")
        else:
            self.proxy.set_catalog_filter(self._catalog_internal_name(value))
        self._update_type_filter(value)
        self._update_catalog_summary()
        self._schedule_auto_fit()
        if not self._syncing_compact:
            self._sync_compact_filters()

    def _on_type_changed(self, value: str) -> None:
        if value == "All":
            self.proxy.set_type_filter("")
        else:
            self.proxy.set_type_filter(value)
        self._schedule_auto_fit()
        if not self._syncing_compact:
            self._sync_compact_filters()

    def _on_status_changed(self, value: str) -> None:
        if value == "All":
            self.proxy.set_status_filter("")
        else:
            self.proxy.set_status_filter(value)
        self._schedule_auto_fit()
        if not self._syncing_compact:
            self._sync_compact_filters()

    def _on_search_changed(self, text: str) -> None:
        self.proxy.set_search_text(text)
        self._schedule_auto_fit()

    def _on_compact_catalog_changed(self, value: str) -> None:
        if self._syncing_compact:
            return
        self._syncing_compact = True
        try:
            self.catalog_filter.setCurrentText(value)
        finally:
            self._syncing_compact = False

    def _on_compact_toolbar_catalog_changed(self, value: str) -> None:
        if self._syncing_compact:
            return
        self._syncing_compact = True
        try:
            self.catalog_filter.setCurrentText(value)
        finally:
            self._syncing_compact = False

    def _on_compact_type_changed(self, value: str) -> None:
        if self._syncing_compact:
            return
        self._syncing_compact = True
        try:
            self.type_filter.setCurrentText(value)
        finally:
            self._syncing_compact = False

    def _on_compact_toolbar_type_changed(self, value: str) -> None:
        if self._syncing_compact:
            return
        self._syncing_compact = True
        try:
            self.type_filter.setCurrentText(value)
        finally:
            self._syncing_compact = False

    def _on_compact_status_changed(self, value: str) -> None:
        if self._syncing_compact:
            return
        self._syncing_compact = True
        try:
            self.status_filter.setCurrentText(value)
        finally:
            self._syncing_compact = False

    def _on_compact_toolbar_status_changed(self, value: str) -> None:
        if self._syncing_compact:
            return
        self._syncing_compact = True
        try:
            self.status_filter.setCurrentText(value)
        finally:
            self._syncing_compact = False

    def _on_compact_zoom_changed(self, value: int) -> None:
        if self._syncing_compact:
            return
        self._syncing_compact = True
        try:
            self.zoom_slider.setValue(value)
        finally:
            self._syncing_compact = False

    def _on_compact_wiki_toggled(self, enabled: bool) -> None:
        if self._syncing_compact:
            return
        self._syncing_compact = True
        try:
            self.wiki_thumbs.setChecked(enabled)
        finally:
            self._syncing_compact = False

    def _update_catalog_summary(self) -> None:
        current = self.catalog_filter.currentText()
        if current == "All" or not current:
            filtered = self.items
            title = "All Catalogues"
        else:
            internal = self._catalog_internal_name(current)
            filtered = [item for item in self.items if item.catalog == internal]
            title = internal
        total = len(filtered)
        captured = sum(1 for item in filtered if item.image_paths)
        suffix = "" if title == "All Catalogues" else " Catalogue"
        if total:
            self.catalog_title.setText(self._catalog_title_text(title, suffix))
            self.catalog_count.setText(f"{captured}/{total} captured")
        else:
            self.catalog_title.setText(self._catalog_title_text(title, suffix))
            self.catalog_count.setText("0/0 captured")

    @staticmethod
    def _catalog_display_name(name: str) -> str:
        if name in {"IC"}:
            return f"{name} (In progress)"
        return name

    @staticmethod
    def _catalog_title_text(title: str, suffix: str) -> str:
        if title in {"IC"}:
            return f"{title}{suffix} (In progress)"
        return f"{title}{suffix}"

    @staticmethod
    def _catalog_internal_name(display_name: str) -> str:
        return display_name.replace(" (In progress)", "")

    def _update_type_filter(self, catalog_value: str) -> None:
        current_type = self.type_filter.currentText() if self.type_filter.count() else ""
        internal = self._catalog_internal_name(catalog_value) if catalog_value else ""
        if internal and internal != "All":
            filtered = [item for item in self.items if item.catalog == internal]
            types = collect_object_types(filtered)
        else:
            types = collect_object_types(self.items)
        self.type_filter.blockSignals(True)
        self.type_filter.clear()
        self.type_filter.addItem("All")
        self.type_filter.addItems(types)
        if current_type and current_type in {"All", *types}:
            self.type_filter.setCurrentText(current_type)
        self.type_filter.blockSignals(False)
        self.type_filter.view().setMinimumWidth(220)
        self._sync_compact_filters()

    def _on_zoom_changed(self, value: int) -> None:
        self._auto_fit_enabled = False
        self._pending_zoom = value
        self._zoom_timer.start()
        if not self._syncing_compact:
            self._sync_compact_state()

    def _apply_zoom(self) -> None:
        value = self._pending_zoom
        self._update_grid_metrics(value)
        self.config["thumb_size"] = value
        save_config(self.config_path, self.config)
        self.thumbnail_cache = ThumbnailCache(self._cache_dir(), value)
        self.model.update_cache(self.thumbnail_cache)
        self._schedule_view_refresh()

    def _schedule_auto_fit(self) -> None:
        if self._suppress_auto_fit:
            self._schedule_view_refresh()
            return
        if self._auto_fit_enabled:
            self._auto_fit_timer.start()
        else:
            self._schedule_view_refresh()

    def _auto_fit_thumbnails(self) -> None:
        if not self._auto_fit_enabled:
            return
        item_count = self.proxy.rowCount()
        if item_count <= 0:
            return
        width = self.grid.viewport().width()
        height = self.grid.viewport().height()
        if width <= 0 or height <= 0:
            return
        spacing = self.grid.spacing()
        grid_extra = 2
        min_size = 60
        max_size = max(min(width, height), min_size)
        best = min_size
        best_gap = width

        max_columns = min(item_count, max(1, width // min_size))
        for columns in range(1, max_columns + 1):
            rows = (item_count + columns - 1) // columns
            grid_size_w = (width + spacing) // columns - spacing
            grid_size_h = (height + spacing) // rows - spacing
            grid_size = min(grid_size_w, grid_size_h)
            size = grid_size - grid_extra
            if size < min_size:
                continue
            if size > max_size:
                size = max_size
            grid_size = size + grid_extra
            used_width = max(0, columns * (grid_size + spacing) - spacing)
            gap = max(0, width - used_width)
            if gap < best_gap or (gap == best_gap and size > best):
                best = size
                best_gap = gap
        self.grid.setIconSize(QtCore.QSize(best, best))
        self._update_grid_metrics(best)
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(best)
        self.zoom_slider.blockSignals(False)
        self.config["thumb_size"] = best
        save_config(self.config_path, self.config)
        self.thumbnail_cache = ThumbnailCache(self._cache_dir(), best)
        self.model.update_cache(self.thumbnail_cache)
        self._schedule_view_refresh()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_toolbar_compact_mode()
        self._sync_grid_controls_width()
        self._schedule_auto_fit()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        self._update_toolbar_compact_mode()
        self._sync_grid_controls_width()
        QtCore.QTimer.singleShot(0, self._sync_grid_controls_width)
        if self._suppress_auto_fit:
            QtCore.QTimer.singleShot(250, self._disable_startup_auto_fit)

    def _disable_startup_auto_fit(self) -> None:
        self._suppress_auto_fit = False

    def _sync_grid_controls_width(self) -> None:
        if not hasattr(self, "grid_controls_container"):
            return
        if not hasattr(self, "search"):
            return
        target_width = self.search.width()
        if not target_width:
            target_width = self.search.sizeHint().width()
        if target_width:
            self.grid_controls_container.setFixedWidth(target_width)

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if obj is self.grid.viewport() and event.type() == QtCore.QEvent.Type.Resize:
            self._schedule_auto_fit()
        return super().eventFilter(obj, event)

    def _schedule_view_refresh(self) -> None:
        if self._loading:
            return
        QtCore.QTimer.singleShot(0, self._refresh_view)

    def _refresh_view(self) -> None:
        if self._loading:
            return
        self.grid.doItemsLayout()
        self.grid.viewport().update()

    def _open_settings(self) -> None:
        base_config = self.config
        dialog = SettingsDialog(self.config, self)
        dialog.previewChanged.connect(self._preview_settings_changed)
        result = dialog.exec()
        if result != QtWidgets.QDialog.DialogCode.Accepted:
            if self._preview_active:
                self._preview_active = False
                self._start_catalog_load(base_config)
            return
        self.config = dialog.updated_config
        save_config(self.config_path, self.config)
        self.thumbnail_cache = ThumbnailCache(self._cache_dir(), self.config.get("thumb_size", 240))
        self._auto_fit_enabled = True
        self._start_catalog_load()
        self._preview_active = False

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._closing = True
        SHUTDOWN_EVENT.set()
        self._thread_pool.clear()
        self._thread_pool.waitForDone(1500)
        self._persist_ui_state()
        super().closeEvent(event)

    def _start_catalog_load(self, config_override: Optional[Dict] = None) -> None:
        config = config_override or self.config
        if self._loading:
            self._pending_reload = True
            self._pending_config = config
            return
        if self._zoom_timer.isActive():
            self._zoom_timer.stop()
        self._loading = True
        self._loading_config = config
        self._set_ui_enabled(False)
        self.status_label.setText("Loading catalog…")
        task = CatalogLoadTask(config)
        task.signals.loaded.connect(self._on_catalog_loaded)
        self._catalog_pool.start(task)

    def _on_catalog_loaded(self, items: List[CatalogItem]) -> None:
        self.items = items
        self.model.set_items(self.items)
        wiki_enabled = bool(self._loading_config.get("use_wiki_thumbnails", False))
        self.model.set_wiki_thumbnails_enabled(wiki_enabled)
        self._update_filters()
        if not self._saved_state_applied:
            self._apply_saved_filters()
            self._saved_state_applied = True
        self._schedule_view_refresh()
        self.status_label.setText("")
        self._loading = False
        self._set_ui_enabled(True)
        if self._pending_selection_key:
            QtCore.QTimer.singleShot(150, self._restore_pending_selection)
        else:
            QtCore.QTimer.singleShot(150, self._select_first_item)
        if self._pending_reload:
            pending = self._pending_config
            self._pending_reload = False
            self._pending_config = None
            self._start_catalog_load(pending)

    def _select_first_item(self) -> None:
        if self.grid.selectionModel().hasSelection():
            return
        if self.proxy.rowCount() == 0:
            return
        index = self.proxy.index(0, 0)
        if not index.isValid():
            return
        self.grid.setCurrentIndex(index)
        self.grid.selectionModel().select(
            index, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect
        )

    def _restore_pending_selection(self) -> None:
        key = self._pending_selection_key
        image_name = self._pending_image_name
        self._pending_selection_key = None
        self._pending_image_name = None
        if not key:
            self._select_first_item()
            return
        source_index = self.model.index_for_key(key)
        if source_index is None or not source_index.isValid():
            self._select_first_item()
            return
        proxy_index = self.proxy.mapFromSource(source_index)
        if not proxy_index.isValid():
            self._select_first_item()
            return
        self.grid.setCurrentIndex(proxy_index)
        self.grid.selectionModel().select(
            proxy_index, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect
        )
        if image_name:
            QtCore.QTimer.singleShot(0, lambda: self.detail.set_current_image_by_name(image_name))

    def _update_grid_metrics(self, size: int) -> None:
        self.grid.setIconSize(QtCore.QSize(size, size))
        self.grid.setGridSize(QtCore.QSize(size + 2, size + 2))

    def _set_ui_enabled(self, enabled: bool) -> None:
        self.search.setEnabled(enabled)
        self.catalog_filter.setEnabled(enabled)
        self.type_filter.setEnabled(enabled)
        self.status_filter.setEnabled(enabled)
        self.zoom_slider.setEnabled(enabled)
        self.grid.setEnabled(enabled)
        self.wiki_thumbs.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
        self.settings_button.setEnabled(enabled)
        self.about_button.setEnabled(enabled)
        if hasattr(self, "compact_catalog_filter"):
            self.compact_catalog_filter.setEnabled(enabled)
            self.compact_type_filter.setEnabled(enabled)
            self.compact_status_filter.setEnabled(enabled)
            self.compact_zoom_slider.setEnabled(enabled)
            self.compact_wiki_thumbs.setEnabled(enabled)
            self.compact_refresh_button.setEnabled(enabled)
            self.compact_settings_button.setEnabled(enabled)
            self.compact_about_button.setEnabled(enabled)
        if hasattr(self, "compact_toolbar_catalog_button"):
            self.compact_toolbar_catalog_button.setEnabled(enabled)
            self.compact_toolbar_type_button.setEnabled(enabled)
            self.compact_toolbar_status_button.setEnabled(enabled)

    def _preview_settings_changed(self, config: Dict) -> None:
        self._preview_active = True
        self._start_catalog_load(config)

    def _on_notes_changed(self) -> None:
        if self.detail.notes_blocked():
            return
        item = self.detail.current_item()
        if item is None:
            return
        image_name = self.detail.current_image_name()
        note_key = f"{item.unique_key}::{image_name or ''}"
        self._pending_notes[note_key] = (item.catalog, item.object_id, image_name, self.detail.current_notes())
        self._notes_timer.start()

    def _on_thumbnail_selected(self, catalog: str, object_id: str, thumbnail_name: str) -> None:
        metadata_path = resolve_metadata_path(self.config, catalog)
        if metadata_path is None:
            return
        save_thumbnail(metadata_path, catalog, object_id, thumbnail_name)
        item = self.detail.current_item()
        if item:
            self.model.update_item_thumbnail(item.unique_key, thumbnail_name)

    def _on_wiki_thumbs_toggled(self, enabled: bool) -> None:
        self.config["use_wiki_thumbnails"] = bool(enabled)
        save_config(self.config_path, self.config)
        self.model.set_wiki_thumbnails_enabled(bool(enabled))
        current = self.detail.current_item()
        if current and not current.image_paths and not enabled:
            self.detail.update_item(current)
        self._schedule_view_refresh()
        if not self._syncing_compact:
            self._sync_compact_state()

    def _open_about(self) -> None:
        if self._about_dialog and self._about_dialog.isVisible():
            self._about_dialog.raise_()
            self._about_dialog.activateWindow()
            return
        dialog = AboutDialog(self.config, APP_VERSION, self._data_version, self)
        dialog.check_updates_requested.connect(self._check_updates_user)
        dialog.auto_check_toggled.connect(self._set_auto_check_updates)
        dialog.set_update_status(self._update_status, self._latest_version, self._update_url)
        self._about_dialog = dialog
        dialog.finished.connect(lambda _result: self._clear_about_dialog())
        dialog.show()

    def _clear_about_dialog(self) -> None:
        self._about_dialog = None

    def _set_auto_check_updates(self, enabled: bool) -> None:
        self.config["auto_check_updates"] = bool(enabled)
        save_config(self.config_path, self.config)

    def _start_data_version_fetch(self) -> None:
        if self._data_version_task is not None:
            return
        task = DataVersionFetchTask(DATA_VERSION_URL)
        task.signals.loaded.connect(self._apply_remote_data_version)
        task.signals.failed.connect(self._data_version_fetch_failed)
        self._data_version_task = task
        self._thread_pool.start(task)

    def _apply_remote_data_version(self, version: str) -> None:
        self._data_version_task = None
        if not version or version == self._data_version:
            return
        self._data_version = version
        self.config["data_version_override"] = version
        save_config(self.config_path, self.config)
        if self._about_dialog:
            self._about_dialog.set_data_version(version)

    def _data_version_fetch_failed(self, _message: str) -> None:
        self._data_version_task = None

    def _check_updates_silent(self) -> None:
        self._start_update_check(silent=True)

    def _check_updates_user(self) -> None:
        self._start_update_check(silent=False)

    def _start_update_check(self, silent: bool) -> None:
        task = UpdateCheckTask(APP_VERSION)
        self._update_tasks.append(task)
        task.signals.available.connect(lambda tag, url: self._on_update_available(tag, url, silent))
        task.signals.up_to_date.connect(lambda tag: self._on_update_uptodate(tag, silent))
        task.signals.failed.connect(lambda message: self._on_update_failed(message, silent))
        task.signals.finished.connect(lambda: self._discard_update_task(task))
        self._thread_pool.start(task)

    def _discard_update_task(self, task: UpdateCheckTask) -> None:
        if task in self._update_tasks:
            self._update_tasks.remove(task)

    def _on_update_available(self, tag: str, url: str, silent: bool) -> None:
        if self._closing:
            return
        self._update_status = f"Update available: {tag}"
        self._latest_version = tag
        self._update_url = url
        if self._about_dialog:
            self._about_dialog.set_update_status(self._update_status, self._latest_version, self._update_url)
        elif not silent:
            QtWidgets.QMessageBox.information(self, "Update available", f"New version available: {tag}")

    def _on_update_uptodate(self, tag: str, silent: bool) -> None:
        if self._closing:
            return
        self._update_status = f"Up to date ({tag})"
        self._latest_version = tag
        self._update_url = None
        if self._about_dialog:
            self._about_dialog.set_update_status(self._update_status, self._latest_version, self._update_url)
        elif not silent:
            QtWidgets.QMessageBox.information(self, "Up to date", f"You're on the latest version ({tag}).")

    def _on_update_failed(self, message: str, silent: bool) -> None:
        if self._closing:
            return
        self._update_status = message
        self._latest_version = None
        self._update_url = None
        if self._about_dialog:
            self._about_dialog.set_update_status(self._update_status, self._latest_version, self._update_url)
        elif not silent:
            QtWidgets.QMessageBox.warning(self, "Update check failed", message)

    def _on_wiki_thumbnail_loaded(self, item_key: str, pixmap: QtGui.QPixmap) -> None:
        current = self.detail.current_item()
        if not current or current.unique_key != item_key:
            return
        if current.image_paths:
            return
        self.detail.set_wiki_pixmap(pixmap)

    @staticmethod
    def _next_image_name(item: CatalogItem, current_name: Optional[str]) -> Optional[str]:
        if not current_name or not item.image_paths:
            return None
        names = [path.name for path in item.image_paths]
        if current_name not in names:
            return None
        if len(names) == 1:
            return None
        index = names.index(current_name)
        if index + 1 < len(names):
            return names[index + 1]
        if index > 0:
            return names[index - 1]
        return None

    def _on_archive_requested(self, path_value: str) -> None:
        self._flush_notes()
        archive_dir = (self.config.get("archive_image_dir") or "").strip()
        if not archive_dir:
            choice = QtWidgets.QMessageBox.question(
                self,
                "Archive folder not set",
                "Set an Archive Image Folder in Settings to enable archiving.",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
            )
            if choice == QtWidgets.QMessageBox.StandardButton.Yes:
                self._open_settings()
            return

        path = Path(path_value)
        if not path.exists():
            QtWidgets.QMessageBox.warning(
                self,
                "Image not found",
                "The selected image no longer exists on disk.",
            )
            return

        archive_root = Path(archive_dir).expanduser()
        if not archive_root.is_absolute():
            archive_root = (PROJECT_ROOT / archive_root).resolve()
        else:
            archive_root = archive_root.resolve()
        source_dir = path.parent.resolve()
        if archive_root == source_dir:
            QtWidgets.QMessageBox.warning(
                self,
                "Archive folder invalid",
                "The archive folder is the same as the image folder.",
            )
            return
        archive_inside_scanned = []
        master_dir = (self.config.get("master_image_dir") or "").strip()
        if master_dir:
            master_path = Path(master_dir).expanduser()
            if not master_path.is_absolute():
                master_path = (PROJECT_ROOT / master_path).resolve()
            else:
                master_path = master_path.resolve()
            if archive_root == master_path or archive_root.is_relative_to(master_path):
                archive_inside_scanned.append(str(master_path))
        for catalog in self.config.get("catalogs", []):
            for image_dir in catalog.get("image_dirs", []):
                if not image_dir:
                    continue
                image_path = Path(image_dir).expanduser()
                if not image_path.is_absolute():
                    image_path = (PROJECT_ROOT / image_path).resolve()
                else:
                    image_path = image_path.resolve()
                if archive_root == image_path or archive_root.is_relative_to(image_path):
                    archive_inside_scanned.append(str(image_path))
        if archive_inside_scanned:
            choice = QtWidgets.QMessageBox.question(
                self,
                "Archive folder inside image library",
                "The archive folder is inside a scanned image folder, so archived files may still appear.\n\n"
                "Continue anyway?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
            )
            if choice != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        archive_root.mkdir(parents=True, exist_ok=True)

        stat = path.stat()
        size = self._format_bytes(stat.st_size)
        modified = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        target = self._next_available_path(archive_root / path.name)

        confirm = QtWidgets.QMessageBox.question(
            self,
            "Archive image",
            (
                "Move this image to the archive folder?\n\n"
                f"File: {path.name}\n"
                f"Size: {size}\n"
                f"Modified: {modified}\n"
                f"From: {path}\n"
                f"To: {target}"
            ),
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        try:
            shutil.move(str(path), str(target))
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self,
                "Archive failed",
                f"Unable to move the image.\n\n{exc}",
            )
            return
        if path.exists():
            QtWidgets.QMessageBox.warning(
                self,
                "Archive incomplete",
                "The file still exists at the original location after moving.",
            )

        self.status_label.setText(f"Archived {path.name}")
        current_item = self.detail.current_item()
        if current_item:
            current_image = self.detail.current_image_name()
            self._pending_selection_key = current_item.unique_key
            self._pending_image_name = self._next_image_name(current_item, current_image)
        self._start_catalog_load()

    def _flush_notes(self) -> None:
        if not self._pending_notes:
            return
        pending = list(self._pending_notes.values())
        self._pending_notes.clear()
        current = self.detail.current_item()
        for catalog, object_id, image_name, notes in pending:
            metadata_path = resolve_metadata_path(self.config, catalog)
            if metadata_path is None:
                continue
            item_key = f"{catalog}:{object_id}"
            if image_name:
                save_image_note(metadata_path, catalog, object_id, image_name, notes)
                self.model.update_item_image_note(item_key, image_name, notes)
                if current and current.unique_key == item_key:
                    self.detail.update_current_item_notes(image_name, notes)
            else:
                save_note(metadata_path, catalog, object_id, notes)
                self.model.update_item_notes(item_key, notes)
                if current and current.unique_key == item_key:
                    self.detail.update_current_item_notes(None, None, notes)

    def _apply_saved_filters(self) -> None:
        state = self._saved_state or {}
        filters = state.get("filters", {})
        search = state.get("search", "")

        catalog = filters.get("catalog", "")
        if not catalog:
            catalog = "Messier"
        type_filter = filters.get("type", "")
        status_filter = filters.get("status", "")

        self.search.blockSignals(True)
        self.search.setText(search or "")
        self.search.blockSignals(False)
        self._on_search_changed(self.search.text())

        self.catalog_filter.blockSignals(True)
        self.catalog_filter.setCurrentText(catalog if catalog in [self.catalog_filter.itemText(i) for i in range(self.catalog_filter.count())] else "All")
        self.catalog_filter.blockSignals(False)
        self._on_catalog_changed(self.catalog_filter.currentText())

        self.type_filter.blockSignals(True)
        if type_filter and type_filter in [self.type_filter.itemText(i) for i in range(self.type_filter.count())]:
            self.type_filter.setCurrentText(type_filter)
        else:
            self.type_filter.setCurrentText("All")
        self.type_filter.blockSignals(False)
        self._on_type_changed(self.type_filter.currentText())

        self.status_filter.blockSignals(True)
        if status_filter and status_filter in [self.status_filter.itemText(i) for i in range(self.status_filter.count())]:
            self.status_filter.setCurrentText(status_filter)
        else:
            self.status_filter.setCurrentText("All")
        self.status_filter.blockSignals(False)
        self._on_status_changed(self.status_filter.currentText())

    def _capture_ui_state(self) -> None:
        self.config["ui_state"] = {
            "window_size": [self.width(), self.height()],
            "splitter_sizes": self.splitter.sizes() if self.splitter else [],
            "filters": {
                "catalog": self.catalog_filter.currentText() if self.catalog_filter else "",
                "type": self.type_filter.currentText() if self.type_filter else "",
                "status": self.status_filter.currentText() if self.status_filter else "",
            },
            "search": self.search.text() if self.search else "",
        }

    def _persist_ui_state(self) -> None:
        if self._zoom_timer.isActive():
            self._zoom_timer.stop()
            self._apply_zoom()
        self._capture_ui_state()
        if hasattr(self, "grid"):
            size = self.grid.iconSize().width()
        else:
            size = self.zoom_slider.value()
        if size:
            self.config["thumb_size"] = size
        save_config(self.config_path, self.config)

    @staticmethod
    def _format_bytes(value: int) -> str:
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(value)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
            size /= 1024.0

    @staticmethod
    def _next_available_path(path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        counter = 1
        while True:
            candidate = parent / f"{stem}-{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1


class SettingsDialog(QtWidgets.QDialog):
    previewChanged = QtCore.Signal(dict)

    def __init__(self, config: Dict, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self._config = config
        self.updated_config: Dict = {}
        self._map_server: Optional[_MapHttpServer] = None
        self._map_url: Optional[str] = None
        self._map_open_timer: Optional[QtCore.QTimer] = None
        self._scan_thread_pool = QtCore.QThreadPool.globalInstance()
        self._scan_task: Optional[DuplicateScanTask] = None
        self._report_path: Optional[Path] = None

        observer = config.get("observer", {})
        self.latitude = QtWidgets.QDoubleSpinBox()
        self.latitude.setRange(-90.0, 90.0)
        self.latitude.setDecimals(5)
        self.latitude.setValue(observer.get("latitude", 0.0))

        self.longitude = QtWidgets.QDoubleSpinBox()
        self.longitude.setRange(-180.0, 180.0)
        self.longitude.setDecimals(5)
        self.longitude.setValue(observer.get("longitude", 0.0))

        self.elevation = QtWidgets.QDoubleSpinBox()
        self.elevation.setRange(-500.0, 9000.0)
        self.elevation.setDecimals(1)
        self.elevation.setSuffix(" m")
        self.elevation.setValue(observer.get("elevation_m", 0.0))

        form = QtWidgets.QFormLayout()
        form.addRow("Latitude", self.latitude)
        form.addRow("Longitude", self.longitude)
        form.addRow("Elevation", self.elevation)

        map_button = QtWidgets.QPushButton("Pick on Map")
        map_button.clicked.connect(self._open_map_picker)
        form.addRow("", map_button)

        self.master_folder = QtWidgets.QLineEdit()
        self.master_folder.setText(config.get("master_image_dir", ""))
        browse_master = QtWidgets.QPushButton("Browse…")
        browse_master.clicked.connect(self._browse_master_folder)
        master_row = QtWidgets.QHBoxLayout()
        master_row.addWidget(self.master_folder)
        master_row.addWidget(browse_master)
        form.addRow("Master Image Folder", master_row)
        master_note = QtWidgets.QLabel(
            "Images placed in the Master Image Folder will be automatically sorted into "
            "catalog folders when running a duplicate scan."
        )
        master_note.setWordWrap(True)
        master_note.setStyleSheet("color: #bcbcbc;")
        form.addRow("", master_note)

        self.archive_folder = QtWidgets.QLineEdit()
        self.archive_folder.setText(config.get("archive_image_dir", ""))
        browse_archive = QtWidgets.QPushButton("Browse…")
        browse_archive.clicked.connect(self._browse_archive_folder)
        archive_row = QtWidgets.QHBoxLayout()
        archive_row.addWidget(self.archive_folder)
        archive_row.addWidget(browse_archive)
        form.addRow("Archive Image Folder", archive_row)

        clear_cache = QtWidgets.QPushButton("Clear thumbnail cache")
        clear_cache.clicked.connect(self._clear_thumbnail_cache)
        form.addRow("Thumbnail Cache", clear_cache)

        scan_row = QtWidgets.QHBoxLayout()
        self.scan_button = QtWidgets.QPushButton("Scan")
        self.scan_button.clicked.connect(self._scan_duplicate_images)
        scan_row.addWidget(self.scan_button)
        self.report_label = QtWidgets.QLabel("")
        self.report_label.setOpenExternalLinks(False)
        self.report_label.linkActivated.connect(self._open_duplicate_report)
        self.report_label.hide()
        scan_row.addWidget(self.report_label, stretch=1)
        form.addRow("Duplicate Scan", scan_row)

        self.cleanup_button = QtWidgets.QPushButton("Clean invalid entries")
        self.cleanup_button.clicked.connect(self._run_cleanup_now)
        form.addRow("Cleanup", self.cleanup_button)


        self.catalog_fields: Dict[str, QtWidgets.QLineEdit] = {}
        catalogs = config.get("catalogs", [])
        catalog_group = QtWidgets.QGroupBox("")
        catalog_layout = QtWidgets.QFormLayout(catalog_group)
        for catalog in catalogs:
            name = catalog.get("name", "Unknown")
            field = QtWidgets.QLineEdit()
            image_dirs = catalog.get("image_dirs", [])
            field.setText(image_dirs[0] if image_dirs else "")
            browse = QtWidgets.QPushButton("Browse…")
            browse.clicked.connect(lambda _checked=False, n=name: self._browse_catalog_folder(n))
            row = QtWidgets.QHBoxLayout()
            row.addWidget(field)
            row.addWidget(browse)
            catalog_layout.addRow(name, row)
            self.catalog_fields[name] = field

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(catalog_group)
        layout.addWidget(buttons)

    def accept(self) -> None:
        updated = dict(self._config)
        updated["observer"] = {
            "latitude": self.latitude.value(),
            "longitude": self.longitude.value(),
            "elevation_m": self.elevation.value(),
        }
        updated["master_image_dir"] = self.master_folder.text().strip()
        updated["archive_image_dir"] = self.archive_folder.text().strip()

        catalogs = []
        for catalog in updated.get("catalogs", []):
            name = catalog.get("name", "Unknown")
            field = self.catalog_fields.get(name)
            if field:
                paths = [part.strip() for part in field.text().split(",") if part.strip()]
                catalog["image_dirs"] = paths
            catalogs.append(catalog)
        updated["catalogs"] = catalogs

        self.updated_config = updated
        super().accept()

    def _browse_catalog_folder(self, name: str) -> None:
        field = self.catalog_fields.get(name)
        if field is None:
            return
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, f"Select {name} Image Folder")
        if not directory:
            return
        field.setText(directory)
        self._emit_preview()

    def _browse_master_folder(self) -> None:
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Master Image Folder")
        if not directory:
            return
        self.master_folder.setText(directory)
        self._emit_preview()

    def _browse_archive_folder(self) -> None:
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Archive Image Folder")
        if not directory:
            return
        self.archive_folder.setText(directory)
        self._emit_preview()

    def _clear_thumbnail_cache(self) -> None:
        parent = self.parent()
        if parent is None or not hasattr(parent, "clear_thumbnail_cache"):
            QtWidgets.QMessageBox.warning(self, "Thumbnail Cache", "Unable to clear thumbnail cache.")
            return
        if parent.clear_thumbnail_cache():
            QtWidgets.QMessageBox.information(self, "Thumbnail Cache", "Thumbnail cache cleared.")
            return
        QtWidgets.QMessageBox.warning(self, "Thumbnail Cache", "Unable to clear thumbnail cache.")

    def _scan_duplicate_images(self) -> None:
        config = self._build_preview_config()
        config_dir = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppConfigLocation)
        output_dir = Path(config_dir) if config_dir else PROJECT_ROOT
        output_dir.mkdir(parents=True, exist_ok=True)
        config_path = output_dir / "duplicate_scan_config.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        report_path = output_dir / "duplicate_image_report.txt"
        extensions = config.get(
            "image_extensions",
            [".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"],
        )
        self.scan_button.setEnabled(False)
        self.scan_button.setText("Scanning...")
        self.report_label.hide()
        self.report_label.setText("")
        task = DuplicateScanTask(config_path, extensions, report_path)
        task.signals.finished.connect(self._on_duplicate_scan_finished)
        self._scan_task = task
        self._scan_thread_pool.start(task)

    def _on_duplicate_scan_finished(self, report_path: str, error: str) -> None:
        self.scan_button.setEnabled(True)
        self.scan_button.setText("Scan")
        if error:
            QtWidgets.QMessageBox.warning(
                self,
                "Duplicate Scan",
                f"Unable to complete duplicate scan.\n\n{error}",
            )
            return
        self._report_path = Path(report_path)
        self.report_label.setText(f'<a href="{report_path}">Duplicate report available</a>')
        self.report_label.show()
        groups = self._load_duplicate_groups(self._report_path)
        if not groups:
            QtWidgets.QMessageBox.information(
                self,
                "Duplicate Scan",
                "Duplicate scan complete. No duplicates found.",
            )
            return
        choice = QtWidgets.QMessageBox.question(
            self,
            "Duplicate Scan",
            f"Duplicate scan complete. {len(groups)} duplicate group(s) found.\n\n"
            "Move duplicate files to the archive folder?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        if choice != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._move_duplicate_groups(groups)

    def _open_duplicate_report(self, link: str) -> None:
        if link:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(link))

    def _load_duplicate_groups(self, report_path: Path) -> List[Dict[str, object]]:
        json_path = report_path.with_suffix(".json")
        if not json_path.exists():
            return []
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        groups = payload.get("groups", [])
        return groups if isinstance(groups, list) else []

    def _move_duplicate_groups(self, groups: List[Dict[str, object]]) -> None:
        archive_dir = self.archive_folder.text().strip()
        if not archive_dir:
            object_name = self._describe_duplicate_object(groups)
            QtWidgets.QMessageBox.information(
                self,
                "Archive folder required",
                f"You have duplicate images of '{object_name}'. "
                "Choose an archive folder to safely store your duplicate images.",
            )
            self._browse_archive_folder()
            archive_dir = self.archive_folder.text().strip()
            if not archive_dir:
                return
        archive_root = Path(archive_dir).expanduser()
        if not archive_root.is_absolute():
            archive_root = (PROJECT_ROOT / archive_root).resolve()
        archive_root.mkdir(parents=True, exist_ok=True)
        moved = 0
        for group in groups:
            files = group.get("files", [])
            if not isinstance(files, list) or len(files) <= 1:
                continue
            file_paths = [Path(item.get("path", "")) for item in files if isinstance(item, dict)]
            file_paths = [path for path in file_paths if path.exists()]
            if len(file_paths) <= 1:
                continue
            file_paths = sorted(file_paths, key=lambda p: p.name.lower())
            for path in file_paths[1:]:
                target = self._next_available_path(archive_root / path.name)
                try:
                    shutil.move(str(path), str(target))
                    moved += 1
                except OSError:
                    continue
        QtWidgets.QMessageBox.information(
            self,
            "Duplicate Scan",
            f"Moved {moved} duplicate file(s) to the archive folder.",
        )

    def _run_cleanup_now(self) -> None:
        parent = self.parent()
        if parent is None or not hasattr(parent, "_cleanup_invalid_image_only_entries"):
            QtWidgets.QMessageBox.warning(self, "Cleanup", "Unable to run cleanup.")
            return
        parent._cleanup_invalid_image_only_entries()
        parent.config["cleanup_invalid_image_only_entries_done"] = True
        save_config(parent.config_path, parent.config)
        QtWidgets.QMessageBox.information(self, "Cleanup", "Cleanup complete.")

    def _describe_duplicate_object(self, groups: List[Dict[str, object]]) -> str:
        for group in groups:
            catalog = group.get("catalog")
            common_ids = group.get("common_ids", [])
            if not isinstance(common_ids, list) or not common_ids:
                continue
            object_id = str(common_ids[0])
            name = self._lookup_object_name(catalog, object_id)
            return f"{object_id} ({name})" if name else object_id
        return "this object"

    def _lookup_object_name(self, catalog: object, object_id: str) -> str:
        if not isinstance(catalog, str):
            return ""
        config = self._build_preview_config()
        for entry in config.get("catalogs", []):
            if entry.get("name") != catalog:
                continue
            metadata_value = entry.get("metadata_file")
            if not metadata_value:
                return ""
            metadata_path = Path(metadata_value)
            if not metadata_path.is_absolute():
                metadata_path = (PROJECT_ROOT / metadata_path).resolve()
            if not metadata_path.exists():
                return ""
            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return ""
            catalog_data = data.get(catalog, {})
            if not isinstance(catalog_data, dict):
                return ""
            entry_data = catalog_data.get(object_id, {})
            if not isinstance(entry_data, dict):
                return ""
            return str(entry_data.get("name") or "").strip()
        return ""

    @staticmethod
    def _next_available_path(path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        counter = 1
        while True:
            candidate = parent / f"{stem}-{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    def _open_map_picker(self) -> None:
        if self._map_server is None:
            self._map_server = _MapHttpServer(self)
            self._map_server_thread = threading.Thread(
                target=self._map_server.serve_forever, daemon=True
            )
            self._map_server_thread.start()
            self._map_url = f"http://127.0.0.1:{self._map_server.port}/"
            self._map_open_timer = QtCore.QTimer(self)
            self._map_open_timer.setSingleShot(True)
            self._map_open_timer.setInterval(200)
            self._map_open_timer.timeout.connect(self._open_map_url)
            self._map_open_timer.start()
        else:
            self._open_map_url()

    def _open_map_url(self) -> None:
        if self._map_url:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(self._map_url))

    def _apply_location(self, lat: float, lon: float) -> None:
        self.latitude.setValue(lat)
        self.longitude.setValue(lon)
        self._emit_preview()

    @QtCore.Slot(float, float)
    def _post_location(self, lat: float, lon: float) -> None:
        self._apply_location(lat, lon)

    def _map_html(self) -> str:
        lat = self.latitude.value()
        lon = self.longitude.value()
        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Pick Location</title>
  <style>
    html, body, #map {{ height: 100%; margin: 0; background: #111; }}
    .controls {{
      position: absolute; top: 10px; left: 10px; z-index: 999;
      background: rgba(0,0,0,0.6); color: #fff; padding: 8px 10px;
      font-family: sans-serif; font-size: 14px; border-radius: 6px;
    }}
    .controls button {{ margin-right: 8px; }}
  </style>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
</head>
<body>
  <div id="map"></div>
  <div class="controls">
    <button id="geo">Use My Location</button>
    <span id="status">Click on the map to set location</span>
  </div>
  <script>
    const map = L.map('map').setView([{lat}, {lon}], 3);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 10,
      attribution: '&copy; OpenStreetMap'
    }}).addTo(map);
    const marker = L.marker([{lat}, {lon}]).addTo(map);
    function sendLocation(lat, lon) {{
      marker.setLatLng([lat, lon]);
      fetch('/set_location', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ lat, lon }})
      }}).catch(() => {{}});
      document.getElementById('status').textContent = `Selected: ${{lat.toFixed(5)}}, ${{lon.toFixed(5)}}`;
    }}
    map.on('click', (e) => sendLocation(e.latlng.lat, e.latlng.lng));
    document.getElementById('geo').addEventListener('click', () => {{
      navigator.geolocation.getCurrentPosition(
        (pos) => {{
          const lat = pos.coords.latitude;
          const lon = pos.coords.longitude;
          map.setView([lat, lon], 7);
          sendLocation(lat, lon);
        }},
        () => {{ document.getElementById('status').textContent = 'Location permission denied.'; }}
      );
    }});
  </script>
</body>
</html>"""

    def _shutdown_map_server(self) -> None:
        if self._map_server is not None:
            self._map_server.shutdown()
            self._map_server = None
        self._map_url = None
        if self._map_open_timer is not None:
            self._map_open_timer.stop()
            self._map_open_timer = None

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._shutdown_map_server()
        super().closeEvent(event)

    def _emit_preview(self) -> None:
        self.previewChanged.emit(self._build_preview_config())

    def _build_preview_config(self) -> Dict:
        updated = dict(self._config)
        updated["observer"] = {
            "latitude": self.latitude.value(),
            "longitude": self.longitude.value(),
            "elevation_m": self.elevation.value(),
        }
        updated["master_image_dir"] = self.master_folder.text().strip()
        updated["archive_image_dir"] = self.archive_folder.text().strip()
        catalogs = []
        for catalog in updated.get("catalogs", []):
            name = catalog.get("name", "Unknown")
            field = self.catalog_fields.get(name)
            if field:
                value = field.text().strip()
                catalog["image_dirs"] = [value] if value else []
            catalogs.append(catalog)
        updated["catalogs"] = catalogs
        return updated


class _MapHttpServer:
    def __init__(self, dialog: QtCore.QObject) -> None:
        self.dialog = dialog

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                try:
                    path = urlparse(self.path).path
                    if path not in ("/", "/index.html"):
                        self.send_error(404)
                        return
                    dialog = self.server.dialog  # type: ignore[attr-defined]
                    if dialog is None:
                        self.send_error(410)
                        return
                    body = dialog._map_html()
                    data = body.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    self.send_error(500)

            def do_POST(self) -> None:
                try:
                    path = urlparse(self.path).path
                    if path != "/set_location":
                        self.send_error(404)
                        return
                    dialog = self.server.dialog  # type: ignore[attr-defined]
                    if dialog is None:
                        self.send_error(410)
                        return
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = self.rfile.read(length).decode("utf-8")
                    data = json.loads(payload)
                    lat = float(data.get("lat"))
                    lon = float(data.get("lon"))
                    QtCore.QMetaObject.invokeMethod(
                        dialog,
                        "_post_location",
                        QtCore.Qt.ConnectionType.QueuedConnection,
                        QtCore.Q_ARG(float, lat),
                        QtCore.Q_ARG(float, lon),
                    )
                    self.send_response(204)
                    self.end_headers()
                except Exception:
                    self.send_error(400)

            def log_message(self, _format: str, *args: object) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._server.dialog = self.dialog  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class WelcomeDialog(QtWidgets.QDialog):
    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Welcome")
        self.setMinimumWidth(560)

        title = QtWidgets.QLabel("Welcome to Astro Catalogue Viewer")
        title.setObjectName("welcomeTitle")

        body = QtWidgets.QTextBrowser()
        body.setOpenExternalLinks(True)
        body.setObjectName("welcomeBody")
        body.setHtml(
            """
            <p>This app helps you browse deep-sky catalogs with your own imagery.</p>
            <p><b>Quick start</b></p>
            <ul>
              <li>Open <b>Settings</b> to choose image folders for each catalog.</li>
              <li>Set your observer location so visibility hints match your sky.</li>
              <li>Use the filters and search to find objects fast.</li>
              <li>Click an object to view metadata and add notes.</li>
            </ul>
            <p><b>Image naming</b></p>
            <p>Filenames should include the standard object ID, such as <b>M31</b>, <b>NGC2088</b>, <b>IC5070</b>, or <b>C14</b>.</p>
            <p><b>Missing images</b></p>
            <p>Enable <b>Wiki thumbnails</b> in the toolbar to preview missing targets while you build your library.</p>
            <p><b>Support development</b></p>
            <p>This project takes time and money to develop. If you find it useful, please consider supporting:</p>
            <p><a href="https://buymeacoffee.com/PaulSpinelli">buymeacoffee.com/PaulSpinelli</a><br>
               <a href="https://www.paypal.com/donate/?hosted_button_id=9GDUBHS78MH52">paypal.com/donate</a></p>
            <p><b>Feedback</b></p>
            <p>Please share suggestions and bug reports via the GitHub repo issues page.</p>
            """
        )
        body.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body.document().setTextWidth(520)
        body.setMinimumHeight(int(body.document().size().height()) + 16)

        self.skip_checkbox = QtWidgets.QCheckBox("Don't show again")

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
        )
        buttons.accepted.connect(self.accept)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(body)
        layout.addWidget(self.skip_checkbox)
        layout.addWidget(buttons)

    def skip_requested(self) -> bool:
        return self.skip_checkbox.isChecked()


class AboutDialog(QtWidgets.QDialog):
    check_updates_requested = QtCore.Signal()
    auto_check_toggled = QtCore.Signal(bool)

    def __init__(
        self,
        config: Dict,
        app_version: str,
        data_version: str,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("About")
        self.setMinimumWidth(760)
        self._config = config
        self._app_version = app_version
        self._data_version = data_version

        title = QtWidgets.QLabel("Astro Catalogue Viewer")
        title.setObjectName("aboutTitle")
        self.app_version_label = QtWidgets.QLabel(f"App Version: {app_version}")
        self.app_version_label.setObjectName("aboutVersion")
        self.data_version_label = QtWidgets.QLabel(f"Data Version: {data_version}")
        self.data_version_label.setObjectName("aboutDataVersion")

        about = QtWidgets.QLabel(
            "Astro Catalogue Viewer helps you organize deep-sky catalogs with your own imagery, "
            "track capture progress, and plan what to shoot next."
        )
        about.setWordWrap(True)

        links = QtWidgets.QLabel(
            'Website: <a href="https://astro-catalogue-viewer.com/">astro-catalogue-viewer.com</a><br>'
            'Support: <a href="https://buymeacoffee.com/PaulSpinelli">buymeacoffee.com/PaulSpinelli</a><br>'
            'PayPal: <a href="https://www.paypal.com/donate/?hosted_button_id=9GDUBHS78MH52">paypal.com/donate</a><br>'
            f'Repo: <a href="https://github.com/{UPDATE_REPO}">github.com/{UPDATE_REPO}</a>'
        )
        links.setOpenExternalLinks(True)
        links.setObjectName("aboutLinks")

        sponsor_box = QtWidgets.QGroupBox("Sponsors")
        sponsor_layout = QtWidgets.QVBoxLayout(sponsor_box)
        self.supporters_status = QtWidgets.QLabel("Loading supporters…")
        self.supporters_status.setWordWrap(True)
        self.supporters_status.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.supporters_status.setOpenExternalLinks(True)
        sponsor_scroll = QtWidgets.QScrollArea()
        sponsor_scroll.setWidgetResizable(True)
        sponsor_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        sponsor_scroll.setWidget(self.supporters_status)
        sponsor_layout.addWidget(sponsor_scroll)

        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(title)
        left_layout.addWidget(self.app_version_label)
        left_layout.addWidget(self.data_version_label)
        left_layout.addSpacing(8)
        left_layout.addWidget(about)
        left_layout.addSpacing(10)
        left_layout.addWidget(links)
        left_layout.addSpacing(10)
        left_layout.addWidget(sponsor_box)
        left_layout.addStretch(1)

        quick_title = QtWidgets.QLabel("Quick start")
        quick_title.setObjectName("aboutSectionTitle")
        quick_list = QtWidgets.QLabel(
            "1. Open Settings to set catalog image folders.\n"
            "2. Set your observer location for accurate visibility hints.\n"
            "3. Use filters and search to find targets fast.\n"
            "4. Open an object to view metadata and add notes."
        )
        quick_list.setWordWrap(True)

        updates_title = QtWidgets.QLabel("Updates")
        updates_title.setObjectName("aboutSectionTitle")
        self.update_status = QtWidgets.QLabel("Not checked")
        self.update_status.setObjectName("aboutUpdateStatus")
        self.update_status.setWordWrap(True)
        self.auto_check = QtWidgets.QCheckBox("Check for updates automatically")
        self.auto_check.setChecked(bool(config.get("auto_check_updates", True)))
        self.auto_check.toggled.connect(self.auto_check_toggled.emit)
        self.check_updates = QtWidgets.QPushButton("Check for updates")
        self.check_updates.clicked.connect(self.check_updates_requested.emit)

        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(quick_title)
        right_layout.addWidget(quick_list)
        right_layout.addSpacing(16)
        right_layout.addWidget(updates_title)
        right_layout.addWidget(self.update_status)
        right_layout.addWidget(self.auto_check)
        right_layout.addWidget(self.check_updates)
        right_layout.addStretch(1)

        content = QtWidgets.QHBoxLayout()
        content.addWidget(left, stretch=3)
        content.addWidget(right, stretch=2)
        content.setSpacing(24)

        close_button = QtWidgets.QPushButton("Close")
        close_button.clicked.connect(self.accept)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(content)
        layout.addWidget(close_button, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

        self._supporters_task: Optional[SupportersFetchTask] = None
        self._supporters_thread_pool = QtCore.QThreadPool.globalInstance()
        self._start_supporters_fetch()

    def set_update_status(self, status: str, latest: Optional[str], url: Optional[str]) -> None:
        if url:
            self.update_status.setText(f'{status} <a href="{url}">View release</a>')
            self.update_status.setOpenExternalLinks(True)
        else:
            self.update_status.setText(status)
            self.update_status.setOpenExternalLinks(False)

    def set_data_version(self, version: str) -> None:
        if not version:
            return
        self._data_version = version
        self.data_version_label.setText(f"Data Version: {version}")

    def _start_supporters_fetch(self) -> None:
        task = SupportersFetchTask(SUPPORTERS_URL, user_agent=f"{APP_NAME}/{self._app_version}")
        task.signals.loaded.connect(self._apply_supporters)
        task.signals.failed.connect(self._supporters_failed)
        self._supporters_task = task
        self._supporters_thread_pool.start(task)

    def _apply_supporters(self, supporters: List[str]) -> None:
        if not supporters:
            self.supporters_status.setText("No supporters listed yet.")
            return
        self.supporters_status.setText("<br>".join(supporters))

    def _supporters_failed(self, message: str) -> None:
        self.supporters_status.setText(message)


class UpdateCheckTask(QtCore.QRunnable):
    def __init__(self, current_version: str) -> None:
        super().__init__()
        self.current_version = current_version
        self.signals = UpdateSignals()

    def run(self) -> None:
        if SHUTDOWN_EVENT.is_set():
            return
        try:
            payload = self._fetch_latest_release()
            tag = (payload.get("tag_name") or "").strip()
            html_url = (payload.get("html_url") or "").strip()
            latest = self._normalize_version(tag)
            current = self._normalize_version(self.current_version)
            if not latest:
                self._emit_failed("No release tag found.")
                return
            if latest != current:
                self._emit_available(tag, html_url)
            else:
                self._emit_up_to_date(tag)
        except Exception:
            self._emit_failed("Update check failed.")
        finally:
            self._emit_finished()

    @staticmethod
    def _normalize_version(value: str) -> str:
        return value.strip().lstrip("vV")

    def _emit_available(self, tag: str, url: str) -> None:
        if SHUTDOWN_EVENT.is_set() or not isValid(self.signals):
            return
        try:
            self.signals.available.emit(tag, url)
        except RuntimeError:
            return

    def _emit_up_to_date(self, tag: str) -> None:
        if SHUTDOWN_EVENT.is_set() or not isValid(self.signals):
            return
        try:
            self.signals.up_to_date.emit(tag)
        except RuntimeError:
            return

    def _emit_failed(self, message: str) -> None:
        if SHUTDOWN_EVENT.is_set() or not isValid(self.signals):
            return
        try:
            self.signals.failed.emit(message)
        except RuntimeError:
            return

    def _emit_finished(self) -> None:
        if SHUTDOWN_EVENT.is_set() or not isValid(self.signals):
            return
        try:
            self.signals.finished.emit()
        except RuntimeError:
            return

    @staticmethod
    def _fetch_latest_release() -> Dict:
        if SHUTDOWN_EVENT.is_set():
            return {}
        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = subprocess.CREATE_NO_WINDOW
        url = f"https://api.github.com/repos/{UPDATE_REPO}/releases"
        result = subprocess.run(
            [
                "curl",
                "-sL",
                "--max-time",
                "8",
                "--retry",
                "2",
                "--retry-delay",
                "1",
                "-H",
                "User-Agent: AstroCatalogueViewer/1.0",
                url,
            ],
            check=True,
            capture_output=True,
            creationflags=creationflags,
        )
        payload = json.loads(result.stdout or "{}")
        if isinstance(payload, list) and payload:
            for entry in payload:
                if isinstance(entry, dict) and entry.get("tag_name"):
                    return entry
            return {}
        if isinstance(payload, dict):
            return payload
        return {}


class SupportersFetchTask(QtCore.QRunnable):
    def __init__(self, url: str, user_agent: Optional[str] = None) -> None:
        super().__init__()
        self.url = url
        self.user_agent = user_agent or f"{APP_NAME}/{APP_VERSION}"
        self.signals = SupportersSignals()

    def run(self) -> None:
        if SHUTDOWN_EVENT.is_set():
            return
        try:
            payload = self._fetch_payload()
            supporters = self._normalize_supporters(payload)
            self._emit_loaded(supporters)
        except Exception:
            self._emit_failed("Unable to load supporters.")

    def _emit_loaded(self, supporters: List[str]) -> None:
        if SHUTDOWN_EVENT.is_set() or not isValid(self.signals):
            return
        try:
            self.signals.loaded.emit(supporters)
        except RuntimeError:
            return

    def _emit_failed(self, message: str) -> None:
        if SHUTDOWN_EVENT.is_set() or not isValid(self.signals):
            return
        try:
            self.signals.failed.emit(message)
        except RuntimeError:
            return

    def _fetch_payload(self) -> Dict:
        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = subprocess.CREATE_NO_WINDOW
        for url in self._candidate_urls():
            result = subprocess.run(
                [
                    "curl",
                    "-sL",
                    "--max-time",
                    "6",
                    "--retry",
                    "2",
                    "--retry-delay",
                    "1",
                    "-H",
                    f"User-Agent: {self.user_agent}",
                    url,
                ],
                check=False,
                capture_output=True,
                creationflags=creationflags,
            )
            if result.returncode != 0:
                continue
            payload = json.loads(result.stdout or "{}")
            if payload:
                return payload
        return {}

    def _candidate_urls(self) -> List[str]:
        if "/main/" in self.url:
            return [self.url, self.url.replace("/main/", "/master/")]
        return [self.url]

    @staticmethod
    def _normalize_supporters(payload) -> List[str]:
        if isinstance(payload, dict):
            payload = payload.get("supporters", payload.get("supporter", []))
        if isinstance(payload, list):
            stargazers: List[str] = []
            supporters: List[str] = []
            for entry in payload:
                if isinstance(entry, str):
                    supporters.append(entry)
                    continue
                if isinstance(entry, dict):
                    name = str(entry.get("name") or "").strip()
                    tier = str(entry.get("tier") or "").strip()
                    url = str(entry.get("url") or "").strip()
                    if not name:
                        continue
                    line = f"{name} — {tier}" if tier else name
                    if url:
                        line = f'{line} — <a href="{url}">YouTube</a>'
                    tier_key = tier.casefold()
                    if tier_key in {"stargazer", "stargazers"}:
                        stargazers.append(line)
                    else:
                        supporters.append(line)
            return stargazers + supporters
        return []


class DataVersionFetchTask(QtCore.QRunnable):
    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url
        self.signals = DataVersionSignals()

    def run(self) -> None:
        if SHUTDOWN_EVENT.is_set():
            return
        try:
            payload = self._fetch_payload()
            version = _extract_version(payload)
            if version:
                self._emit_loaded(version)
            else:
                self._emit_failed("Unable to load version.")
        except Exception:
            self._emit_failed("Unable to load version.")

    def _emit_loaded(self, version: str) -> None:
        if SHUTDOWN_EVENT.is_set() or not isValid(self.signals):
            return
        try:
            self.signals.loaded.emit(version)
        except RuntimeError:
            return

    def _emit_failed(self, message: str) -> None:
        if SHUTDOWN_EVENT.is_set() or not isValid(self.signals):
            return
        try:
            self.signals.failed.emit(message)
        except RuntimeError:
            return

    def _fetch_payload(self) -> Dict:
        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = subprocess.CREATE_NO_WINDOW
        for url in self._candidate_urls():
            result = subprocess.run(
                [
                    "curl",
                    "-sL",
                    "--max-time",
                    "6",
                    "--retry",
                    "2",
                    "--retry-delay",
                    "1",
                    "-H",
                    f"User-Agent: {APP_NAME}/{APP_VERSION}",
                    url,
                ],
                check=False,
                capture_output=True,
                creationflags=creationflags,
            )
            if result.returncode != 0:
                continue
            payload = json.loads(result.stdout or "{}")
            if payload:
                return payload
        return {}

    def _candidate_urls(self) -> List[str]:
        if "/main/" in self.url:
            return [self.url, self.url.replace("/main/", "/master/")]
        return [self.url]


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    QtCore.QLoggingCategory.setFilterRules("qt.gui.imageio=false\n")

    location = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppConfigLocation)
    if location:
        config_dir = Path(location)
    else:
        config_dir = PROJECT_ROOT
    config_path = config_dir / "config.json"

    window = MainWindow(config_path)
    app.aboutToQuit.connect(window._persist_ui_state)
    if window.config.get("show_welcome", True):
        welcome = WelcomeDialog(window)
        welcome.exec()
        if welcome.skip_requested():
            window.config["show_welcome"] = False
            save_config(config_path, window.config)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
