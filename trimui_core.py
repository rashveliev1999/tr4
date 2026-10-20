# -*- coding: utf-8 -*-
"""NX REDUX-aware filesystem helpers. No GUI dependencies."""
from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path
from typing import Callable, Iterable

APP_VERSION = "0.4.0"
ROM_EXTS = {
    ".zip", ".7z", ".rar", ".iso", ".chd", ".cue", ".bin", ".rom", ".nes",
    ".smc", ".sfc", ".gba", ".gbc", ".gb", ".nds", ".3ds", ".cia", ".n64",
    ".z64", ".v64", ".md", ".gen", ".sms", ".gg", ".sg", ".pce", ".pbp",
    ".cso", ".cdi", ".gdi", ".wad", ".wud", ".wbfs", ".dol", ".elf", ".m3u",
    ".adf", ".dsk", ".tap", ".prg", ".lnx", ".j64", ".a26", ".a78", ".ws",
    ".wsc", ".mgw", ".p8", ".p8.png", ".love", ".sh", ".pkg", ".3dsx", ".xci",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
ART_DIRS = {
    "screenshot": "screenshot",
    "boxart": "boxart",
    "boxart2d": "boxart2d",
    "wheel": "wheel",
    "mix": "mix",
}
SKIP_DIRS = {
    ".media", ".retroachievements", ".git", "__pycache__", "node_modules",
    "lost+found", "manuals", "manual", "videos", "video", "screenshots",
    "covers", "images", "artwork", "bios", "saves", "states", "cheats",
    "cache", "caches", "system volume information", "$recycle.bin", "__macosx",
}

ProgressCallback = Callable[[int, str], None]


def locate_roms(root: str | Path, relative: str = "Roms") -> Path | None:
    """Find the configured Roms directory, tolerating capitalization differences."""
    base = Path(root).expanduser()
    if not base.exists() or not base.is_dir():
        return None
    rel = Path(relative.strip() or "Roms")
    candidate = base / rel
    if candidate.is_dir():
        return candidate
    # NX REDUX uses Roms; tolerate a user changing case on a Windows-formatted card.
    wanted = rel.name.casefold()
    try:
        for p in base.iterdir():
            if p.is_dir() and p.name.casefold() == wanted:
                return p
    except OSError:
        return None
    return None


def folder_tag(folder_name: str) -> str | None:
    """Return NX REDUX / ScreenScraper system tag at the end of a folder name."""
    m = re.search(r"\(([^()]+)\)\s*$", folder_name)
    return m.group(1).strip() if m else None


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _dir_size(path: Path, exclude_media: bool = True) -> int:
    total = 0
    for current, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".") and (not exclude_media or d.casefold() not in {".media", ".retroachievements"})]
        for name in files:
            if name.startswith("."):
                continue
            total += _file_size(Path(current) / name)
    return total


