"""Plugin upgrade rules - exact filename matching for VST upgrades.

Maps old plugin filenames to new ones via config file.
Only explicit rules are applied - no automatic pattern guessing.
"""

from __future__ import annotations

import pathlib

from abletoolz.misc import default_vst_dirs


def find_plugin(filename: str, scan_paths: list[pathlib.Path] | None = None) -> pathlib.Path | None:
    """Find a plugin file on the system by filename."""
    target = filename.lower()
    for base in (scan_paths or default_vst_dirs()):
        if not base.exists():
            continue
        for ext in ("*.dll", "*.vst3"):
            for found in base.rglob(ext):
                if found.name.lower() == target:
                    return found
    return None


def get_upgrade(source: str, rules: dict[str, list[str]],
                scan_paths: list[pathlib.Path] | None = None) -> tuple[str, pathlib.Path] | None:
    """Get upgrade for a plugin if one exists and is installed.

    Args:
        source: Plugin filename (e.g. "FabFilter Pro-Q.64.dll")
        rules: Dict mapping source filenames to list of target filenames
        scan_paths: Directories to search for plugins

    Returns:
        Tuple of (target_filename, target_path) or None
    """
    source_name = pathlib.Path(source).name.lower()

    # Find matching rule (case-insensitive)
    targets = None
    for key, value in rules.items():
        if key.lower() == source_name:
            targets = value
            break

    if not targets:
        return None

    # Try each target in order
    for target in targets:
        path = find_plugin(target, scan_paths)
        if path:
            return (target, path)

    return None
