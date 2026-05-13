"""JSON-backed dict storage with atomic writes."""

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class JsonStorage:
    """Persist a dict to disk as JSON. Subclasses add domain methods."""

    def __init__(self, file_path: Path):
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.file_path.exists():
            return {}
        try:
            with open(self.file_path, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            # File exists but is malformed JSON — could be disk corruption,
            # an external edit, or a pre-fsync save from an old version.
            # Move it aside so the next _save doesn't overwrite the only
            # copy of (potentially recoverable) user data with `{}`.
            backup = self.file_path.with_name(
                f"{self.file_path.name}.corrupt-{int(time.time())}"
            )
            try:
                os.rename(self.file_path, backup)
                logger.warning(
                    "Corrupt JSON at %s — preserved as %s; starting empty",
                    self.file_path,
                    backup,
                )
            except OSError as e:
                logger.error(
                    "Corrupt JSON at %s and rename to backup failed (%s); "
                    "next save WILL overwrite the corrupt file",
                    self.file_path,
                    e,
                )
            return {}
        except OSError as e:
            # Read failed (permissions, transient disk error). Preserve
            # existing degraded behavior — log and start empty rather than
            # crash on bot startup.
            logger.error("Failed to read %s (%s); starting empty", self.file_path, e)
            return {}

    def _save(self) -> None:
        """Atomic + durable write: tempfile + flush + fsync, then os.replace.

        Without fsync, a power loss between rename and OS-level flush can
        leave a renamed-but-empty file; fsync forces the bytes to disk
        before the rename makes the new file visible.
        """
        directory = self.file_path.parent
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=directory,
            prefix=self.file_path.name + ".",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(self._data, tmp, indent=2, ensure_ascii=False)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
        try:
            os.replace(tmp_path, self.file_path)
        except Exception:
            # If the rename fails (e.g. Windows: target locked; macOS dev:
            # file held by another process), unlink the tempfile so it
            # doesn't accumulate in `data/`. Mirrors cache.py's pattern.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    async def _save_async(self) -> None:
        """Run _save in a worker thread so PTB's event loop isn't blocked.

        Thread-safety: `self._data` mutations must be serialized by the
        caller (PTB's single async event loop); only the disk I/O is
        offloaded. Don't introduce concurrent `to_thread` writers without
        adding a lock around `_data` mutations first.
        """
        await asyncio.to_thread(self._save)