def scan_library(
    root: str | Path,
    roms_rel: str = "Roms",
    stop_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> list[dict]:
    """Scan an NX REDUX library without touching its files.

    Systems are folders directly under Roms. Artwork and hidden metadata folders are
    never interpreted as games. Multi-disc folders containing a same-named .m3u/.cue
    are represented as one game, as in NX REDUX's artwork manager.
    """
    romroot = locate_roms(root, roms_rel)
    if not romroot:
        raise FileNotFoundError(f"Не найдена папка игр: {Path(root) / roms_rel}")

    try:
        systems = sorted(
            (p for p in romroot.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name.casefold() not in SKIP_DIRS),
            key=lambda p: p.name.casefold(),
        )
    except OSError as exc:
        raise OSError(f"Не удалось прочитать {romroot}: {exc}") from exc

    games: list[dict] = []
    visited = 0
    for system in systems:
        if stop_event and stop_event.is_set():
            break
        for current, dirs, files in os.walk(system):
            if stop_event and stop_event.is_set():
                break
            current_path = Path(current)
            dirs[:] = sorted(
                [d for d in dirs if not d.startswith(".") and d.casefold() not in SKIP_DIRS],
                key=str.casefold,
            )
            # Hidden/system files are not ROM entries.
            names = [n for n in files if not n.startswith(".") and not n.startswith("._") and n.casefold() != ".ds_store"]
            # NX REDUX treats a folder with a same-named M3U/CUE as one multi-disc game.
            if current_path != system:
                folder_stem = current_path.name.casefold()
                same_named = [n for n in names if Path(n).stem.casefold() == folder_stem and Path(n).suffix.casefold() in {".m3u", ".cue"}]
                if same_named:
                    same_named.sort(key=lambda n: (Path(n).suffix.casefold() != ".m3u", n.casefold()))
                    selected = current_path / same_named[0]
                    games.append({
                        "path": selected,
                        "name": current_path.name,
                        "stem": current_path.name,
                        "platform": system.name,
                        "size": _dir_size(current_path),
                        "art_base": current_path.parent,
                        "is_folder_game": True,
                        "relative": str(current_path.relative_to(romroot)),
                        "suffix": selected.suffix.lower(),
                        "tag": folder_tag(system.name),
                    })
                    dirs[:] = []
                    visited += len(names)
                    if progress and (visited % 80 == 0):
                        progress(visited, str(current_path))
                    continue

            # .cue normally represents a track set; don't list its paired .bin as a second game.
            cue_stems = {Path(n).stem.casefold() for n in names if Path(n).suffix.casefold() in {".cue", ".m3u"}}
            # Prefer .m3u over same-folder .cue entries when one exists for a multi-disc set.
            for name in names:
                p = current_path / name
                suffix = p.suffix.casefold()
                if suffix not in ROM_EXTS and not p.name.casefold().endswith(".p8.png"):
                    continue
                if suffix == ".bin" and p.stem.casefold() in cue_stems:
                    continue
                if suffix == ".cue" and p.stem.casefold() in {Path(n).stem.casefold() for n in names if Path(n).suffix.casefold() == ".m3u"}:
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                games.append({
                    "path": p,
                    "name": p.stem,
                    "stem": p.stem,
                    "platform": system.name,
                    "size": size,
                    "art_base": current_path,
                    "is_folder_game": False,
                    "relative": str(p.relative_to(romroot)),
                    "suffix": suffix,
                    "tag": folder_tag(system.name),
                })
                visited += 1
                if progress and (visited % 80 == 0):
                    progress(visited, str(p))

    games.sort(key=lambda g: (g["platform"].casefold(), g["name"].casefold(), str(g["path"]).casefold()))
    if progress:
        progress(len(games), "Готово")
    return games


def media_dir(game: dict) -> Path:
    return Path(game["art_base"]) / ".media"


def asset_path_for_game(game: dict, asset_type: str = "screenshot", include_fallback: bool = True) -> Path | None:
    """Return existing artwork for an NX REDUX game, with the documented fallback."""
    base = media_dir(game)
    stem = str(game["stem"])
    kinds = {"screenshot": "screenshot", "boxart": "boxart", "boxart2d": "boxart2d", "wheel": "wheel", "mix": "mix"}
    folder = kinds.get(asset_type, "screenshot")
    search_dirs = [base / folder]
    if asset_type == "screenshot" and include_fallback:
        # NX REDUX also displays user-supplied .media/<game>.png when no screenshot exists.
        search_dirs.append(base)
    for directory in search_dirs:
        for ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            candidate = directory / f"{stem}{ext}"
            if candidate.is_file():
                return candidate
        try:
            wanted = stem.casefold()
            if directory.is_dir():
                for candidate in directory.iterdir():
                    if candidate.is_file() and candidate.stem.casefold() == wanted and candidate.suffix.casefold() in IMAGE_EXTS:
                        return candidate
        except OSError:
            pass
    return None


def build_asset_index(games: Iterable[dict]) -> dict[tuple[str, str], Path | None]:
    """Index all game artwork in a small number of directory reads.

    Large libraries often have hundreds of missing covers. Looking through the
    entire screenshot folder once for every missing game is quadratic and makes an
    SD-card scan feel frozen. This function indexes each distinct artwork folder
    once and returns the same (game-path, asset-type) keys used by the GUI cache.
    """
    game_list = list(games)
    by_key: dict[tuple[str, str], Path | None] = {}
    games_by_base: dict[str, list[dict]] = {}
    for game in game_list:
        games_by_base.setdefault(str(Path(game["art_base"])), []).append(game)

    # Priority is deterministic: PNG first, then JPEG/WebP/BMP; Screenshot folder
    # has priority over the documented .media/<game>.png fallback.
    extension_priority = {".png": 0, ".jpg": 1, ".jpeg": 2, ".webp": 3, ".bmp": 4}
    for base_text, base_games in games_by_base.items():
        wanted_stems = {str(game["stem"]).casefold() for game in base_games}
        media = Path(base_text) / ".media"
        found: dict[tuple[str, str], Path] = {}
        for kind, folder_name in ART_DIRS.items():
            directory = media / folder_name
            try:
                items = sorted(
                    (p for p in directory.iterdir() if p.is_file() and p.suffix.casefold() in IMAGE_EXTS),
                    key=lambda p: (extension_priority.get(p.suffix.casefold(), 99), p.name.casefold()),
                )
            except OSError:
                items = []
            for path in items:
                stem = path.stem.casefold()
                if stem in wanted_stems:
                    found.setdefault((stem, kind), path)
        # Fallback: NX REDUX also uses .media/<game>.png for Screenshot artwork.
        try:
            fallback_items = sorted(
                (p for p in media.iterdir() if p.is_file() and p.suffix.casefold() in IMAGE_EXTS),
                key=lambda p: (extension_priority.get(p.suffix.casefold(), 99), p.name.casefold()),
            )
        except OSError:
            fallback_items = []
        for path in fallback_items:
            stem = path.stem.casefold()
            if stem in wanted_stems:
                found.setdefault((stem, "screenshot"), path)
        for game in base_games:
            stem = str(game["stem"]).casefold()
            for kind in ART_DIRS:
                by_key[(str(game["path"]), kind)] = found.get((stem, kind))
    return by_key


def asset_destination(game: dict, asset_type: str = "screenshot") -> Path:
    folder = ART_DIRS.get(asset_type, "screenshot")
    return media_dir(game) / folder / f"{game['stem']}.png"


def system_asset_destination(root: str | Path, roms_rel: str, platform: str, asset_type: str) -> Path:
    media = Path(root) / roms_rel / platform / ".media"
    if asset_type == "logo":
        return media / "logo.png"
    if asset_type == "background":
        return media / "bg.png"
    if asset_type == "wallpaper":
        return Path(root) / "bg.png"
    raise ValueError(f"Unknown system asset type: {asset_type}")


def art_coverage(games: Iterable[dict], asset_type: str = "screenshot") -> tuple[int, int]:
    total = 0
    present = 0
    for g in games:
        total += 1
        if asset_path_for_game(g, asset_type=asset_type):
            present += 1
    return present, total


def image_count(path: str | Path) -> int:
    p = Path(path)
    if not p.is_dir():
        return 0
    try:
        return sum(1 for f in p.iterdir() if f.is_file() and f.suffix.casefold() in IMAGE_EXTS)
    except OSError:
        return 0


def sha256_file(path: str | Path, stop_event: threading.Event | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            if stop_event and stop_event.is_set():
                raise InterruptedError("Операция отменена пользователем")
            block = handle.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def find_duplicates(games: list[dict], stop_event: threading.Event | None = None, progress: ProgressCallback | None = None) -> list[list[dict]]:
    by_size: dict[int, list[dict]] = {}
    for game in games:
        by_size.setdefault(int(game["size"]), []).append(game)
    candidates = [group for group in by_size.values() if len(group) > 1]
    total = sum(len(group) for group in candidates)
    done = 0
    found: list[list[dict]] = []
    for group in candidates:
        if stop_event and stop_event.is_set():
            break
        by_hash: dict[str, list[dict]] = {}
        for game in group:
            if stop_event and stop_event.is_set():
                break
            try:
                checksum = sha256_file(game["path"], stop_event)
            except (OSError, InterruptedError):
                continue
            by_hash.setdefault(checksum, []).append(game)
            done += 1
            if progress:
                progress(done, f"Проверено {done} из {total} файлов")
        found.extend(matches for matches in by_hash.values() if len(matches) > 1)
    return found


def inspect_card(root: str | Path, roms_rel: str = "Roms", games: list[dict] | None = None) -> dict:
    """Read-only NX REDUX layout diagnostics."""
    rootp = Path(root).expanduser()
    romroot = locate_roms(rootp, roms_rel)
    systems: list[Path] = []
    if romroot and romroot.is_dir():
        try:
            systems = sorted((p for p in romroot.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name.casefold())
        except OSError:
            systems = []
    untagged = [p.name for p in systems if folder_tag(p.name) is None]
    screenshot_count = 0
    for s in systems:
        screenshot_count += image_count(s / ".media" / "screenshot")
    missing = max(0, len(games or []) - sum(1 for g in (games or []) if asset_path_for_game(g, "screenshot")))
    checks = [
        {"title": "Корень microSD доступен", "ok": rootp.is_dir(), "detail": str(rootp)},
        {"title": "Папка Roms найдена", "ok": romroot is not None, "detail": str(romroot) if romroot else f"Не найдена: {rootp / roms_rel}"},
        {"title": "Есть системные папки", "ok": bool(systems), "detail": f"Найдено систем: {len(systems)}"},
        {"title": "Файлы сохранений", "ok": (rootp / "Saves").exists() or (rootp / ".userdata").exists(), "detail": "Saves/ и/или .userdata/ найдены" if (rootp / "Saves").exists() or (rootp / ".userdata").exists() else "Не обнаружены; это не обязательно ошибка"},
        {"title": "Папка BIOS", "ok": (rootp / "Bios").exists() or (rootp / "BIOS").exists(), "detail": "Bios/ найдена" if (rootp / "Bios").exists() or (rootp / "BIOS").exists() else "Не найдена; создаётся не на всех картах"},
        {"title": "Теги систем для Artwork Manager", "ok": len(untagged) == 0, "detail": "Все папки имеют тег (TAG)" if not untagged else "Без тега: " + ", ".join(untagged[:8]) + (" …" if len(untagged) > 8 else "")},
    ]
    return {
        "root": rootp,
        "romroot": romroot,
        "systems": systems,
        "untagged": untagged,
        "screenshot_count": screenshot_count,
        "missing_screenshots": missing,
        "checks": checks,
    }
