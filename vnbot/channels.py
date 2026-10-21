"""On/off switches for the notification channels, set from the web page (/ai).

Saved in ``channels.json`` under the data directory, so a restart keeps them.
A switch only silences or enables something already configured; it never
replaces the configuration (a Zoom app without credentials stays off).

- ``telegram``: broadcasts to the Telegram report chats (daily reports, AI
  critic, alerts). Commands are still answered, so it can be turned back on.
- ``zoom_notify``: the same broadcasts to Zoom.
- ``zoom_commands``: commands typed in Zoom (validation and uninstall events
  are still handled).
- ``intraday``: the intraday alerts; its default is ``INTRADAY_ALERTS``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from vnbot.jobs import write_text_atomic

NAMES = ("telegram", "zoom_notify", "zoom_commands", "intraday")


class Switches:
    def __init__(self, data_dir: Path | None = None, defaults: dict | None = None):
        self.path = Path(data_dir) / "channels.json" if data_dir else None
        self.defaults = dict(defaults or {})
        self._lock = threading.Lock()
        self._mem: dict = {}

    def _read(self) -> dict:
        if self.path is None:
            return dict(self._mem)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def on(self, name: str) -> bool:
        with self._lock:
            value = self._read().get(name)
        return bool(self.defaults.get(name, True)) if value is None else bool(value)

    def set(self, name: str, on: bool) -> None:
        if name not in NAMES:
            raise ValueError(f"Kênh không hợp lệ: {name}")
        with self._lock:
            data = self._read()
            data[name] = bool(on)
            if self.path is None:
                self._mem = data
            else:
                write_text_atomic(self.path, json.dumps(data))
