"""Native file discovery with an option to surface access errors."""

import glob
import os
import stat
from fnmatch import fnmatchcase


def path_info(path: str) -> os.stat_result | None:
    """Missing paths are optional and access errors are meaningful."""
    try:
        return os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def record_paths(root: str, *parts: str, strict: bool = False) -> list[str]:
    """Match adapter layouts. Strict discovery must distinguish empty and unreadable."""
    if not strict:
        return glob.glob(os.path.join(root, *parts), recursive="**" in parts)
    return list(_walk(root, parts))


def _walk(path, parts, ancestors=frozenset()):
    info = path_info(path)
    if info is None:
        return
    if not parts:
        if stat.S_ISREG(info.st_mode):
            yield path
        return
    if not stat.S_ISDIR(info.st_mode):
        return
    pattern, *rest = parts
    if pattern == "**":
        key = (info.st_dev, info.st_ino)
        if key in ancestors:
            return
        yield from _walk(path, rest)
        children = ancestors | {key}
        for name in _visible_entries(path):
            yield from _walk(os.path.join(path, name), parts, children)
    elif "*" in pattern:
        for name in _visible_entries(path):
            if fnmatchcase(name, pattern):
                yield from _walk(os.path.join(path, name), rest)
    else:
        yield from _walk(os.path.join(path, pattern), rest)


def _visible_entries(path: str) -> list[str]:
    try:
        return [name for name in os.listdir(path) if not name.startswith(".")]
    except (FileNotFoundError, NotADirectoryError):
        return []
