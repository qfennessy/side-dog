"""Side Dog's optional configuration file.

Side Dog already keeps state under ``~/.local/state/side-dog``: a few megabytes
of recorded activity plus the display toggles from the last run. None of that
belongs in a dotfiles repository, because all of it is disposable and most of it
is enormous. This module holds the other half - the small file a person writes
by hand, keeps in version control, and expects to be read the same way on every
machine.

Everything here is optional. With no file at all Side Dog behaves exactly as it
did before there was one, and an unreadable or half-typed file is ignored rather
than fatal, the same way a corrupt ``display.json`` already is. A pane that
refuses to start because of a stray bracket would be worse than a pane that
starts with its defaults.
"""

from __future__ import annotations

import fnmatch
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Iterable


CONFIG_HOME_ENV = "XDG_CONFIG_HOME"
CONFIG_DIR_NAME = "side-dog"
CONFIG_FILE_NAME = "config.toml"
SPACES_FILE_NAME = "spaces.toml"

# The two spellings the file uses for toggles the pane thinks of as booleans.
# The file says what a person would say; the watcher keeps its own names.
DISPLAY_ORDER = {"newest": True, "oldest": False}
DISPLAY_DETAIL = {"compact": False, "expanded": True}
DISPLAY_ORDER_NAMES = {True: "newest", False: "oldest"}
DISPLAY_DETAIL_NAMES = {True: "expanded", False: "compact"}

BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")

SPACES_HEADER = """\
# Folder sets saved by `side-dog watch --save <name>`, restored with
# `side-dog watch @<name>`.
#
# Side Dog rewrites this file whole every time it saves, so anything you add by
# hand here is lost. Put hand-written spaces and their comments in config.toml
# under [spaces] instead: both files are read, and a name saved here wins so
# that --save always takes effect.
"""


def config_home() -> Path:
    """The folder holding Side Dog's configuration, honouring XDG_CONFIG_HOME."""
    configured = os.environ.get(CONFIG_HOME_ENV)
    home = Path(configured) if configured else Path.home() / ".config"
    return home.expanduser() / CONFIG_DIR_NAME


def config_path() -> Path:
    return config_home() / CONFIG_FILE_NAME


def spaces_path() -> Path:
    return config_home() / SPACES_FILE_NAME


def read_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file, or return nothing at all if it cannot be read.

    tomllib is read-only and ships with Python 3.11, so nothing new is
    installed to get a configuration file. Every failure - missing, unreadable,
    malformed, not a table - lands here as an empty document, because the pane
    starting with its defaults beats the pane not starting.
    """
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def load_config() -> dict[str, Any]:
    return read_toml(config_path())


def expand_path(value: str) -> str:
    """Expand ``~`` and environment variables in a configured path.

    A configuration file is meant to be copied between machines, so it should be
    able to say ``~/src`` and ``$WORK/checkouts`` rather than one person's home
    folder spelled out.
    """
    return os.path.expanduser(os.path.expandvars(value))


def string_list(value: Any) -> list[str]:
    """A TOML array of non-empty strings; anything else contributes nothing."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def config_paths(document: dict[str, Any], key: str) -> list[str]:
    """One of the top-level path lists, expanded but not yet resolved."""
    return [expand_path(item) for item in string_list(document.get(key))]


def config_pins(document: dict[str, Any]) -> list[str]:
    return config_paths(document, "pin")


def config_ignores(document: dict[str, Any]) -> list[str]:
    return config_paths(document, "ignore")


def path_is_ignored(path: str | os.PathLike[str], patterns: Iterable[str]) -> bool:
    """Whether a resolved absolute path matches any ignore pattern.

    fnmatch is used rather than pathlib's own matching because ``*`` there stops
    at a path separator: ``~/.codex/worktrees/*`` is meant to cover everything
    under that folder, not only its immediate children.
    """
    text = os.fspath(path)
    return any(fnmatch.fnmatch(text, pattern) for pattern in patterns)


def config_display(document: dict[str, Any]) -> dict[str, Any]:
    """The [display] table under the names the watcher already uses.

    A value the file does not set, or sets to something unrecognized, is left
    out entirely so the watcher's own default survives it.
    """
    table = document.get("display")
    if not isinstance(table, dict):
        return {}
    settings: dict[str, Any] = {}
    order = table.get("order")
    if isinstance(order, str) and order in DISPLAY_ORDER:
        settings["newest_first"] = DISPLAY_ORDER[order]
    detail = table.get("detail")
    if isinstance(detail, str) and detail in DISPLAY_DETAIL:
        settings["expanded_history"] = DISPLAY_DETAIL[detail]
    event_filter = table.get("filter")
    if isinstance(event_filter, str):
        # The watcher checks this against the filters it knows, so an unknown
        # name falls back there rather than being rejected twice.
        settings["event_filter"] = event_filter
    return settings


def config_limit(document: dict[str, Any], default: int) -> int:
    """How many folders may share the pane, or the built-in cap."""
    table = document.get("display")
    if not isinstance(table, dict):
        return default
    limit = table.get("limit")
    # bool is an int in Python, and `limit = true` means nothing here.
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return default
    return limit


