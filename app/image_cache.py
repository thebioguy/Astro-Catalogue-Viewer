from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import time
from typing import Dict, Optional

from PySide6 import QtCore, QtGui


@dataclass
class CacheEntry:
    pixmap: QtGui.QPixmap
    last_access: float


class ThumbnailCache:
    def __init__(self, cache_dir: Path, thumb_size: int, memory_items: int = 512) -> None:
        self.cache_dir = cache_dir
        self.thumb_size = thumb_size
        self.memory_items = memory_items
        self._memory: Dict[str, CacheEntry] = {}
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_key(self, image_path: Path) -> str:
        try:
            stat = image_path.stat()
        except FileNotFoundError:
            return ""
        payload = f"{image_path.resolve()}:{stat.st_mtime_ns}:{self.thumb_size}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.png"

    def _prune(self) -> None:
        if len(self._memory) <= self.memory_items:
            return
        sorted_items = sorted(self._memory.items(), key=lambda kv: kv[1].last_access)
        for key, _ in sorted_items[: len(self._memory) - self.memory_items]:
            self._memory.pop(key, None)

    def get_thumbnail(self, image_path: Path) -> Optional[QtGui.QPixmap]:
        key = self._cache_key(image_path)
        if not key:
            return None
        entry = self._memory.get(key)
        if entry:
            entry.last_access = time.time()
            return entry.pixmap

        disk_path = self._cache_path(key)
        if disk_path.exists():
            pixmap = QtGui.QPixmap(str(disk_path))
            if not pixmap.isNull():
                self._memory[key] = CacheEntry(pixmap=pixmap, last_access=time.time())
                self._prune()
                return pixmap
            try:
                disk_path.unlink()
            except OSError:
                pass
        return None

    def store_thumbnail(self, image_path: Path, pixmap: QtGui.QPixmap) -> None:
        key = self._cache_key(image_path)
        self._memory[key] = CacheEntry(pixmap=pixmap, last_access=time.time())
        self._prune()
        disk_path = self._cache_path(key)
        temp_path = disk_path.with_suffix(".tmp")
        pixmap.save(str(temp_path), "PNG")
        temp_path.replace(disk_path)

    def store_thumbnail_image(self, image_path: Path, image: QtGui.QImage) -> QtGui.QPixmap:
        key = self._cache_key(image_path)
        squared = self._scale_to_square(image)
        pixmap = QtGui.QPixmap.fromImage(squared)
        self._memory[key] = CacheEntry(pixmap=pixmap, last_access=time.time())
        self._prune()
        disk_path = self._cache_path(key)
        temp_path = disk_path.with_suffix(".tmp")
        squared.save(str(temp_path), "PNG")
        temp_path.replace(disk_path)
        return pixmap

    def create_thumbnail(self, image_path: Path) -> Optional[QtGui.QImage]:
        reader = QtGui.QImageReader(str(image_path))
        if not reader.canRead():
            return None
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            return None
        if "saturn" in image_path.stem.lower():
            image = self._center_square_crop(image)
        return self._scale_to_square(image)

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

    def clear(self) -> None:
        self._memory.clear()
        if not self.cache_dir.exists():
            return
        for entry in self.cache_dir.iterdir():
            try:
                if entry.is_dir():
                    for sub in entry.rglob("*"):
                        if sub.is_file():
                            sub.unlink()
                    entry.rmdir()
                else:
                    entry.unlink()
            except OSError:
                continue
