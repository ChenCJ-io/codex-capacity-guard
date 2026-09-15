"""Local configuration. No model credentials are read or stored by the guard."""

from __future__ import annotations

import json
import math
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve()


def state_dir() -> Path:
    return Path(os.environ.get("CODEX_CAPACITY_GUARD_HOME", str(codex_home() / "capacity-guard"))).expanduser().resolve()


def find_codex() -> str:
    explicit = os.environ.get("CODEX_CAPACITY_GUARD_CODEX") or os.environ.get("CODEX_CLI_PATH")
    if explicit:
        return explicit
    desktop = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    if desktop.is_file():
        return str(desktop)
    return shutil.which("codex") or "codex"


@dataclass(frozen=True)
class Settings:
    initial_delay: float = 30.0
    max_delay: float = 120.0
    poll_interval: float = 2.0
    jitter: float = 0.15
    codex_bin: str = ""
    socket_path: str | None = None
    backend: str = "auto"

    def __post_init__(self) -> None:
        for value in (self.initial_delay, self.max_delay, self.poll_interval, self.jitter):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError("Intervals and jitter must be finite numbers.")
        if not 1 <= self.initial_delay <= self.max_delay:
            raise ValueError("Delays must satisfy 1 <= initial_delay <= max_delay.")
        if not 0.2 <= self.poll_interval <= 60:
            raise ValueError("poll_interval must be between 0.2 and 60 seconds.")
        if not 0 <= self.jitter <= 0.5:
            raise ValueError("jitter must be between 0 and 0.5.")
        if self.backend not in {"auto", "app-server", "desktop"}:
            raise ValueError("backend must be auto, app-server, or desktop.")

    def delay(self, attempts: int, random_fraction: float = 0.5) -> float:
        # Bound the exponent even after months of capacity failures.
        base = min(self.max_delay, self.initial_delay * 2 ** min(max(attempts, 0), 20))
        return base * (1 + self.jitter * (2 * random_fraction - 1))


def load_settings(directory: Path | None = None) -> Settings:
    path = (directory or state_dir()) / "config.json"
    values = json.loads(path.read_text()) if path.exists() else {}
    return Settings(**values)


def save_settings(settings: Settings, directory: Path | None = None) -> None:
    directory = directory or state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "config.json"
    temporary = directory / "config.json.tmp"
    temporary.write_text(json.dumps(asdict(settings), indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)