def toml_escape(character: str) -> str:
    # TOML gives \u exactly four hex digits and \U exactly eight. An emoji
    # written as \u1f600 reads back as one character and two strays.
    point = ord(character)
    return f"\\U{point:08x}" if point > 0xFFFF else f"\\u{point:04x}"


def toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = "".join(
        character if " " <= character < "\x7f" else toml_escape(character)
        for character in escaped
    )
    return f'"{escaped}"'


def toml_key(name: str) -> str:
    return name if BARE_KEY.fullmatch(name) else toml_string(name)


def render_table(name: str, values: dict[str, Any]) -> str:
    """Emit one small TOML table.

    Side Dog writes only tables it owns entirely - a first configuration file it
    just created, and the spaces file - so this covers the two shapes those
    need: strings with arrays of strings, and scalars.
    """
    lines = [f"[{name}]"]
    for key, value in values.items():
        if isinstance(value, list):
            items = ", ".join(toml_string(str(item)) for item in value)
            lines.append(f"{toml_key(key)} = [{items}]")
        elif isinstance(value, bool):
            lines.append(f"{toml_key(key)} = {'true' if value else 'false'}")
        elif isinstance(value, int):
            lines.append(f"{toml_key(key)} = {value}")
        else:
            lines.append(f"{toml_key(key)} = {toml_string(str(value))}")
    return "\n".join(lines) + "\n"


def write_private(path: Path, text: str) -> bool:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(text)
    except OSError:
        return False
    return True


def migrate_display_settings(saved: dict[str, Any]) -> bool:
    """Copy the remembered toggles into a first configuration file.

    Anyone already using Side Dog has preferences saved by the e, f and r keys.
    The first time there is a file to write them down in, they should find them
    there rather than a configuration file that silently disagrees with the pane
    they are looking at. Nothing is removed: display.json stays exactly where it
    is and those keys keep writing to it, so this is a copy and never a move.
    """
    path = config_path()
    if not saved or path.exists():
        return False
    settings: dict[str, Any] = {}
    if isinstance(saved.get("newest_first"), bool):
        settings["order"] = DISPLAY_ORDER_NAMES[saved["newest_first"]]
    if isinstance(saved.get("expanded_history"), bool):
        settings["detail"] = DISPLAY_DETAIL_NAMES[saved["expanded_history"]]
    if isinstance(saved.get("event_filter"), str):
        settings["filter"] = saved["event_filter"]
    if not settings:
        return False
    header = (
        "# Side Dog configuration. Everything in it is optional.\n"
        "# Written once from the display settings Side Dog had already\n"
        "# remembered; edit it freely, and see the README for pin, ignore\n"
        "# and spaces.\n\n"
    )
    return write_private(path, header + render_table("display", settings))


def spaces_table(document: dict[str, Any]) -> dict[str, list[str]]:
    """The [spaces] table of one document, as name to expanded folder list."""
    table = document.get("spaces")
    if not isinstance(table, dict):
        return {}
    spaces: dict[str, list[str]] = {}
    for name, value in table.items():
        folders = [expand_path(item) for item in string_list(value)]
        if folders:
            spaces[str(name)] = folders
    return spaces


def load_spaces() -> dict[str, list[str]]:
    """Saved folder sets, from the file Side Dog writes and the one you write.

    tomllib reads and never writes, so `--save` cannot edit config.toml without
    re-emitting it, and re-emitting it would lose the comments a person put
    there. It owns spaces.toml instead and rewrites that whole. A name in both
    files resolves to the saved one, so saving always takes effect.
    """
    spaces = spaces_table(load_config())
    saved = spaces_table(read_toml(spaces_path()))
    # Lookup ignores case, so `review` saved over a hand-written `Review` is
    # the same name and must replace it, not sit beside it and lose.
    saved_names = {name.casefold() for name in saved}
    spaces = {
        name: folders
        for name, folders in spaces.items()
        if name.casefold() not in saved_names
    }
    spaces.update(saved)
    return spaces


def find_space(name: str, spaces: dict[str, list[str]]) -> list[str] | None:
    """Look a saved space up by name, ignoring case."""
    folded = name.casefold()
    for candidate, folders in spaces.items():
        if candidate.casefold() == folded:
            return folders
    return None


def save_space(name: str, folders: Iterable[str]) -> Path | None:
    """Record a folder set under a name, leaving the other saved names alone.

    Returns where it was written, or nothing if the file could not be written.
    """
    saved = spaces_table(read_toml(spaces_path()))
    # Replace any existing name that differs only in case, so saving twice
    # cannot leave two spaces that @name can no longer tell apart.
    folded = name.casefold()
    saved = {key: value for key, value in saved.items() if key.casefold() != folded}
    saved[name] = [os.fspath(folder) for folder in folders]
    ordered = {key: saved[key] for key in sorted(saved)}
    path = spaces_path()
    if not write_private(path, SPACES_HEADER + "\n" + render_table("spaces", ordered)):
        return None
    return path
