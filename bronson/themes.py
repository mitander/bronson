"""Load Flume's generated palettes and follow its atomic active-theme switch."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

from textual.color import Color, ColorParseError
from textual.theme import Theme
from watchfiles import awatch

_ROLES: dict[str, dict[str, str]] = {}
_THEME_COLORS = (
    "primary",
    "secondary",
    "accent",
    "background",
    "surface",
    "panel",
    "foreground",
    "success",
    "warning",
    "error",
)


def _extras_dir() -> Path:
    if value := os.environ.get("BRONSON_FLUME_DIR"):
        return Path(value).expanduser()
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    configured = config / "bronson" / "flume"
    if configured.exists():
        return configured
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data / "nvim" / "lazy" / "flume.nvim" / "extras"


def _theme_dir() -> Path:
    value = os.environ.get("FLUME_TRACKER_THEME_DIR")
    return Path(value).expanduser() if value else _extras_dir() / "tracker-tui"


def _schema_file() -> Path:
    value = os.environ.get("FLUME_SCHEMA_FILE")
    return Path(value).expanduser() if value else _extras_dir() / "current" / "schema"


def _read_theme(path: Path) -> Theme:
    data = json.loads(path.read_text())
    name = data["name"]
    if not isinstance(name, str) or not name.startswith("flume-"):
        raise ValueError("not a Flume theme")
    if not isinstance(data["dark"], bool):
        raise ValueError("dark must be a boolean")
    colors = {key: data[key] for key in _THEME_COLORS}
    roles = data["roles"]
    source = data["variables"]
    variables = {
        "scrollbar": source["scrollbar"],
        "scrollbar-hover": source["scrollbar_hover"],
        "scrollbar-active": source["scrollbar_active"],
        "scrollbar-background": source["scrollbar_background"],
        "screen-selection-background": source["selection_background"],
        "screen-selection-foreground": source["selection_foreground"],
        "input-selection-background": source["selection_background"],
        "input-selection-foreground": source["selection_foreground"],
        "text": roles["text"],
        "text-muted": roles["text_muted"],
        "text-disabled": roles["text_muted"],
        "text-primary": roles["accent"],
        "text-secondary": roles["accent_secondary"],
        "text-accent": roles["accent_tertiary"],
        "foreground-muted": roles["text_muted"],
        "foreground-disabled": roles["text_muted"],
        "block-cursor-foreground": roles["text"],
        "block-cursor-background": source["cursor"],
        "block-cursor-blurred-foreground": roles["text_subtle"],
        "block-cursor-blurred-background": source["cursor"],
        "block-hover-background": source["cursor"],
        "border": source["border_focus"],
        "border-blurred": source["border"],
        "surface-active": source["cursor"],
        "footer-foreground": roles["text_subtle"],
        "footer-background": data["panel"],
        "footer-key-foreground": roles["accent"],
        "footer-description-foreground": roles["text_subtle"],
        "input-cursor-background": roles["text"],
        "input-cursor-foreground": data["background"],
        "button-foreground": roles["text"],
        "button-color-foreground": source["selection_foreground"],
        "link-background-hover": source["cursor"],
        "link-color": roles["accent"],
        "link-color-hover": roles["accent_tertiary"],
    }
    for role in ("warning", "error", "success"):
        variables["text-" + role] = roles[role]
    for role in ("primary", "secondary", "accent", "warning", "error", "success"):
        variables[role + "-muted"] = source["cursor"]
    for level, role in enumerate(
        ("accent", "accent", "accent_secondary", "text", "text_subtle", "text_muted"), 1
    ):
        variables[f"markdown-h{level}-color"] = roles[role]
    # Reject invalid palette colors before Textual applies them to the running UI.
    for value in (*colors.values(), *roles.values()):
        if not isinstance(value, str):
            raise ValueError("theme colors must be strings")
        Color.parse(value)
    for value in variables.values():
        if not isinstance(value, str):
            raise ValueError("theme variables must be strings")
        # Textual CSS accepts a separate opacity suffix, unlike Color.parse.
        parts = value.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].endswith("%"):
            if not 0 <= float(parts[1][:-1]) <= 100:
                raise ValueError("invalid color opacity")
            value = parts[0]
        Color.parse(value)
    theme = Theme(
        name=name,
        **colors,
        dark=data["dark"],
        luminosity_spread=0.0,
        text_alpha=1.0,
        variables=variables,
    )
    _ROLES[name] = dict(roles)
    return theme


def load_flume_themes(_app: str = "actions") -> list[Theme]:
    _ROLES.clear()
    themes = []
    for path in sorted(_theme_dir().glob("flume-*.json")):
        try:
            themes.append(_read_theme(path))
        except (OSError, KeyError, TypeError, ValueError, ColorParseError):
            continue
    return themes


def theme_roles(name: str) -> dict[str, str] | None:
    return _ROLES.get(name)


def active_flume_theme() -> str | None:
    try:
        schema = _schema_file().read_text().strip()
    except (OSError, UnicodeError):
        return None
    name = f"flume-{schema}"
    return name if name in _ROLES else None


async def watch_flume_theme_changes() -> AsyncIterator[str]:
    # Watch the parent, not the symlink target: Flume swaps current atomically.
    schema = _schema_file()
    extras = schema.parent.parent
    paths = [extras] if extras.is_dir() else []
    # Legacy overrides can point to a standalone, directly updated schema file.
    if os.environ.get("FLUME_SCHEMA_FILE") and schema.parent.is_dir():
        paths.append(schema.parent)
    if not paths:
        return
    previous = active_flume_theme()
    try:
        async for _changes in awatch(*paths, recursive=False, watch_filter=None):
            current = active_flume_theme()
            if current and current != previous:
                previous = current
                yield current
    except OSError:
        return  # An unavailable optional theme directory must not take down the dashboard.
