from __future__ import annotations

import sys
import hashlib
import subprocess
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import http.server
import json
import threading
from urllib.parse import urlparse, unquote
import datetime
import array
from dataclasses import replace

from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid

from catalog import CatalogItem, collect_object_types, load_config, load_catalog_items, resolve_metadata_path, save_config, save_note, save_thumbnail, save_image_note
from catalog import PROJECT_ROOT
from image_cache import ThumbnailCache


APP_NAME = "Astro Catalogue Viewer"
APP_VERSION = "1.6.0-beta"
ORG_NAME = "AstroCatalogueViewer"
UPDATE_REPO = "thebioguy/Astro-Catalogue-Viewer"
SUPPORTERS_URL = f"https://raw.githubusercontent.com/{UPDATE_REPO}/main/data/supporters.json"
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
    failed = QtCore.Signal(int, str)


class UpdateSignals(QtCore.QObject):
    available = QtCore.Signal(str, str)
    up_to_date = QtCore.Signal(str)
    failed = QtCore.Signal(str)
    finished = QtCore.Signal()


class SupportersSignals(QtCore.QObject):
    loaded = QtCore.Signal(list)
    failed = QtCore.Signal(str)


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
    def __init__(self, item_key: str, page_title: str, cache_path: Path, thumb_size: int) -> None:
        super().__init__()
        self.item_key = item_key
        self.page_title = page_title
        self.cache_path = cache_path
        self.thumb_size = thumb_size
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
        image = _load_display_image(self.image_path)
        if image is None or image.isNull():
            if not isValid(self.signals):
                return
            try:
                self.signals.failed.emit(self.request_id, str(self.image_path))
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

    def __init__(self, items: List[CatalogItem], cache: ThumbnailCache, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self._items = items
        self._cache = cache
        self._loading = set()
        self._pixmaps: Dict[str, QtGui.QPixmap] = {}
        self._remote_pixmaps: Dict[str, QtGui.QPixmap] = {}
        self._remote_loading = set()
        self._remote_failed = set()
        self._wiki_enabled = False
        self._row_lookup = {item.unique_key: row for row, item in enumerate(items)}
        self._thread_pool = QtCore.QThreadPool.globalInstance()
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
        if item.catalog not in {"Messier", "Solar system"}:
            return
        if item.unique_key in self._remote_loading or item.unique_key in self._remote_failed:
            return
        title = self._wiki_title_for_item(item)
        if not title:
            self._remote_failed.add(item.unique_key)
            return
        cache_path = self._wiki_cache_path(title)
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
        task = WikiThumbnailTask(item.unique_key, title, cache_path, self._cache.thumb_size)
        task.signals.loaded.connect(self._on_wiki_thumbnail_loaded)
        task.signals.failed.connect(self._on_wiki_thumbnail_failed)
        self._thread_pool.start(task)

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
            if len(item.image_paths) > 1:
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


def _load_tiff_with_tifffile(path: Path) -> Optional[QtGui.QImage]:
    try:
        import numpy as np
        import tifffile
    except Exception:
        return None

    try:
        data = tifffile.imread(str(path))
    except Exception:
        return None

    if data is None:
        return None
    if data.ndim == 2:
        return _tone_map_numpy_to_qimage(data, "L")
    if data.ndim == 3 and data.shape[2] in (3, 4):
        mode = "RGB" if data.shape[2] == 3 else "RGBA"
        return _tone_map_numpy_to_qimage(data, mode)
    return None


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


def _load_display_image(path: Path) -> Optional[QtGui.QImage]:
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
            return image
    tif_image = _load_tiff_with_tifffile(path)
    if tif_image is not None:
        return tif_image
    fallback = QtGui.QImage(str(path))
    return fallback if not fallback.isNull() else None


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

    def _on_image_failed(self, request_id: int, path_value: str) -> None:
        if request_id != self._image_load_id:
            return
        self.image_view.set_pixmap(None)
        self.image_info.setText("Unable to load image (unsupported TIFF bit depth)")
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
        self._ensure_user_metadata_files()
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
        self._thread_pool = QtCore.QThreadPool.globalInstance()
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
        self._closing = False

        self._build_ui()
        self._apply_dark_theme()
        self._apply_saved_window_state()
        self._update_filters()
        self._start_catalog_load()
        if self.config.get("auto_check_updates", True):
            QtCore.QTimer.singleShot(250, self._check_updates_silent)

    def _cache_dir(self) -> Path:
        location = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.CacheLocation)
        return Path(location)

    def _ensure_user_metadata_files(self) -> None:
        if not hasattr(sys, "_MEIPASS"):
            return
        location = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppConfigLocation)
        if not location:
            return
        metadata_dir = Path(location) / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        updated = False
        for catalog in self.config.get("catalogs", []):
            meta_value = catalog.get("metadata_file")
            if not meta_value:
                continue
            meta_path = Path(meta_value)
            if not meta_path.is_absolute():
                meta_path = (PROJECT_ROOT / meta_path).resolve()
            if metadata_dir in meta_path.parents:
                continue
            target = metadata_dir / meta_path.name
            if not target.exists():
                try:
                    shutil.copy2(meta_path, target)
                except OSError:
                    continue
            catalog["metadata_file"] = str(target)
            updated = True
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

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(10)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search by object ID or name")
        self.search.textChanged.connect(self._on_search_changed)
        self.search.setMinimumWidth(280)
        self.search.setMaximumWidth(700)
        self.search.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
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

        toolbar.addWidget(self.search)
        toolbar.addStretch(1)
        toolbar.addWidget(self.catalog_summary_container)
        toolbar.addStretch(1)
        toolbar.addWidget(QtWidgets.QLabel("Catalog"))
        toolbar.addWidget(self.catalog_filter)
        toolbar.addSpacing(6)
        toolbar.addWidget(QtWidgets.QLabel("Object Type"))
        toolbar.addWidget(self.type_filter)
        toolbar.addSpacing(6)
        toolbar.addWidget(QtWidgets.QLabel("Status"))
        toolbar.addWidget(self.status_filter)
        toolbar.addSpacing(6)
        toolbar.addWidget(QtWidgets.QLabel("Zoom"))
        toolbar.addWidget(self.zoom_slider)
        toolbar.addWidget(self.wiki_thumbs)
        toolbar.addWidget(self.refresh_button)
        toolbar.addWidget(self.settings_button)
        toolbar.addWidget(self.about_button)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setObjectName("statusLabel")

        layout.addLayout(toolbar)
        layout.addWidget(self.status_label)

        self.grid = QtWidgets.QListView()
        self.grid.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(0)
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

    def _apply_dark_theme(self) -> None:
        self.setStyleSheet(
            """
            QWidget { background: #141414; color: #e5e5e5; font-family: 'Avenir Next', 'Helvetica Neue', Arial; }
            QLineEdit, QComboBox, QTextEdit { background: #1d1d1d; border: 1px solid #333; padding: 6px; }
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
            QPushButton { background: #2c2c2c; border: 1px solid #3b3b3b; padding: 6px 12px; }
            QPushButton:hover { background: #3a3a3a; }
            QSlider::groove:horizontal { height: 6px; background: #2a2a2a; }
            QSlider::handle:horizontal { width: 14px; background: #d9a441; margin: -4px 0; border-radius: 7px; }
            """
        )

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

    def _on_type_changed(self, value: str) -> None:
        if value == "All":
            self.proxy.set_type_filter("")
        else:
            self.proxy.set_type_filter(value)
        self._schedule_auto_fit()

    def _on_status_changed(self, value: str) -> None:
        if value == "All":
            self.proxy.set_status_filter("")
        else:
            self.proxy.set_status_filter(value)
        self._schedule_auto_fit()

    def _on_search_changed(self, text: str) -> None:
        self.proxy.set_search_text(text)
        self._schedule_auto_fit()

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
        if name in {"NGC", "IC", "Caldwell"}:
            return f"{name} (In progress)"
        return name

    @staticmethod
    def _catalog_title_text(title: str, suffix: str) -> str:
        if title in {"NGC", "IC", "Caldwell"}:
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

    def _on_zoom_changed(self, value: int) -> None:
        self._auto_fit_enabled = False
        self._pending_zoom = value
        self._zoom_timer.start()

    def _apply_zoom(self) -> None:
        value = self._pending_zoom
        self._update_grid_metrics(value)
        self.config["thumb_size"] = value
        self.thumbnail_cache = ThumbnailCache(self._cache_dir(), value)
        self.model.update_cache(self.thumbnail_cache)
        self._schedule_view_refresh()

    def _schedule_auto_fit(self) -> None:
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
        min_size = 60
        max_size = max(min(width, height), min_size)
        best = min_size
        best_gap = width

        max_columns = min(item_count, max(1, width // min_size))
        for columns in range(1, max_columns + 1):
            rows = (item_count + columns - 1) // columns
            size_w = (width + spacing) // columns - spacing
            size_h = (height + spacing) // rows - spacing
            size = min(size_w, size_h)
            if size < min_size:
                continue
            if size > max_size:
                size = max_size
            used_width = max(0, columns * (size + spacing) - spacing)
            gap = max(0, width - used_width)
            if size > best or (size == best and gap < best_gap):
                best = size
                best_gap = gap
        self.grid.setIconSize(QtCore.QSize(best, best))
        self._update_grid_metrics(best)
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(best)
        self.zoom_slider.blockSignals(False)
        self.config["thumb_size"] = best
        self.thumbnail_cache = ThumbnailCache(self._cache_dir(), best)
        self.model.update_cache(self.thumbnail_cache)
        self._schedule_view_refresh()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._schedule_auto_fit()

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
        self._capture_ui_state()
        save_config(self.config_path, self.config)
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
        self._thread_pool.start(task)

    def _on_catalog_loaded(self, items: List[CatalogItem]) -> None:
        self.items = items
        self.model.set_items(self.items)
        wiki_enabled = bool(self._loading_config.get("use_wiki_thumbnails", False))
        self.model.set_wiki_thumbnails_enabled(wiki_enabled)
        self._update_filters()
        if not self._saved_state_applied:
            self._apply_saved_filters()
            self._saved_state_applied = True
        self._auto_fit_enabled = True
        self._schedule_auto_fit()
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

    def _open_about(self) -> None:
        if self._about_dialog and self._about_dialog.isVisible():
            self._about_dialog.raise_()
            self._about_dialog.activateWindow()
            return
        dialog = AboutDialog(self.config, self)
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


        self.catalog_fields: Dict[str, QtWidgets.QLineEdit] = {}
        catalogs = config.get("catalogs", [])
        catalog_group = QtWidgets.QGroupBox("Image folder per catalog")
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

    def __init__(self, config: Dict, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About")
        self.setMinimumWidth(760)
        self._config = config

        title = QtWidgets.QLabel("Astro Catalogue Viewer")
        title.setObjectName("aboutTitle")
        version = QtWidgets.QLabel(f"Version {APP_VERSION}")
        version.setObjectName("aboutVersion")

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
        left_layout.addWidget(version)
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

    def _start_supporters_fetch(self) -> None:
        task = SupportersFetchTask(SUPPORTERS_URL)
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
    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url
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

    @staticmethod
    def _normalize_supporters(payload) -> List[str]:
        if isinstance(payload, dict):
            payload = payload.get("supporters", payload.get("supporter", []))
        if isinstance(payload, list):
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
                    supporters.append(line)
            return supporters
        return []


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
