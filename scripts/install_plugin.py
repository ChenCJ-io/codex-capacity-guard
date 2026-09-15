#!/usr/bin/env python3
"""Register this checkout in the implicitly discovered personal marketplace."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


NAME = "codex-capacity-guard"


def register(root: Path, agents_home: Path) -> tuple[str, Path]:
    marketplace = agents_home / "plugins" / "marketplace.json"
    data = json.loads(marketplace.read_text()) if marketplace.exists() else {
        "name": "personal", "interface": {"displayName": "Personal"}, "plugins": [],
    }
    if not isinstance(data, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", data.get("name", "")) or not isinstance(data.get("plugins"), list):
        raise ValueError("Existing personal marketplace is invalid; it was not changed.")
    # Codex 0.153 resolves the implicit personal marketplace from the home
    # directory. Keep the manifest-relative link for hosts using that layout too.
    links = [agents_home.parent / "plugins" / NAME, marketplace.parent / "plugins" / NAME]
    for link in links:
        if (link.exists() or link.is_symlink()) and link.resolve() != root:
            raise ValueError("A different plugin already occupies the personal source path.")
    entry = {
        "name": NAME, "source": {"source": "local", "path": f"./plugins/{NAME}"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }
    existing = [item for item in data["plugins"] if item.get("name") == NAME]
    if existing and existing != [entry]:
        raise ValueError("An existing marketplace entry differs; it was not overwritten.")
    for link in links:
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.is_symlink() and not link.exists():
            link.symlink_to(root, target_is_directory=True)
    if not existing:
        data["plugins"].append(entry)
        temporary = marketplace.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n")
        temporary.replace(marketplace)
    return data["name"], marketplace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register-only", action="store_true", help="Register the source without calling codex plugin add")
    parser.add_argument("--codex-bin")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        name, marketplace = register(root, Path.home() / ".agents")
        print(f"Registered {NAME} in {marketplace}")
        if not args.register_only:
            desktop = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
            binary = args.codex_bin or os.environ.get("CODEX_CAPACITY_GUARD_CODEX") or (str(desktop) if desktop.exists() else shutil.which("codex"))
            if not binary:
                raise ValueError("Codex CLI was not found. The source is registered; install from the Codex Plugins screen.")
            # Capture output: config diagnostics must not accidentally print secrets.
            result = subprocess.run([binary, "plugin", "add", f"{NAME}@{name}"], capture_output=True, text=True, timeout=60)
            if result.returncode:
                print("CLI installation did not complete. Source is registered; open Codex Plugins and install Capacity Guard.", file=sys.stderr)
                return 1
            print("Plugin installed. Review its hooks in Codex, then run Capacity Guard doctor and enable.")
        else:
            print("Source registered. Install Capacity Guard from the Codex Plugins screen.")
        print("Recovery remains disabled until explicitly enabled.")
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, ValueError) else type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
