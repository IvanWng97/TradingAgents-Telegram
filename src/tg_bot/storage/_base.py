"""JSON-backed dict storage with atomic writes."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any


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
            with open(self.file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        """Atomic write: tempfile in the same dir, then os.replace."""
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
            tmp_path = tmp.name
        os.replace(tmp_path, self.file_path)

    async def _save_async(self) -> None:
        """Run _save in a worker thread so PTB's event loop isn't blocked."""
        await asyncio.to_thread(self._save)
