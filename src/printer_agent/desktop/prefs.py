"""Per-user desktop preferences.

Kept out of ``agent.yaml`` on purpose: the agent config is the service contract,
and a UI colour choice has no business round-tripping through it (or through the
elevated ProgramData file the service reads).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .theme import ACCENT_PRESETS, ThemeMode


def preferences_path() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / ".config"
    return base / "printer-agent" / "ui.json"


@dataclass(slots=True)
class Preferences:
    theme_mode: str = ThemeMode.system.value
    accent: str = "system"
    live_status: bool = True
    poll_interval_s: int = 10
    window_width: int = 1120
    window_height: int = 720

    def normalized(self) -> "Preferences":
        modes = {mode.value for mode in ThemeMode}
        return Preferences(
            theme_mode=self.theme_mode if self.theme_mode in modes else ThemeMode.system.value,
            accent=self.accent if self.accent in ACCENT_PRESETS else "system",
            live_status=bool(self.live_status),
            poll_interval_s=min(300, max(3, int(self.poll_interval_s or 10))),
            window_width=max(960, int(self.window_width or 1120)),
            window_height=max(640, int(self.window_height or 720)),
        )

    @property
    def mode(self) -> ThemeMode:
        return ThemeMode(self.theme_mode)


def load_preferences(path: Path | None = None) -> Preferences:
    target = path or preferences_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Preferences()
    if not isinstance(raw, dict):
        return Preferences()
    known = {field for field in Preferences.__slots__}
    return Preferences(**{key: value for key, value in raw.items() if key in known}).normalized()


def save_preferences(preferences: Preferences, path: Path | None = None) -> None:
    target = path or preferences_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(asdict(preferences.normalized()), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        # A read-only profile must not take the whole app down over a colour choice.
        pass
