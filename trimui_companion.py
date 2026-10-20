# -*- coding: utf-8 -*-
"""TrimUI Companion — desktop helper tailored to NX REDUX 2.x.

This app manages files in user folders only. It never edits .system/ or flashes firmware.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Callable
import tkinter as tk
from tkinter import filedialog as fd, messagebox as mb

import customtkinter as ctk
from PIL import Image, ImageOps

try:
    import paramiko
except Exception:
    paramiko = None

from trimui_core import (
    APP_VERSION, ART_DIRS, IMAGE_EXTS, asset_destination, asset_path_for_game, build_asset_index,
    art_coverage, find_duplicates, folder_tag, image_count, inspect_card, locate_roms,
    media_dir, scan_library, system_asset_destination,
)

APP_NAME = "TrimUI Companion NX"
CFG_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "TrimUICompanion"
CFG_FILE = CFG_DIR / "settings.json"
KNOWN_HOSTS = CFG_DIR / "known_hosts"

C = {
    "bg": "#080e17", "panel": "#101a28", "panel2": "#142235", "panel3": "#192b40",
    "line": "#23374d", "text": "#f2f6fb", "muted": "#9bacc0", "dim": "#71849b",
    "accent": "#58e0b2", "accent_hover": "#32b88f", "accent_dark": "#173d35",
    "blue": "#82b8ff", "yellow": "#ffc86a", "red": "#ff7e8d", "white": "#ffffff",
}

ASSET_LABELS = {
    "screenshot": "Скриншот игры",
    "boxart": "3D-обложка",
    "boxart2d": "Плоская обложка (2D)",
    "wheel": "Логотип игры (Wheel)",
    "mix": "Mix-изображение",
}


def read_settings() -> dict:
    try:
        data = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_settings(data: dict) -> None:
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    temp = CFG_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(CFG_FILE)


def fmt_size(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def reveal_path(path: str | Path) -> None:
    p = Path(path)
    try:
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as exc:
        raise OSError(f"Не удалось открыть путь: {p}\n{exc}") from exc


def save_as_png(source: str | Path, destination: str | Path) -> None:
    """Validate an image and write a normalized PNG for NX REDUX."""
    src = Path(source)
    dst = Path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as original:
        original.verify()
    with Image.open(src) as original:
        img = ImageOps.exif_transpose(original)
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")
        img.save(dst, format="PNG", optimize=True)


class ConfirmHostKey(paramiko.MissingHostKeyPolicy if paramiko else object):
    """Ask the user before trusting an unknown SSH server key; do not silently trust it."""
    def __init__(self, app: "TrimUIApp"):
        self.app = app

    def missing_host_key(self, client, hostname, key):
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
        answer: dict[str, bool] = {}
        ready = threading.Event()

        def ask():
            answer["trust"] = mb.askyesno(
                "Проверка SSH-ключа",
                f"Устройство {hostname} ещё не сохранено в списке доверенных.\n\n"
                f"Тип ключа: {key.get_name()}\nОтпечаток: {fingerprint}\n\n"
                "Принимай ключ только если подключаешься к своей консоли. Доверять этому ключу?",
                parent=self.app,
            )
            ready.set()

        self.app.after(0, ask)
        if not ready.wait(180):
            raise RuntimeError("Не получено подтверждение SSH-ключа.")
        if not answer.get("trust"):
            raise RuntimeError("SSH-ключ не подтверждён.")
        client.get_host_keys().add(hostname, key.get_name(), key)
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        client.save_host_keys(str(KNOWN_HOSTS))


class TrimUIApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("green")
        self.title("TrimUI Companion NX — NX REDUX edition")
        self.geometry("1380x870")
        self.minsize(1080, 720)
        self.configure(fg_color=C["bg"])
        self._try_set_icon()

        self.cfg = read_settings()
        self.sd_var = ctk.StringVar(value=self.cfg.get("sd_root", ""))
        self.roms_var = ctk.StringVar(value=self.cfg.get("roms_rel", "Roms"))
        self.backup_var = ctk.StringVar(value=self.cfg.get("backup_root", str(Path.home() / "TrimUI NX Backups")))
        # Fixed to the NX REDUX layout: .media/screenshot per system/game folder.
        self.host_var = ctk.StringVar(value=self.cfg.get("ssh_host", ""))
        self.port_var = ctk.StringVar(value=str(self.cfg.get("ssh_port", 22)))
        self.user_var = ctk.StringVar(value=self.cfg.get("ssh_user", "root"))
        self.password_var = ctk.StringVar(value="")
        self.remote_var = ctk.StringVar(value=self.cfg.get("ssh_path", "/mnt/SDCARD"))

        self.games: list[dict] = []
        self.art_cache: dict[tuple[str, str], Path | None] = {}
        self.platform_counts: dict[str, list[dict]] = {}
        self.expanded_platforms: set[str] = set()
        self.visible_game_rows: dict[str, int] = {}
        self.selected_game: dict | None = None
        self.selected_art_platform = ""
        self.page_buttons: dict[str, ctk.CTkButton] = {}
        self.busy = False
        self.stop_event = threading.Event()
        self.task_token = 0
        self.anim_token = 0
        self._active_page: ctk.CTkFrame | None = None
        self._previous_page: ctk.CTkFrame | None = None
        self._photos: dict[str, ctk.CTkImage] = {}
        self.ssh_client = None
        self.sftp = None
        self.sftp_remote_listing: list[dict] = []
        self.current_page = "overview"
        self.last_scan_time = "Ещё не сканировали"
        self.last_scan_error = ""

        self._build_shell()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<F5>", lambda _e: self.scan_async())
        self.bind_all("<Control-f>", self._focus_search)
        self.show_page("overview", animate=False)
        self.after(300, self._initial_scan_if_possible)

    def _try_set_icon(self):
        path = self._resource_path("TrimUICompanion.ico")
        try:
            if path.exists():
                self.iconbitmap(str(path))
        except Exception:
            pass

    @staticmethod
    def _resource_path(relative: str) -> Path:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        return base / relative

    def _build_shell(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.sidebar = ctk.CTkFrame(self, width=242, corner_radius=0, fg_color="#0e1825")
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)

        brand = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand.pack(fill="x", padx=21, pady=(24, 28))
        mark = ctk.CTkFrame(brand, width=42, height=42, corner_radius=13, fg_color=C["accent_dark"], border_width=1, border_color="#2c6c5b")
        mark.pack(side="left", padx=(0, 11))
        mark.pack_propagate(False)
        ctk.CTkLabel(mark, text="◆", font=ctk.CTkFont(size=24, weight="bold"), text_color=C["accent"]).pack(expand=True)
        brand_text = ctk.CTkFrame(brand, fg_color="transparent")
        brand_text.pack(side="left")
        ctk.CTkLabel(brand_text, text="TRIMUI", font=ctk.CTkFont(family="Segoe UI", size=19, weight="bold"), text_color=C["text"]).pack(anchor="w")
        ctk.CTkLabel(brand_text, text="COMPANION  /  NX REDUX", font=ctk.CTkFont(size=9, weight="bold"), text_color=C["accent"]).pack(anchor="w", pady=(1, 0))

        ctk.CTkLabel(self.sidebar, text="РАБОЧЕЕ ПРОСТРАНСТВО", font=ctk.CTkFont(size=9, weight="bold"), text_color=C["dim"]).pack(anchor="w", padx=22, pady=(0, 9))
        nav = [
            ("overview", "⌂", "Обзор"),
            ("library", "▦", "Игровая библиотека"),
            ("art", "▧", "Обложки и оформление"),
            ("backup", "◉", "Резервные копии"),
            ("health", "✣", "Диагностика карты"),
            ("transfer", "⌁", "Wi-Fi / SFTP"),
            ("settings", "⚙", "Настройки"),
        ]
        for key, icon, label in nav:
            btn = ctk.CTkButton(
                self.sidebar, text=f"{icon}    {label}", anchor="w", height=43, corner_radius=10,
                fg_color="transparent", hover_color="#1b2b3c", text_color="#c9d5e4",
                font=ctk.CTkFont(family="Segoe UI", size=12), command=lambda k=key: self.show_page(k),
            )
            btn.pack(fill="x", padx=12, pady=3)
            self.page_buttons[key] = btn

        ctk.CTkFrame(self.sidebar, fg_color=C["line"], height=1).pack(fill="x", padx=19, pady=(23, 13))
        ctk.CTkLabel(self.sidebar, text="MICROSD", font=ctk.CTkFont(size=9, weight="bold"), text_color=C["dim"]).pack(anchor="w", padx=22, pady=(0, 7))
        self.device_label = ctk.CTkLabel(self.sidebar, text="●  Карта не выбрана", font=ctk.CTkFont(size=11), text_color=C["yellow"], anchor="w", wraplength=195, justify="left")
        self.device_label.pack(fill="x", padx=22)
        self.sidebar_bottom = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        self.sidebar_bottom.pack(side="bottom", fill="x", padx=20, pady=17)
        ctk.CTkLabel(self.sidebar_bottom, text=f"Версия {APP_VERSION}", font=ctk.CTkFont(size=10), text_color="#60738a").pack(anchor="w")
        ctk.CTkLabel(self.sidebar_bottom, text="Неофициальный помощник", font=ctk.CTkFont(size=9), text_color="#52647a").pack(anchor="w", pady=(3, 0))

        self.main = ctk.CTkFrame(self, fg_color=C["bg"], corner_radius=0)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(self.main, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=28, pady=(20, 9))
        top.grid_columnconfigure(0, weight=1)
        self.page_title = ctk.CTkLabel(top, text="Обзор", font=ctk.CTkFont(family="Segoe UI", size=27, weight="bold"), text_color=C["text"])
        self.page_title.grid(row=0, column=0, sticky="w")
        self.version_pill = ctk.CTkLabel(top, text="NX REDUX EDITION", fg_color="#142a29", text_color=C["accent"], corner_radius=8, font=ctk.CTkFont(size=9, weight="bold"), padx=11, pady=7)
        self.version_pill.grid(row=0, column=1, sticky="e")

        self.page_host = ctk.CTkFrame(self.main, fg_color="transparent", corner_radius=0)
        self.page_host.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 10))
        self.page_host.grid_columnconfigure(0, weight=1)
        self.page_host.grid_rowconfigure(0, weight=1)

        footer = ctk.CTkFrame(self.main, fg_color="#101a28", corner_radius=10)
        footer.grid(row=2, column=0, sticky="ew", padx=28, pady=(0, 18))
        footer.grid_columnconfigure(0, weight=1)
        self.status_label = ctk.CTkLabel(footer, text="Выбери папку microSD с NX REDUX.", anchor="w", text_color=C["muted"], font=ctk.CTkFont(size=10))
        self.status_label.grid(row=0, column=0, sticky="ew", padx=14, pady=9)
        self.task_progress = ctk.CTkProgressBar(footer, width=138, height=5, mode="indeterminate", progress_color=C["accent"], fg_color="#243449")
        self.task_progress.grid(row=0, column=1, sticky="e", padx=(4, 14), pady=9)
        self.task_progress.grid_remove()

    def card(self, parent, title: str, value: str, caption: str, accent: str = C["accent"], clickable: Callable | None = None):
        frame = ctk.CTkFrame(parent, fg_color=C["panel"], corner_radius=15, border_width=1, border_color=C["line"])
        bar = ctk.CTkFrame(frame, width=3, corner_radius=0, fg_color=accent)
        bar.pack(side="left", fill="y", padx=(0, 0))
        body = ctk.CTkFrame(frame, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True, padx=15, pady=13)
        ctk.CTkLabel(body, text=title.upper(), font=ctk.CTkFont(size=9, weight="bold"), text_color=C["dim"]).pack(anchor="w")
        val = ctk.CTkLabel(body, text=value, font=ctk.CTkFont(size=25, weight="bold"), text_color=accent)
        val.pack(anchor="w", pady=(4, 1))
        ctk.CTkLabel(body, text=caption, font=ctk.CTkFont(size=10), text_color=C["muted"], wraplength=220, justify="left").pack(anchor="w")
        if clickable:
            for w in (frame, bar, body, *body.winfo_children()):
                w.bind("<Button-1>", lambda _e: clickable(), add="+")
                try: w.configure(cursor="hand2")
                except Exception: pass
        return frame, val

    def section(self, parent, title: str, subtitle: str | None = None):
        frame = ctk.CTkFrame(parent, fg_color=C["panel"], corner_radius=15, border_width=1, border_color=C["line"])
        ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(size=15, weight="bold"), text_color=C["text"]).pack(anchor="w", padx=18, pady=(16, 4))
        if subtitle:
            ctk.CTkLabel(frame, text=subtitle, font=ctk.CTkFont(size=10), text_color=C["muted"], wraplength=900, justify="left").pack(anchor="w", padx=18, pady=(0, 12))
        return frame

    def button(self, parent, text, command, style="primary", width=None, height=36):
        colors = {
            "primary": (C["accent"], C["accent_hover"], "#062019"),
            "secondary": (C["panel3"], "#29405a", C["text"]),
            "warning": ("#413321", "#5a442a", C["yellow"]),
            "danger": ("#40252e", "#61313d", "#ffb5be"),
            "ghost": ("transparent", "#1b2b3c", C["muted"]),
        }
        fg, hover, text_color = colors.get(style, colors["secondary"])
        kwargs = {
            "text": text, "command": command, "height": height, "corner_radius": 9,
            "fg_color": fg, "hover_color": hover, "text_color": text_color,
            "font": ctk.CTkFont(size=11, weight="bold" if style == "primary" else "normal"),
        }
        if width is not None:
            kwargs["width"] = width
        return ctk.CTkButton(parent, **kwargs)

    def show_page(self, key: str, animate: bool = True):
        titles = {
            "overview": "Обзор системы", "library": "Игровая библиотека", "art": "Обложки и оформление",
            "backup": "Резервные копии", "health": "Диагностика microSD", "transfer": "Wi-Fi / SFTP",
            "settings": "Настройки",
        }
        if key not in titles:
            key = "overview"
        self.current_page = key
        self.page_title.configure(text=titles[key])
        for k, btn in self.page_buttons.items():
            active = k == key
            btn.configure(fg_color=C["accent_dark"] if active else "transparent", text_color=C["accent"] if active else "#c9d5e4")
        # If a user clicks during an animation, snap the current view to its final location.
        self.anim_token += 1
        token = self.anim_token
        if self._previous_page is not None:
            try: self._previous_page.destroy()
            except Exception: pass
            self._previous_page = None
        old = self._active_page
        if old is not None and old.winfo_exists():
            old.place_configure(x=0, y=0)
        new = ctk.CTkFrame(self.page_host, fg_color="transparent", corner_radius=0)
        new.grid_columnconfigure(0, weight=1)
        new.grid_rowconfigure(0, weight=1)
        try:
            getattr(self, f"build_{key}")(new)
        except Exception as exc:
            err = ctk.CTkFrame(new, fg_color=C["panel"], corner_radius=14)
            err.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
            ctk.CTkLabel(err, text="Не удалось отобразить раздел", font=ctk.CTkFont(size=18, weight="bold"), text_color=C["red"]).pack(anchor="w", padx=20, pady=(22, 8))
            ctk.CTkLabel(err, text=str(exc), wraplength=800, text_color=C["muted"]).pack(anchor="w", padx=20, pady=(0, 18))
        self._active_page = new
        if animate and old is not None and old.winfo_exists():
            self._previous_page = old
            new.place(x=27, y=0, relwidth=1, relheight=1)
            def step(x=27):
                if token != self.anim_token or not new.winfo_exists():
                    return
                if x <= 0:
                    new.place_configure(x=0, y=0)
                    if old.winfo_exists():
                        old.destroy()
                    if self._previous_page is old:
                        self._previous_page = None
                    return
                new.place_configure(x=x, y=0)
                self.after(10, lambda: step(max(0, x - 5)))
            step()
        else:
            if old is not None and old.winfo_exists():
                old.destroy()
            self._previous_page = None
            new.place(x=0, y=0, relwidth=1, relheight=1)

    def _initial_scan_if_possible(self):
        root = Path(self.sd_var.get().strip().strip('"')) if self.sd_var.get().strip() else None
        if root and root.is_dir() and locate_roms(root, self.roms_var.get()):
            self.scan_async(silent=True)
        else:
            self._update_device_badge()

    def _sd_root(self) -> Path | None:
        raw = self.sd_var.get().strip().strip('"')
        return Path(raw).expanduser() if raw else None

    def _update_device_badge(self):
        root = self._sd_root()
        if not root or not root.is_dir():
            self.device_label.configure(text="●  Карта не выбрана", text_color=C["yellow"])
            self.version_pill.configure(text="ВЫБЕРИ MICROSD", text_color=C["yellow"], fg_color="#352c1c")
            return
        romroot = locate_roms(root, self.roms_var.get())
        if romroot:
            label = root.name if root.name else str(root)
            self.device_label.configure(text=f"●  {label}\n   Roms обнаружена", text_color=C["accent"])
            self.version_pill.configure(text="СТРУКТУРА Roms НАЙДЕНА", text_color=C["accent"], fg_color="#142a29")
        else:
            self.device_label.configure(text=f"●  {root}\n   Roms не найдена", text_color=C["yellow"])
            self.version_pill.configure(text="ПРОВЕРЬ ПУТЬ", text_color=C["yellow"], fg_color="#352c1c")

    def _save_cfg(self, notify: bool = False):
        self.cfg.update({
            "sd_root": self.sd_var.get().strip(), "roms_rel": self.roms_var.get().strip() or "Roms",
            "backup_root": self.backup_var.get().strip(), "ssh_host": self.host_var.get().strip(),
            "ssh_port": int(self.port_var.get() or 22) if (self.port_var.get() or "22").isdigit() else 22,
            "ssh_user": self.user_var.get().strip() or "root", "ssh_path": self.remote_var.get().strip() or "/mnt/SDCARD",
        })
        write_settings(self.cfg)
        self._update_device_badge()
        if notify:
            self._set_status("Настройки сохранены.")
            mb.showinfo(APP_NAME, "Настройки сохранены.", parent=self)

    def _set_status(self, text: str):
        try: self.status_label.configure(text=text)
        except Exception: pass

    def _run_task(self, label: str, worker: Callable, on_success: Callable, on_error: Callable | None = None):
        if self.busy:
            self._set_status("Подожди завершения текущей операции…")
            return
        self.busy = True
        self.task_token += 1
        token = self.task_token
        self.stop_event.clear()
        self.task_progress.grid()
        self.task_progress.start()
        self._set_status(label)

        def finish_success(result):
            if token != self.task_token: return
            self.busy = False
            self.task_progress.stop(); self.task_progress.grid_remove()
            on_success(result)

        def finish_error(exc):
            if token != self.task_token: return
            self.busy = False
            self.task_progress.stop(); self.task_progress.grid_remove()
            self._set_status("Операция завершилась с ошибкой.")
            if on_error:
                on_error(exc)
            else:
                mb.showerror(APP_NAME, str(exc), parent=self)

        def run():
            try:
                result = worker()
            except Exception as exc:
                self.after(0, lambda e=exc: finish_error(e))
            else:
                self.after(0, lambda r=result: finish_success(r))
        threading.Thread(target=run, daemon=True, name="TrimUI-Worker").start()

    def scan_async(self, silent: bool = False):
        root = self._sd_root()
        if not root or not root.is_dir():
            if not silent: mb.showwarning(APP_NAME, "Сначала выбери корень microSD — папку, внутри которой находится Roms.", parent=self)
            return
        roms_rel = self.roms_var.get().strip() or "Roms"

        def worker():
            games = scan_library(root, roms_rel, stop_event=self.stop_event)
            if self.stop_event.is_set():
                raise InterruptedError("Сканирование отменено")
            # Index cover folders once per system/game directory. Avoids repeatedly
            # enumerating hundreds or thousands of images on slower microSD cards.
            cover_paths = build_asset_index(games)
            if self.stop_event.is_set():
                raise InterruptedError("Сканирование отменено")
            count = sum(1 for game in games if cover_paths.get((str(game["path"]), "screenshot")))
            return games, cover_paths, count

        def success(result):
            self.games, self.art_cache, screenshot_count = result
            self.platform_counts = {}
            for g in self.games:
                self.platform_counts.setdefault(g["platform"], []).append(g)
            if not self.selected_art_platform or self.selected_art_platform not in self.platform_counts:
                self.selected_art_platform = next(iter(sorted(self.platform_counts, key=str.casefold)), "")
            self.last_scan_time = datetime.now().strftime("%d.%m.%Y %H:%M")
            self.last_scan_error = ""
            self._set_status(f"Сканирование завершено: {len(self.games):,} игр · {len(self.platform_counts)} платформ · обложек {screenshot_count}/{len(self.games)}".replace(",", " "))
            self._update_device_badge()
            if self.current_page in {"overview", "library", "art", "health"}:
                self.show_page(self.current_page, animate=False)
            if not silent:
                self._set_status(f"Готово: найдено {len(self.games)} игр в {len(self.platform_counts)} системах.")

        def error(exc):
            self.last_scan_error = str(exc)
            if not silent:
                mb.showerror(APP_NAME, f"Не удалось просканировать карту.\n\n{exc}", parent=self)
            self._update_device_badge()
        self._run_task("Сканирую Roms, системы и обложки в фоне…", worker, success, error)

    def _focus_search(self, _event=None):
        if self.current_page == "library" and hasattr(self, "search_entry"):
            self.search_entry.focus_set()
            return "break"
        if self.current_page == "art" and hasattr(self, "art_search_entry"):
            self.art_search_entry.focus_set()
            return "break"
        return None

    def _cache_art_path(self, game: dict, kind: str = "screenshot") -> Path | None:
        key = (str(game["path"]), kind)
        if key in self.art_cache:
            return self.art_cache[key]
        path = asset_path_for_game(game, kind)
        self.art_cache[key] = path
        return path

    # ───────────────────────────── Overview ─────────────────────────────
    def build_overview(self, parent):
        root = self._sd_root()
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        scroll.grid(row=0, column=0, sticky="nsew")
        hero = ctk.CTkFrame(scroll, fg_color="#122a3d", corner_radius=18, border_width=1, border_color="#1c3b54")
        hero.pack(fill="x", pady=(0, 15))
        hero.grid_columnconfigure(0, weight=1)
        left = ctk.CTkFrame(hero, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=24, pady=22)
        ctk.CTkLabel(left, text="Твоя библиотека.\nПод контролем.", font=ctk.CTkFont(size=29, weight="bold"), text_color=C["text"], justify="left").pack(anchor="w")
        ctk.CTkLabel(left, text="Удобный менеджер microSD для NX REDUX: игры, ресурсы оформления, резервные копии и безопасная диагностика.", font=ctk.CTkFont(size=11), text_color="#b5c7d9", wraplength=520, justify="left").pack(anchor="w", pady=(9, 17))
        actions = ctk.CTkFrame(left, fg_color="transparent")
        actions.pack(anchor="w")
        self.button(actions, "Выбрать microSD", self.choose_sd, "primary", height=38).pack(side="left", padx=(0, 8))
        self.button(actions, "⟳  Сканировать", self.scan_async, "secondary", height=38).pack(side="left")
        right = ctk.CTkFrame(hero, width=165, height=150, fg_color="#16364a", corner_radius=18, border_width=1, border_color="#245b69")
        right.grid(row=0, column=1, padx=24, pady=20)
        right.grid_propagate(False)
        ctk.CTkLabel(right, text="◆", font=ctk.CTkFont(size=76, weight="bold"), text_color=C["accent"]).pack(expand=True)
        ctk.CTkLabel(right, text="NX REDUX", font=ctk.CTkFont(size=10, weight="bold"), text_color="#a5dfd3").pack(pady=(0, 13))

        game_count = len(self.games)
        platforms = len(self.platform_counts)
        screenshot_count = sum(1 for g in self.games if self._cache_art_path(g, "screenshot"))
        cover_pct = round(100 * screenshot_count / game_count) if game_count else 0
        try:
            disk = shutil.disk_usage(root) if root and root.exists() else None
            free_txt = fmt_size(disk.free) if disk else "—"
        except Exception:
            free_txt = "—"
        metrics = ctk.CTkFrame(scroll, fg_color="transparent")
        metrics.pack(fill="x", pady=(0, 15))
        for i in range(4): metrics.grid_columnconfigure(i, weight=1, uniform="metric")
        items = [
            ("Игры", f"{game_count:,}".replace(",", " "), "Найдено на microSD", C["accent"], lambda: self.show_page("library")),
            ("Платформы", str(platforms), "Нажми, чтобы открыть обзор", C["blue"], self.show_platform_summary),
            ("Обложки", f"{cover_pct}%", f"{screenshot_count} из {game_count} игр", C["accent"], lambda: self.show_page("art")),
            ("Свободно", free_txt, "На карте памяти", C["yellow"], lambda: self.show_page("health")),
        ]
        for i, (title, value, caption, color, cmd) in enumerate(items):
            card, _ = self.card(metrics, title, value, caption, color, cmd)
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))

        section = self.section(scroll, "Быстрый доступ", "Частые задачи — без долгих поисков по меню.")
        section.pack(fill="x", pady=(0, 14))
        grid = ctk.CTkFrame(section, fg_color="transparent")
        grid.pack(fill="x", padx=14, pady=(0, 15))
        choices = [
            ("▦", "Библиотека", "Фильтруй системы и запускай редактор обложки двойным щелчком.", lambda: self.show_page("library")),
            ("▧", "Оформление", "Скриншоты, логотипы систем и фон меню в путях NX REDUX.", lambda: self.show_page("art")),
            ("◉", "Backup", "Скопируй сохранения и пользовательские данные на ПК.", lambda: self.show_page("backup")),
            ("✣", "Проверка карты", "Проверь структуру Roms, теги систем и покрытие обложками.", lambda: self.show_page("health")),
        ]
        for col in range(4): grid.grid_columnconfigure(col, weight=1, uniform="quick")
        for i, (ico, title, desc, cmd) in enumerate(choices):
            tile = ctk.CTkFrame(grid, fg_color=C["panel2"], corner_radius=12, border_width=1, border_color=C["line"])
            tile.grid(row=0, column=i, sticky="nsew", padx=4)
            ctk.CTkLabel(tile, text=ico, font=ctk.CTkFont(size=22, weight="bold"), text_color=C["accent"]).pack(anchor="w", padx=13, pady=(13, 5))
            ctk.CTkLabel(tile, text=title, font=ctk.CTkFont(size=12, weight="bold"), text_color=C["text"]).pack(anchor="w", padx=13)
            ctk.CTkLabel(tile, text=desc, font=ctk.CTkFont(size=9), text_color=C["muted"], wraplength=185, justify="left").pack(anchor="w", padx=13, pady=(4, 10))
            self.button(tile, "Открыть  →", cmd, "ghost", height=29).pack(fill="x", padx=9, pady=(0, 9))
        ctk.CTkLabel(scroll, text=f"Последняя проверка библиотеки: {self.last_scan_time}    ·    F5 — повторное сканирование    ·    Ctrl+F — поиск", font=ctk.CTkFont(size=10), text_color=C["dim"], anchor="w").pack(fill="x", pady=(0, 7))

    def show_platform_summary(self):
        win = ctk.CTkToplevel(self)
        win.title("Платформы библиотеки")
        win.geometry("560x620")
        win.minsize(420, 380)
        win.configure(fg_color=C["bg"])
        win.transient(self)
        head = ctk.CTkFrame(win, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(20, 10))
        ctk.CTkLabel(head, text="ТВОИ ПЛАТФОРМЫ", font=ctk.CTkFont(size=20, weight="bold"), text_color=C["text"]).pack(anchor="w")
        ctk.CTkLabel(head, text=f"{len(self.platform_counts)} систем · {len(self.games)} игр", font=ctk.CTkFont(size=11), text_color=C["muted"]).pack(anchor="w", pady=(4, 0))
        box = ctk.CTkScrollableFrame(win, fg_color=C["panel"], corner_radius=13)
        box.pack(fill="both", expand=True, padx=17, pady=(0, 17))
        for platform, games in sorted(self.platform_counts.items(), key=lambda x: x[0].casefold()):
            art_count = sum(1 for g in games if self._cache_art_path(g, "screenshot"))
            row = ctk.CTkFrame(box, fg_color=C["panel2"], corner_radius=9)
            row.pack(fill="x", pady=3, padx=3)
            ctk.CTkLabel(row, text="▦", text_color=C["accent"], font=ctk.CTkFont(size=20)).pack(side="left", padx=12, pady=11)
            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True, pady=8)
            ctk.CTkLabel(info, text=platform, text_color=C["text"], font=ctk.CTkFont(size=11, weight="bold"), anchor="w").pack(anchor="w")
            ctk.CTkLabel(info, text=f"{len(games)} игр · обложки {art_count}/{len(games)}", text_color=C["muted"], font=ctk.CTkFont(size=9), anchor="w").pack(anchor="w", pady=(2, 0))
            self.button(row, "Открыть", lambda p=platform: (win.destroy(), self.show_library_platform(p)), "secondary", width=75, height=29).pack(side="right", padx=10)

    def show_library_platform(self, platform):
        self.expanded_platforms.add(platform)
        self.visible_game_rows[platform] = 40
        self.library_platform_filter = platform
        self.show_page("library")

    # ───────────────────────────── Library ─────────────────────────────
    def build_library(self, parent):
        panel = ctk.CTkFrame(parent, fg_color=C["panel"], corner_radius=15, border_width=1, border_color=C["line"])
        panel.grid(row=0, column=0, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)
        bar = ctk.CTkFrame(panel, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 9))
        bar.grid_columnconfigure(0, weight=1)
        self.library_search = ctk.StringVar(value=getattr(self, "_library_search", ""))
        self.library_platform_filter = getattr(self, "library_platform_filter", "Все платформы")
        self.library_art_filter = getattr(self, "library_art_filter", "Все игры")
        self.search_entry = ctk.CTkEntry(bar, textvariable=self.library_search, height=38, placeholder_text="⌕  Поиск по названию, платформе, пути…")
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        values = ["Все платформы"] + sorted(self.platform_counts, key=str.casefold)
        if self.library_platform_filter not in values: self.library_platform_filter = "Все платформы"
        self.platform_menu = ctk.CTkOptionMenu(bar, values=values, variable=ctk.StringVar(value=self.library_platform_filter), width=185, height=38, fg_color=C["panel3"], button_color="#29425c", button_hover_color="#345673", command=self._library_platform_changed)
        self.platform_menu.grid(row=0, column=1, padx=(0, 7))
        self.art_filter_menu = ctk.CTkOptionMenu(bar, values=["Все игры", "Без обложек"], width=115, height=38, fg_color=C["panel3"], button_color="#29425c", button_hover_color="#345673", command=self._library_art_filter_changed)
        self.art_filter_menu.set(self.library_art_filter)
        self.art_filter_menu.grid(row=0, column=2, padx=(0, 7))
        self.button(bar, "Сканировать", self.scan_async, "primary", width=96, height=38).grid(row=0, column=3, padx=(0, 7))
        self.button(bar, "Дубликаты", self.find_duplicates_async, "warning", width=88, height=38).grid(row=0, column=4)
        self.library_search.trace_add("write", self._library_search_changed)
        self.lib_summary = ctk.CTkLabel(panel, text="Сканируй карту, чтобы загрузить игры.", text_color=C["muted"], anchor="w", font=ctk.CTkFont(size=10))
        self.lib_summary.grid(row=1, column=0, sticky="ew", padx=17, pady=(0, 8))
        self.library_scroll = ctk.CTkScrollableFrame(panel, fg_color="#0b1420", corner_radius=10)
        self.library_scroll.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        foot = ctk.CTkFrame(panel, fg_color="transparent")
        foot.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 13))
        self.button(foot, "Открыть папку ROM", self.open_selected_game_folder, "secondary", height=31).pack(side="left")
        self.button(foot, "Экспорт CSV", self.export_csv, "secondary", height=31).pack(side="left", padx=7)
        ctk.CTkLabel(foot, text="Двойной щелчок по игре — обложка и ресурсы", font=ctk.CTkFont(size=9), text_color=C["dim"]).pack(side="right")
        self.render_library_groups()

    def _library_search_changed(self, *_):
        self._library_search = self.library_search.get()
        self.render_library_groups()

    def _library_platform_changed(self, value):
        self.library_platform_filter = value
        self.render_library_groups()

    def _library_art_filter_changed(self, value):
        self.library_art_filter = value
        self.render_library_groups()

    def render_library_groups(self):
        if not hasattr(self, "library_scroll") or not self.library_scroll.winfo_exists(): return
        for widget in self.library_scroll.winfo_children(): widget.destroy()
        q = self.library_search.get().strip().casefold() if hasattr(self, "library_search") else ""
        chosen_platform = getattr(self, "library_platform_filter", "Все платформы")
        missing_only = getattr(self, "library_art_filter", "Все игры") == "Без обложек"
        filtered: dict[str, list[dict]] = {}
        for game in self.games:
            if chosen_platform != "Все платформы" and game["platform"] != chosen_platform: continue
            if missing_only and self._cache_art_path(game, "screenshot"): continue
            if q and q not in f"{game['name']} {game['platform']} {game['relative']}".casefold(): continue
            filtered.setdefault(game["platform"], []).append(game)
        shown = sum(len(v) for v in filtered.values())
        size = sum(g["size"] for g in self.games)
        self.lib_summary.configure(text=f"Найдено {len(self.games)} игр · показано {shown} · {len(filtered)} платформ · {fmt_size(size)}")
        if not filtered:
            self._empty_state(self.library_scroll, "Ничего не найдено", "Измени фильтр, попробуй другое название или выбери microSD.")
            return
        max_per_platform = 32
        render_budget = 120
        budget_notice_shown = False
        for platform, games in sorted(filtered.items(), key=lambda x: x[0].casefold()):
            total_size = sum(g["size"] for g in games)
            present = sum(1 for g in games if self._cache_art_path(g, "screenshot"))
            opened = platform in self.expanded_platforms or bool(q) or chosen_platform != "Все платформы"
            head = ctk.CTkFrame(self.library_scroll, fg_color="#15263a", corner_radius=10, border_width=1, border_color="#243b52")
            head.pack(fill="x", padx=3, pady=(5, 3))
            ctk.CTkLabel(head, text="▾" if opened else "▸", width=23, text_color=C["accent"], font=ctk.CTkFont(size=14, weight="bold")).pack(side="left", padx=(10, 0), pady=10)
            title = ctk.CTkLabel(head, text=platform, anchor="w", text_color=C["text"], font=ctk.CTkFont(size=12, weight="bold"))
            title.pack(side="left", fill="x", expand=True, padx=7, pady=10)
            ctk.CTkLabel(head, text=f"{len(games)} игр  ·  {fmt_size(total_size)}  ·  обложки {present}/{len(games)}", text_color=C["muted"], font=ctk.CTkFont(size=9)).pack(side="right", padx=12)
            for w in (head, title):
                w.bind("<Button-1>", lambda _e, p=platform: self.toggle_library_platform(p), add="+")
                try: w.configure(cursor="hand2")
                except Exception: pass
            if not opened: continue
            limit = self.visible_game_rows.get(platform, max_per_platform)
            if q or chosen_platform != "Все платформы":
                limit = max(limit, max_per_platform)
            if render_budget <= 0:
                if not budget_notice_shown:
                    ctk.CTkLabel(self.library_scroll, text="Показаны первые 120 совпадений, чтобы список оставался быстрым. Уточни поиск для остальных результатов.", text_color=C["yellow"], font=ctk.CTkFont(size=9), wraplength=700, justify="left").pack(anchor="w", padx=15, pady=10)
                    budget_notice_shown = True
                continue
            limit = min(limit, render_budget)
            rows = games[:limit]
            render_budget -= len(rows)
            for game in rows:
                self._render_game_row(self.library_scroll, game)
            if len(games) > len(rows):
                more = len(games) - len(rows)
                self.button(self.library_scroll, f"Показать ещё {min(40, more)} из {more} игр в {platform}", lambda p=platform: self.show_more_library(p), "ghost", height=29).pack(anchor="w", padx=(42, 8), pady=(3, 6))

    def _render_game_row(self, parent, game: dict):
        row = ctk.CTkFrame(parent, fg_color="#101d2b", corner_radius=8, border_width=1, border_color="#1a2b3e")
        row.pack(fill="x", padx=(21, 4), pady=2)
        art = self._cache_art_path(game, "screenshot")
        if art:
            thumb = self._thumbnail(art, (31, 42), key=f"lib:{game['path']}")
            if thumb:
                ctk.CTkLabel(row, text="", image=thumb, width=35, height=45).pack(side="left", padx=(7, 8), pady=4)
            else:
                ctk.CTkLabel(row, text="▧", width=35, text_color=C["accent"], font=ctk.CTkFont(size=20)).pack(side="left", padx=(7, 8))
        else:
            ctk.CTkLabel(row, text="＋", width=35, text_color="#3e5871", font=ctk.CTkFont(size=19, weight="bold")).pack(side="left", padx=(7, 8))
        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True, pady=5)
        name = ctk.CTkLabel(info, text=game["name"], text_color=C["text"], anchor="w", font=ctk.CTkFont(size=10, weight="bold"))
        name.pack(anchor="w", fill="x")
        ctk.CTkLabel(info, text=game["relative"], text_color=C["dim"], anchor="w", font=ctk.CTkFont(size=8)).pack(anchor="w", fill="x", pady=(2, 0))
        ctk.CTkLabel(row, text=fmt_size(game["size"]), width=75, text_color=C["muted"], font=ctk.CTkFont(size=9)).pack(side="right", padx=10)
        for widget in (row, info, name):
            widget.bind("<Double-Button-1>", lambda _e, g=game: self.open_game_editor(g), add="+")
            widget.bind("<Button-1>", lambda _e, g=game: self._select_game(g), add="+")
            try: widget.configure(cursor="hand2")
            except Exception: pass

    def _select_game(self, game):
        self.selected_game = game

    def toggle_library_platform(self, platform):
        if platform in self.expanded_platforms:
            self.expanded_platforms.remove(platform)
        else:
            self.expanded_platforms.add(platform)
        self.render_library_groups()

    def show_more_library(self, platform):
        self.visible_game_rows[platform] = self.visible_game_rows.get(platform, 32) + 40
        self.render_library_groups()

    def _thumbnail(self, path: Path, size: tuple[int, int], key: str):
        if not path or not Path(path).is_file(): return None
        cached = self._photos.get(key)
        if cached: return cached
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im).convert("RGBA")
                im.thumbnail(size, Image.Resampling.LANCZOS)
                # CTkImage owns a copy, so the source file handle is already closed.
                photo = ctk.CTkImage(light_image=im, dark_image=im, size=(im.width, im.height))
            self._photos[key] = photo
            return photo
        except Exception:
            return None

    def open_selected_game_folder(self):
        if not self.selected_game:
            mb.showinfo(APP_NAME, "Сначала выбери игру в списке.", parent=self)
            return
        try: reveal_path(Path(self.selected_game["path"]).parent)
        except Exception as exc: mb.showerror(APP_NAME, str(exc), parent=self)

    def export_csv(self):
        if not self.games:
            mb.showinfo(APP_NAME, "Сначала просканируй библиотеку.", parent=self)
            return
        target = fd.asksaveasfilename(parent=self, title="Экспорт библиотеки NX REDUX", defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile="nx_redux_library.csv")
        if not target: return
        try:
            with open(target, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle, delimiter=";")
                writer.writerow(["Платформа", "Тег", "Название", "Размер, байт", "Относительный путь", "Файл обложки"])
                for game in self.games:
                    art = self._cache_art_path(game, "screenshot")
                    writer.writerow([game["platform"], game.get("tag") or "", game["name"], game["size"], game["relative"], str(art) if art else ""])
            self._set_status(f"CSV экспортирован: {target}")
            mb.showinfo(APP_NAME, f"Экспорт завершён.\n\n{target}", parent=self)
        except Exception as exc:
            mb.showerror(APP_NAME, f"Не удалось экспортировать CSV:\n{exc}", parent=self)

    def find_duplicates_async(self):
        if not self.games:
            mb.showinfo(APP_NAME, "Сначала просканируй библиотеку. После сканирования нажми «Дубликаты» ещё раз.", parent=self)
            self.scan_async(silent=True)
            return
        if not mb.askyesno(APP_NAME, "Сравнить файлы одинакового размера по SHA-256?\n\nНа большой карте проверка может занять время. Ничего не удаляется.", parent=self):
            return
        def worker():
            return find_duplicates(self.games, stop_event=self.stop_event)
        def success(groups):
            if not groups:
                self._set_status("Дубликатов не найдено.")
                mb.showinfo(APP_NAME, "Совпадающих файлов не найдено.", parent=self)
                return
            lines = []
            for number, group in enumerate(groups, 1):
                lines.append(f"Группа {number}:")
                lines.extend(f"  • {g['platform']} / {g['name']} — {g['relative']}" for g in group)
                lines.append("")
            self._set_status(f"Найдено групп дубликатов: {len(groups)}. Ничего не удалено.")
            self._show_text_dialog("Дубликаты ROM-файлов", "\n".join(lines), 800, 620)
        self._run_task("Сравниваю файлы по SHA-256…", worker, success)

    # ───────────────────────────── Artwork ─────────────────────────────
    def build_art(self, parent):
        parent.grid_columnconfigure(0, weight=0, minsize=245)
        parent.grid_columnconfigure(1, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        left = ctk.CTkFrame(parent, fg_color=C["panel"], corner_radius=14, border_width=1, border_color=C["line"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        ctk.CTkLabel(left, text="СИСТЕМЫ", font=ctk.CTkFont(size=10, weight="bold"), text_color=C["dim"]).pack(anchor="w", padx=15, pady=(16, 8))
        platform_scroll = ctk.CTkScrollableFrame(left, fg_color="transparent", corner_radius=0)
        platform_scroll.pack(fill="both", expand=True, padx=7, pady=(0, 8))
        for platform, games in sorted(self.platform_counts.items(), key=lambda x: x[0].casefold()):
            present = sum(1 for g in games if self._cache_art_path(g, "screenshot"))
            ratio = present / len(games) if games else 0
            row = ctk.CTkFrame(platform_scroll, fg_color=C["accent_dark"] if platform == self.selected_art_platform else C["panel2"], corner_radius=9, border_width=1, border_color="#2d6458" if platform == self.selected_art_platform else C["line"])
            row.pack(fill="x", pady=3, padx=2)
            ctk.CTkLabel(row, text="▦", width=22, text_color=C["accent"], font=ctk.CTkFont(size=16)).pack(side="left", padx=(8, 3), pady=9)
            details = ctk.CTkFrame(row, fg_color="transparent")
            details.pack(side="left", fill="x", expand=True, padx=(3, 8), pady=7)
            ctk.CTkLabel(details, text=platform, text_color=C["text"], anchor="w", font=ctk.CTkFont(size=9, weight="bold")).pack(anchor="w", fill="x")
            ctk.CTkLabel(details, text=f"{present}/{len(games)} обложек", text_color=C["muted"], anchor="w", font=ctk.CTkFont(size=8)).pack(anchor="w", pady=(2, 4))
            bar = ctk.CTkProgressBar(details, height=4, progress_color=C["accent"], fg_color="#25364a")
            bar.pack(fill="x")
            bar.set(ratio)
            for widget in (row, details):
                widget.bind("<Button-1>", lambda _e, p=platform: self.select_art_platform(p), add="+")
                try: widget.configure(cursor="hand2")
                except Exception: pass
        self.button(left, "Обновить библиотеку", self.scan_async, "secondary", height=34).pack(fill="x", padx=11, pady=(0, 12))

        right = ctk.CTkFrame(parent, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(3, weight=1)
        if not self.selected_art_platform or self.selected_art_platform not in self.platform_counts:
            self._empty_state(right, "Сначала просканируй microSD", "Выбери карту с Roms и запусти сканирование.")
            return
        platform = self.selected_art_platform
        games = self.platform_counts.get(platform, [])
        present = sum(1 for g in games if self._cache_art_path(g, "screenshot"))
        top = ctk.CTkFrame(right, fg_color=C["panel"], corner_radius=14, border_width=1, border_color=C["line"])
        top.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text=platform, font=ctk.CTkFont(size=20, weight="bold"), text_color=C["text"]).grid(row=0, column=0, sticky="w", padx=17, pady=(14, 2))
        ctk.CTkLabel(top, text=f"{len(games)} игр  ·  обложки {present}/{len(games)}  ·  путь Roms/{platform}/.media/", font=ctk.CTkFont(size=9), text_color=C["muted"]).grid(row=1, column=0, sticky="w", padx=17, pady=(0, 12))
        self.button(top, "Логотип системы", lambda: self.choose_system_asset(platform, "logo"), "secondary", width=135, height=31).grid(row=0, column=1, padx=(0, 7), pady=(13, 3))
        self.button(top, "Фон системы", lambda: self.choose_system_asset(platform, "background"), "secondary", width=120, height=31).grid(row=0, column=2, padx=(0, 14), pady=(13, 3))

        global_card = ctk.CTkFrame(right, fg_color="#122739", corner_radius=12, border_width=1, border_color="#24455b")
        global_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        global_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(global_card, text="ОБЩИЙ ФОН МЕНЮ NX REDUX", font=ctk.CTkFont(size=9, weight="bold"), text_color=C["accent"]).grid(row=0, column=0, sticky="w", padx=13, pady=(10, 1))
        ctk.CTkLabel(global_card, text="Сохраняется как bg.png в корне microSD — это не папка Themes.", font=ctk.CTkFont(size=9), text_color=C["muted"]).grid(row=1, column=0, sticky="w", padx=13, pady=(0, 10))
        self.button(global_card, "Выбрать фон…", lambda: self.choose_system_asset(platform, "wallpaper"), "primary", width=120, height=31).grid(row=0, column=1, rowspan=2, padx=12, pady=10)

        # Artwork kinds help people understand NX REDUX's real asset layout.
        assets = ctk.CTkFrame(right, fg_color=C["panel"], corner_radius=14, border_width=1, border_color=C["line"])
        assets.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        assets.grid_columnconfigure(0, weight=1, uniform="artassets")
        assets.grid_columnconfigure(1, weight=1, uniform="artassets")
        ctk.CTkLabel(assets, text="РЕСУРСЫ ИГР", font=ctk.CTkFont(size=10, weight="bold"), text_color=C["dim"]).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 5))
        kind_desc = {
            "screenshot": ("Screenshot", ".media/screenshot/Игра.png", "Скриншот для списков и фона"),
            "boxart": ("3D Box art", ".media/boxart/Игра.png", "Обложка коробки"),
            "boxart2d": ("2D Box art", ".media/boxart2d/Игра.png", "Плоская обложка"),
            "wheel": ("Wheel", ".media/wheel/Игра.png", "Логотип игры"),
            "mix": ("Mix", ".media/mix/Игра.png", "Скомпонованное изображение"),
        }
        for i, kind in enumerate(ART_DIRS):
            folder = Path(self._sd_root() or ".") / (self.roms_var.get().strip() or "Roms") / platform / ".media" / ART_DIRS[kind]
            count = image_count(folder)
            title, path, desc = kind_desc[kind]
            tile = ctk.CTkFrame(assets, fg_color=C["panel2"], corner_radius=9)
            tile.grid(row=1 + i // 2, column=i % 2, sticky="ew", padx=6, pady=5)
            ctk.CTkLabel(tile, text=title, text_color=C["text"], font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=11, pady=(9, 2))
            ctk.CTkLabel(tile, text=f"{count} изображений", text_color=C["accent"] if count else C["dim"], font=ctk.CTkFont(size=9)).pack(anchor="w", padx=11)
            ctk.CTkLabel(tile, text=path, text_color=C["muted"], font=ctk.CTkFont(size=8)).pack(anchor="w", padx=11, pady=(3, 1))
            ctk.CTkLabel(tile, text=desc, text_color=C["dim"], font=ctk.CTkFont(size=8)).pack(anchor="w", padx=11, pady=(0, 8))
            self.button(tile, "Открыть папку", lambda p=folder: self.open_folder(p), "ghost", height=25).pack(fill="x", padx=8, pady=(0, 8))

        games_card = ctk.CTkFrame(right, fg_color=C["panel"], corner_radius=14, border_width=1, border_color=C["line"])
        games_card.grid(row=3, column=0, sticky="nsew")
        games_card.grid_columnconfigure(0, weight=1)
        games_card.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(games_card, text="Обложки по играм", font=ctk.CTkFont(size=14, weight="bold"), text_color=C["text"]).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 7))
        bar = ctk.CTkFrame(games_card, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        self.art_search = ctk.StringVar(value=getattr(self, "_art_search", ""))
        self.art_search_entry = ctk.CTkEntry(bar, textvariable=self.art_search, placeholder_text="Найти игру в этой системе…", height=33)
        self.art_search_entry.pack(side="left", fill="x", expand=True)
        self.art_search.trace_add("write", self._art_search_changed)
        self.button(bar, "Открыть редактор", self.open_selected_game_editor, "primary", height=33).pack(side="left", padx=(7, 0))
        self.art_games_scroll = ctk.CTkScrollableFrame(games_card, fg_color="#0b1420", corner_radius=9)
        self.art_games_scroll.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.render_art_games()

    def select_art_platform(self, platform):
        self.selected_art_platform = platform
        self.show_page("art", animate=False)

    def _art_search_changed(self, *_):
        self._art_search = self.art_search.get()
        self.render_art_games()

    def render_art_games(self):
        if not hasattr(self, "art_games_scroll") or not self.art_games_scroll.winfo_exists(): return
        for w in self.art_games_scroll.winfo_children(): w.destroy()
        platform = self.selected_art_platform
        games = self.platform_counts.get(platform, [])
        q = self.art_search.get().strip().casefold() if hasattr(self, "art_search") else ""
        if q:
            games = [g for g in games if q in f"{g['name']} {g['relative']}".casefold()]
        for game in games[:36]:
            row = ctk.CTkFrame(self.art_games_scroll, fg_color=C["panel2"], corner_radius=8)
            row.pack(fill="x", padx=3, pady=2)
            art = self._cache_art_path(game, "screenshot")
            thumb = self._thumbnail(art, (26, 34), key=f"artlist:{game['path']}") if art else None
            if thumb:
                ctk.CTkLabel(row, text="", image=thumb, width=30, height=37).pack(side="left", padx=(7, 5), pady=3)
            else:
                ctk.CTkLabel(row, text="＋", width=30, text_color=C["dim"], font=ctk.CTkFont(size=15)).pack(side="left", padx=(7, 5))
            label = ctk.CTkLabel(row, text=game["name"], anchor="w", text_color=C["text"] if art else C["muted"], font=ctk.CTkFont(size=9, weight="bold"))
            label.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(row, text="ЕСТЬ" if art else "NO IMAGE", text_color=C["accent"] if art else C["yellow"], font=ctk.CTkFont(size=8, weight="bold")).pack(side="right", padx=9)
            for w in (row, label):
                w.bind("<Button-1>", lambda _e, g=game: self._select_game(g), add="+")
                w.bind("<Double-Button-1>", lambda _e, g=game: self.open_game_editor(g), add="+")
                try: w.configure(cursor="hand2")
                except Exception: pass
        if len(games) > 36:
            ctk.CTkLabel(self.art_games_scroll, text=f"Показаны первые 36 из {len(games)} игр. Уточни поиск, чтобы быстрее найти нужную.", text_color=C["dim"], font=ctk.CTkFont(size=9), wraplength=500).pack(anchor="w", padx=10, pady=8)
        if not games:
            self._empty_state(self.art_games_scroll, "Нет подходящих игр", "Измени поиск или выбери другую платформу.")

    def open_selected_game_editor(self):
        if not self.selected_game or self.selected_game["platform"] != self.selected_art_platform:
            mb.showinfo(APP_NAME, "Дважды щёлкни по игре в списке или сначала выбери её.", parent=self)
            return
        self.open_game_editor(self.selected_game)

    def open_game_editor(self, game: dict):
        self.selected_game = game
        win = ctk.CTkToplevel(self)
        win.title("Редактор ресурсов игры — NX REDUX")
        win.geometry("780x665")
        win.minsize(680, 560)
        win.configure(fg_color=C["bg"])
        win.transient(self)
        win.grab_set()
        win.grid_columnconfigure(1, weight=1)
        win.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(win, fg_color=C["panel"], corner_radius=13, border_width=1, border_color=C["line"])
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(16, 12))
        ctk.CTkLabel(header, text=game["name"], font=ctk.CTkFont(size=17, weight="bold"), text_color=C["text"], wraplength=660).pack(anchor="w", padx=16, pady=(12, 3))
        ctk.CTkLabel(header, text=f"{game['platform']}  ·  {game['relative']}", font=ctk.CTkFont(size=9), text_color=C["muted"]).pack(anchor="w", padx=16, pady=(0, 12))
        preview_panel = ctk.CTkFrame(win, width=280, fg_color=C["panel"], corner_radius=13, border_width=1, border_color=C["line"])
        preview_panel.grid(row=1, column=0, sticky="nsew", padx=(16, 8), pady=(0, 16))
        preview_panel.grid_propagate(False)
        details_panel = ctk.CTkFrame(win, fg_color=C["panel"], corner_radius=13, border_width=1, border_color=C["line"])
        details_panel.grid(row=1, column=1, sticky="nsew", padx=(8, 16), pady=(0, 16))
        details_panel.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(preview_panel, text="ПРЕДПРОСМОТР", font=ctk.CTkFont(size=9, weight="bold"), text_color=C["dim"]).pack(anchor="w", padx=14, pady=(14, 8))
        preview_box = ctk.CTkFrame(preview_panel, fg_color="#0a121c", corner_radius=11, border_width=1, border_color=C["line"])
        preview_box.pack(fill="both", expand=True, padx=12, pady=(0, 11))
        preview_label = ctk.CTkLabel(preview_box, text="NO IMAGE", font=ctk.CTkFont(size=19, weight="bold"), text_color="#536981")
        preview_label.pack(expand=True)
        path_label = ctk.CTkLabel(preview_panel, text="", font=ctk.CTkFont(size=8), text_color=C["muted"], wraplength=240, justify="left")
        path_label.pack(fill="x", padx=12, pady=(0, 12))

        ctk.CTkLabel(details_panel, text="Тип изображения", font=ctk.CTkFont(size=12, weight="bold"), text_color=C["text"]).pack(anchor="w", padx=16, pady=(16, 7))
        display_to_kind = {label: key for key, label in ASSET_LABELS.items()}
        kind_var = ctk.StringVar(value=ASSET_LABELS["screenshot"])
        kind_menu = ctk.CTkOptionMenu(details_panel, values=list(ASSET_LABELS.values()), variable=kind_var, command=lambda _v: update_preview(), height=35, fg_color=C["panel3"], button_color="#29425c", button_hover_color="#345673")
        kind_menu.pack(fill="x", padx=16)
        current_kind = lambda: display_to_kind.get(kind_var.get(), "screenshot")
        kind_hint = ctk.CTkLabel(details_panel, text="NX REDUX: Roms/Система/.media/screenshot/Игра.png", font=ctk.CTkFont(size=9), text_color=C["muted"], wraplength=390, justify="left")
        kind_hint.pack(anchor="w", padx=16, pady=(8, 17))
        ctk.CTkLabel(details_panel, text="Операции", font=ctk.CTkFont(size=12, weight="bold"), text_color=C["text"]).pack(anchor="w", padx=16, pady=(0, 8))
        self.button(details_panel, "Выбрать изображение…", lambda: self.choose_game_image(game, current_kind(), win, update_preview), "primary", height=40).pack(fill="x", padx=16, pady=(0, 8))
        self.button(details_panel, "Открыть папку ресурса", lambda: self.open_folder(asset_destination(game, current_kind()).parent), "secondary", height=35).pack(fill="x", padx=16, pady=(0, 8))
        self.button(details_panel, "Открыть папку ROM", lambda: self.open_folder(Path(game["path"]).parent), "secondary", height=35).pack(fill="x", padx=16)
        if self.sftp:
            self.button(details_panel, "Передать обложку на консоль по SFTP", lambda: self.upload_game_art_sftp(game, current_kind(), win), "warning", height=38).pack(fill="x", padx=16, pady=(15, 0))
        ctk.CTkLabel(details_panel, text="Изображение будет сохранено в PNG с именем игры. Исходный ROM не изменяется.", font=ctk.CTkFont(size=9), text_color=C["dim"], wraplength=390, justify="left").pack(anchor="w", padx=16, pady=(16, 14))

        def update_preview(*_):
            kind = current_kind()
            asset = self._cache_art_path(game, kind)
            if not asset:
                asset = asset_path_for_game(game, kind)
            if asset:
                thumb = self._thumbnail(asset, (236, 355), key=f"editor:{game['path']}:{kind}:{asset.stat().st_mtime_ns}")
                if thumb:
                    preview_label.configure(image=thumb, text="")
                    preview_label.image = thumb
                else:
                    preview_label.configure(image=None, text="NO IMAGE")
                path_label.configure(text=str(asset))
            else:
                preview_label.configure(image=None, text="NO IMAGE")
                path_label.configure(text="Для этого типа изображения файл пока не найден.")
            dest = asset_destination(game, kind)
            label = f"NX REDUX: {dest.relative_to(self._sd_root()) if self._sd_root() and self._sd_root() in dest.parents else dest}"
            kind_hint.configure(text=label)
        update_preview()
        win.update_idletasks()

    def choose_game_image(self, game: dict, kind: str, dialog, refresh_callback: Callable | None = None):
        source = fd.askopenfilename(parent=dialog, title="Выбери изображение", filetypes=[("Изображения", "*.png *.jpg *.jpeg *.webp *.bmp"), ("Все файлы", "*.*")])
        if not source: return
        dest = asset_destination(game, kind)
        siblings = [p for p in dest.parent.glob(dest.stem + ".*") if p.suffix.casefold() in IMAGE_EXTS] if dest.parent.exists() else []
        if siblings and not mb.askyesno(APP_NAME, f"Для этой игры уже есть изображение:\n{siblings[0]}\n\nЗаменить ресурс на новый PNG?", parent=dialog): return

        def worker():
            save_as_png(source, dest)
            # Remove only old files of the same artwork type/name after the new PNG is safely written.
            for old in siblings:
                if old != dest:
                    try: old.unlink()
                    except OSError: pass
            return dest
        def success(path):
            self.art_cache[(str(game["path"]), kind)] = path
            # Refresh screenshot fallback cache separately, as .media/<game>.png can be a fallback.
            self.art_cache[(str(game["path"]), "screenshot")] = asset_path_for_game(game, "screenshot")
            self._photos.clear()
            self._set_status(f"Ресурс сохранён: {path}")
            if refresh_callback:
                refresh_callback()
            if self.current_page == "library": self.render_library_groups()
            if self.current_page == "art": self.show_page("art", animate=False)
            mb.showinfo(APP_NAME, f"Изображение установлено.\n\n{path}\n\nROM-файл не изменён.", parent=dialog)
        self._run_task("Подготавливаю изображение PNG…", worker, success)

    def choose_system_asset(self, platform: str, kind: str):
        titles = {"logo": "Логотип системы (logo.png)", "background": "Фон системы (bg.png)", "wallpaper": "Общий фон меню (bg.png в корне карты)"}
        source = fd.askopenfilename(parent=self, title=f"Выбери: {titles[kind]}", filetypes=[("Изображения", "*.png *.jpg *.jpeg *.webp *.bmp"), ("Все файлы", "*.*")])
        if not source: return
        root = self._sd_root()
        if not root:
            mb.showwarning(APP_NAME, "Сначала выбери корень microSD.", parent=self); return
        dest = system_asset_destination(root, self.roms_var.get().strip() or "Roms", platform, kind)
        if dest.exists() and not mb.askyesno(APP_NAME, f"Файл уже существует:\n{dest}\n\nЗаменить его?", parent=self): return
        def worker():
            save_as_png(source, dest)
            return dest
        def success(path):
            self._photos.clear()
            self._set_status(f"Оформление системы сохранено: {path}")
            mb.showinfo(APP_NAME, f"Готово!\n\n{path}\n\nВыбор цветов и макета меню выполняется на самой консоли: Settings → Appearance / Layouts.", parent=self)
            if self.current_page == "art": self.show_page("art", animate=False)
        self._run_task("Сохраняю ресурс оформления…", worker, success)

    # ───────────────────────────── Backups ─────────────────────────────
    def build_backup(self, parent):
        panel = ctk.CTkScrollableFrame(parent, fg_color=C["panel"], corner_radius=15, border_width=1, border_color=C["line"])
        panel.grid(row=0, column=0, sticky="nsew")
        ctk.CTkLabel(panel, text="Сохранения — в безопасности", font=ctk.CTkFont(size=22, weight="bold"), text_color=C["text"]).pack(anchor="w", padx=22, pady=(20, 5))
        ctk.CTkLabel(panel, text="Создаёт датированную копию на компьютере. Ничего не удаляет и не изменяет на microSD.", font=ctk.CTkFont(size=10), text_color=C["muted"], wraplength=900, justify="left").pack(anchor="w", padx=22, pady=(0, 17))
        pathrow = ctk.CTkFrame(panel, fg_color="transparent")
        pathrow.pack(fill="x", padx=20)
        ctk.CTkEntry(pathrow, textvariable=self.backup_var, height=37, placeholder_text="Папка на компьютере для резервных копий").pack(side="left", fill="x", expand=True)
        self.button(pathrow, "Обзор…", self.choose_backup_folder, "secondary", width=90, height=37).pack(side="left", padx=(8, 0))
        section = self.section(panel, "Что копировать", "Рекомендуемый профиль для NX REDUX. .userdata содержит настройки и save states; такая копия предназначена прежде всего для восстановления этой же карты/модели.")
        section.pack(fill="x", padx=20, pady=(20, 12))
        self.backup_checks = {}
        options = [
            ("Игровые сохранения", "Saves", True, "Saves/"),
            ("Пользовательские данные NX REDUX", ".userdata", True, ".userdata/ · настройки, состояния и журналы"),
            ("BIOS", "Bios", True, "Bios/"),
            ("Коллекции", "Collections", False, "Collections/"),
            ("Обложки и ресурсы оформления", "@artwork", False, "Все каталоги .media внутри Roms/"),
        ]
        for title, key, default, description in options:
            row = ctk.CTkFrame(section, fg_color=C["panel2"], corner_radius=8)
            row.pack(fill="x", padx=12, pady=3)
            var = ctk.BooleanVar(value=default)
            self.backup_checks[key] = var
            ctk.CTkCheckBox(row, text=title, variable=var, checkbox_width=19, checkbox_height=19, font=ctk.CTkFont(size=11, weight="bold"), text_color=C["text"]).pack(anchor="w", padx=11, pady=(8, 1))
            ctk.CTkLabel(row, text=description, font=ctk.CTkFont(size=9), text_color=C["muted"]).pack(anchor="w", padx=39, pady=(0, 8))
        warn = ctk.CTkFrame(panel, fg_color="#302719", corner_radius=10, border_width=1, border_color="#5b4628")
        warn.pack(fill="x", padx=20, pady=(2, 12))
        ctk.CTkLabel(warn, text="Важно про .userdata", font=ctk.CTkFont(size=10, weight="bold"), text_color=C["yellow"]).pack(anchor="w", padx=12, pady=(10, 3))
        ctk.CTkLabel(warn, text="Это скрытая служебная папка NX REDUX. Не редактируй её вручную и не восстанавливай настройки поверх другой модели устройства без необходимости.", font=ctk.CTkFont(size=9), text_color="#d4c5ab", wraplength=850, justify="left").pack(anchor="w", padx=12, pady=(0, 10))
        self.backup_progress_text = ctk.CTkLabel(panel, text="", text_color=C["muted"], font=ctk.CTkFont(size=10))
        self.backup_progress_text.pack(anchor="w", padx=22, pady=(5, 2))
        self.backup_progress = ctk.CTkProgressBar(panel, height=8, progress_color=C["accent"], fg_color=C["line"])
        self.backup_progress.pack(fill="x", padx=22, pady=(0, 12))
        self.backup_progress.set(0)
        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.pack(anchor="w", padx=22, pady=(0, 17))
        self.button(actions, "Создать резервную копию", self.backup_now_async, "primary", height=40).pack(side="left")
        self.button(actions, "Открыть резервные копии", lambda: self.open_folder(self.backup_var.get()), "secondary", height=40).pack(side="left", padx=8)

    def choose_backup_folder(self):
        chosen = fd.askdirectory(parent=self, title="Выбери папку для backup")
        if chosen:
            self.backup_var.set(chosen)
            self._save_cfg()

    def backup_now_async(self):
        root = self._sd_root()
        if not root or not root.is_dir():
            mb.showwarning(APP_NAME, "Сначала выбери корень microSD.", parent=self); return
        backup_text = self.backup_var.get().strip()
        if not backup_text:
            mb.showwarning(APP_NAME, "Выбери папку для резервной копии.", parent=self); return
        destination_root = Path(backup_text).expanduser()
        try:
            resolved_root = root.resolve()
            resolved_destination_root = destination_root.resolve()
            if resolved_destination_root == resolved_root or resolved_root in resolved_destination_root.parents:
                mb.showwarning(APP_NAME, "Папка резервных копий должна находиться вне microSD. Иначе можно заполнить карту или создать циклическое копирование.", parent=self)
                return
        except OSError:
            pass
        selected = [key for key, value in getattr(self, "backup_checks", {}).items() if value.get()]
        if not selected:
            mb.showwarning(APP_NAME, "Отметь хотя бы один пункт для копирования.", parent=self); return
        sources: list[tuple[str, Path]] = []
        for key in selected:
            if key == "@artwork":
                romroot = locate_roms(root, self.roms_var.get().strip() or "Roms")
                if romroot:
                    for current, dirs, _files in os.walk(romroot):
                        dirs[:] = [d for d in dirs if not d.startswith(".") or d == ".media"]
                        current_path = Path(current)
                        if current_path.name == ".media":
                            sources.append((str(current_path.relative_to(root)), current_path))
                            dirs[:] = []
                continue
            real_key = "Bios" if key.casefold() == "bios" and not (root / key).exists() and (root / "BIOS").exists() else key
            src = root / real_key
            if src.exists(): sources.append((real_key, src))
        if not sources:
            mb.showwarning(APP_NAME, "Выбранные каталоги не найдены на карте. Проверь содержимое microSD.", parent=self); return
        destination = destination_root / ("NX_REDUX_backup_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
        total_files = 0
        for _rel, src in sources:
            if src.is_file(): total_files += 1
            else:
                for _cur, _dirs, files in os.walk(src): total_files += len(files)
        if not mb.askyesno(APP_NAME, f"Создать резервную копию?\n\nИсточник: {root}\nНазначение: {destination}\nКаталогов: {len(sources)}\nФайлов примерно: {total_files}\n\nИсходники останутся без изменений.", parent=self):
            return

        def worker():
            destination.mkdir(parents=True, exist_ok=False)
            count = 0
            manifest = ["TrimUI Companion NX REDUX backup", "Created: " + datetime.now().isoformat(timespec="seconds"), "Source: " + str(root), "", "Included:"]
            try:
                for relative, src in sources:
                    target = destination / relative
                    if src.is_dir():
                        for current, dirs, files in os.walk(src):
                            # Preserve hidden folders inside Saves/.userdata/Collections;
                            # do not recurse into symlinks that could loop outside the card.
                            dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()]
                            current_path = Path(current)
                            rel = current_path.relative_to(src)
                            outdir = target / rel
                            outdir.mkdir(parents=True, exist_ok=True)
                            for name in files:
                                source_file = current_path / name
                                out = outdir / name
                                shutil.copy2(source_file, out)
                                count += 1
                                if count % 25 == 0 or count == total_files:
                                    pct = min(1.0, count / max(1, total_files))
                                    self.after(0, lambda p=pct, c=count, t=total_files: self._backup_update_progress(p, c, t))
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, target)
                        count += 1
                    manifest.append(f"{relative} -> {src}")
                (destination / "backup_manifest.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
                return destination, count, len(sources)
            except Exception:
                # Keep a partial backup for diagnostics; never delete user data automatically.
                raise

        def success(result):
            path, count, catalogues = result
            self._backup_update_progress(1, count, count)
            self._set_status(f"Резервная копия готова: {path}")
            mb.showinfo(APP_NAME, f"Резервная копия создана.\n\n{path}\n\nФайлов скопировано: {count}", parent=self)
        self._run_task("Копирую выбранные данные в фоне…", worker, success)

    def _backup_update_progress(self, pct, count, total):
        if hasattr(self, "backup_progress") and self.backup_progress.winfo_exists():
            self.backup_progress.set(pct)
            self.backup_progress_text.configure(text=f"Скопировано {count} из примерно {total} файлов")

    # ───────────────────────────── Diagnostics ─────────────────────────────
    def build_health(self, parent):
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        scroll.grid(row=0, column=0, sticky="nsew")
        hero = ctk.CTkFrame(scroll, fg_color=C["panel"], corner_radius=14, border_width=1, border_color=C["line"])
        hero.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(hero, text="Проверяем структуру, не меняя файлы", font=ctk.CTkFont(size=20, weight="bold"), text_color=C["text"]).pack(anchor="w", padx=18, pady=(17, 5))
        ctk.CTkLabel(hero, text="Диагностика только читает microSD. Она не трогает .system/ и не перестраивает разделы.", font=ctk.CTkFont(size=10), text_color=C["muted"], wraplength=900, justify="left").pack(anchor="w", padx=18, pady=(0, 13))
        actions = ctk.CTkFrame(hero, fg_color="transparent")
        actions.pack(anchor="w", padx=16, pady=(0, 15))
        self.button(actions, "Проверить сейчас", self.run_diagnostics, "primary", height=36).pack(side="left")
        self.button(actions, "Открыть сайт NX REDUX", lambda: webbrowser.open("https://nxredux.com/handheld/guide/file-access/"), "secondary", height=36).pack(side="left", padx=8)
        self.health_results = ctk.CTkFrame(scroll, fg_color=C["panel"], corner_radius=14, border_width=1, border_color=C["line"])
        self.health_results.pack(fill="x", pady=(0, 12))
        self._draw_health_results(self.health_results, None)
        if self.games:
            summary = ctk.CTkFrame(scroll, fg_color=C["panel"], corner_radius=14, border_width=1, border_color=C["line"])
            summary.pack(fill="x")
            ctk.CTkLabel(summary, text="Покрытие обложками", font=ctk.CTkFont(size=14, weight="bold"), text_color=C["text"]).pack(anchor="w", padx=18, pady=(15, 8))
            for platform, games in sorted(self.platform_counts.items(), key=lambda x: x[0].casefold()):
                yes = sum(1 for g in games if self._cache_art_path(g, "screenshot"))
                row = ctk.CTkFrame(summary, fg_color="transparent")
                row.pack(fill="x", padx=18, pady=4)
                ctk.CTkLabel(row, text=platform, width=230, anchor="w", text_color=C["muted"], font=ctk.CTkFont(size=10)).pack(side="left")
                bar = ctk.CTkProgressBar(row, height=7, progress_color=C["accent"], fg_color=C["line"])
                bar.pack(side="left", fill="x", expand=True, padx=10)
                bar.set(yes / len(games) if games else 0)
                ctk.CTkLabel(row, text=f"{yes}/{len(games)}", width=75, anchor="e", text_color=C["text"], font=ctk.CTkFont(size=9)).pack(side="right")
            ctk.CTkLabel(summary, text="NX REDUX поддерживает Screenshot, 3D box art, 2D box art, Wheel и Mix в отдельных папках .media.", text_color=C["dim"], font=ctk.CTkFont(size=9), wraplength=850).pack(anchor="w", padx=18, pady=(8, 15))

    def _draw_health_results(self, frame, data):
        for w in frame.winfo_children(): w.destroy()
        ctk.CTkLabel(frame, text="РЕЗУЛЬТАТЫ ПРОВЕРКИ", font=ctk.CTkFont(size=10, weight="bold"), text_color=C["dim"]).pack(anchor="w", padx=17, pady=(14, 8))
        if data is None:
            ctk.CTkLabel(frame, text="Нажми «Проверить сейчас», чтобы проверить Roms, папки сохранений и теги систем.", font=ctk.CTkFont(size=10), text_color=C["muted"], wraplength=850, justify="left").pack(anchor="w", padx=17, pady=(0, 15))
            return
        for item in data["checks"]:
            row = ctk.CTkFrame(frame, fg_color=C["panel2"], corner_radius=8)
            row.pack(fill="x", padx=12, pady=3)
            color = C["accent"] if item["ok"] else C["yellow"]
            ctk.CTkLabel(row, text="✓" if item["ok"] else "!", text_color=color, font=ctk.CTkFont(size=15, weight="bold"), width=30).pack(side="left", padx=(7, 0), pady=9)
            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True, pady=8, padx=7)
            ctk.CTkLabel(info, text=item["title"], text_color=C["text"], font=ctk.CTkFont(size=10, weight="bold"), anchor="w").pack(anchor="w")
            ctk.CTkLabel(info, text=item["detail"], text_color=C["muted"], font=ctk.CTkFont(size=9), wraplength=830, justify="left", anchor="w").pack(anchor="w", pady=(2, 0))

    def run_diagnostics(self):
        root = self._sd_root()
        if not root or not root.is_dir():
            mb.showwarning(APP_NAME, "Выбери корень microSD.", parent=self); return
        def worker(): return inspect_card(root, self.roms_var.get().strip() or "Roms", self.games)
        def success(data):
            self._draw_health_results(self.health_results, data)
            try:
                disk = shutil.disk_usage(root)
                self._set_status(f"Карта: свободно {fmt_size(disk.free)} из {fmt_size(disk.total)} · обложек в screenshot: {data['screenshot_count']}")
            except Exception:
                self._set_status(f"Диагностика завершена · обложек в screenshot: {data['screenshot_count']}")
        self._run_task("Проверяю структуру карты (только чтение)…", worker, success)

    # ───────────────────────────── SFTP ─────────────────────────────
    def build_transfer(self, parent):
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        scroll.grid(row=0, column=0, sticky="nsew")
        hero = self.section(scroll, "Передача по Wi-Fi через SFTP", "NX REDUX умеет открывать SFTP по SSH. Включи его на консоли, чтобы передавать обложки и файлы без извлечения microSD.")
        hero.pack(fill="x", pady=(0, 12))
        if paramiko is None:
            ctk.CTkLabel(hero, text="SFTP-модуль недоступен в этом запуске. В обычной сборке EXE он включён.", text_color=C["yellow"], font=ctk.CTkFont(size=10)).pack(anchor="w", padx=18, pady=(0, 13))
        form = ctk.CTkFrame(hero, fg_color=C["panel2"], corner_radius=10)
        form.pack(fill="x", padx=14, pady=(0, 13))
        form.grid_columnconfigure(1, weight=1)
        fields = [
            ("IP / адрес консоли", self.host_var, "Например, 192.168.1.45"),
            ("Порт SSH", self.port_var, "22"),
            ("Пользователь", self.user_var, "root"),
            ("Пароль (не сохраняется)", self.password_var, "Пароль из настроек устройства"),
            ("Путь microSD на консоли", self.remote_var, "/mnt/SDCARD"),
        ]
        for i, (label, variable, placeholder) in enumerate(fields):
            ctk.CTkLabel(form, text=label, width=175, anchor="w", text_color=C["muted"], font=ctk.CTkFont(size=10)).grid(row=i, column=0, sticky="w", padx=(12, 7), pady=6)
            show = "●" if "Пароль" in label else ""
            entry = ctk.CTkEntry(form, textvariable=variable, placeholder_text=placeholder, show=show, height=33)
            entry.grid(row=i, column=1, sticky="ew", padx=(0, 12), pady=6)
        actions = ctk.CTkFrame(hero, fg_color="transparent")
        actions.pack(anchor="w", padx=15, pady=(0, 15))
        self.button(actions, "Подключиться", self.connect_sftp, "primary", height=37).pack(side="left")
        self.button(actions, "Отключиться", self.disconnect_sftp, "secondary", height=37).pack(side="left", padx=8)
        self.button(actions, "Открыть WinSCP", lambda: webbrowser.open("https://winscp.net/eng/download.php"), "secondary", height=37).pack(side="left")

        advice = self.section(scroll, "Как включить SFTP в NX REDUX", "На консоли: Settings → Developer → Enable SSH / SFTP. Подсказка рядом с переключателем показывает IP и логин. Путь карты обычно /mnt/SDCARD.")
        advice.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(advice, text="Оставляй SSH включённым только в доверенной сети. После передачи отключи его на консоли. Пароль в настройках этой программы не сохраняется; неизвестный ключ сервера потребуется подтвердить вручную.", text_color=C["yellow"], font=ctk.CTkFont(size=10), wraplength=880, justify="left").pack(anchor="w", padx=18, pady=(0, 14))

        remote_panel = ctk.CTkFrame(scroll, fg_color=C["panel"], corner_radius=14, border_width=1, border_color=C["line"])
        remote_panel.pack(fill="both", expand=True)
        remote_head = ctk.CTkFrame(remote_panel, fg_color="transparent")
        remote_head.pack(fill="x", padx=16, pady=(13, 7))
        ctk.CTkLabel(remote_head, text="Удалённая microSD", font=ctk.CTkFont(size=14, weight="bold"), text_color=C["text"]).pack(side="left")
        self.remote_status = ctk.CTkLabel(remote_head, text="Не подключено", font=ctk.CTkFont(size=9), text_color=C["dim"])
        self.remote_status.pack(side="right")
        listing_buttons = ctk.CTkFrame(remote_panel, fg_color="transparent")
        listing_buttons.pack(fill="x", padx=15, pady=(0, 8))
        self.button(listing_buttons, "Обновить список", self.refresh_remote_listing, "secondary", height=30).pack(side="left")
        self.button(listing_buttons, "Перейти к пути…", self.change_remote_path, "secondary", height=30).pack(side="left", padx=7)
        self.remote_listing_frame = ctk.CTkScrollableFrame(remote_panel, fg_color="#0b1420", corner_radius=9, height=180)
        self.remote_listing_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._empty_state(self.remote_listing_frame, "Нет соединения", "Подключись к консоли, чтобы просмотреть файлы.")

    def connect_sftp(self):
        if paramiko is None:
            mb.showerror(APP_NAME, "В сборке нет модуля paramiko. Пересобери программу с requirements.txt.", parent=self); return
        host = self.host_var.get().strip()
        try: port = int(self.port_var.get().strip() or "22")
        except ValueError:
            mb.showwarning(APP_NAME, "Порт должен быть числом.", parent=self); return
        if not host:
            mb.showwarning(APP_NAME, "Введи IP-адрес из Settings → Developer на консоли.", parent=self); return
        user = self.user_var.get().strip() or "root"
        password = self.password_var.get()
        remote = self.remote_var.get().strip() or "/mnt/SDCARD"

        def worker():
            client = paramiko.SSHClient()
            if KNOWN_HOSTS.exists():
                client.load_host_keys(str(KNOWN_HOSTS))
            client.set_missing_host_key_policy(ConfirmHostKey(self))
            client.connect(hostname=host, port=port, username=user, password=password or None, timeout=12, banner_timeout=12, auth_timeout=12, allow_agent=True, look_for_keys=True)
            sftp = client.open_sftp()
            attrs = sftp.listdir_attr(remote)
            return client, sftp, attrs, remote

        def success(result):
            self.disconnect_sftp(silent=True)
            self.ssh_client, self.sftp, attrs, remote = result
            self.password_var.set("")  # never keep the password visible or persist it after connecting
            self.remote_var.set(remote)
            self.sftp_remote_listing = [
                {"name": a.filename, "is_dir": bool(a.st_mode and (a.st_mode & 0o170000) == 0o040000), "size": int(a.st_size or 0)} for a in attrs
            ]
            self.remote_status.configure(text=f"Подключено: {host}:{port}", text_color=C["accent"])
            self.render_remote_listing()
            self._save_cfg()
            self._set_status(f"SFTP подключён к {host} · {remote}")

        def error(exc):
            self.remote_status.configure(text="Ошибка подключения", text_color=C["red"])
            mb.showerror(APP_NAME, f"Не удалось подключиться по SFTP.\n\nПроверь IP, пароль, сеть и включён ли SSH/SFTP на консоли.\n\n{exc}", parent=self)
        self._run_task("Подключаюсь к NX REDUX по SSH/SFTP…", worker, success, error)

    def disconnect_sftp(self, silent: bool = False):
        try:
            if self.sftp: self.sftp.close()
        except Exception: pass
        try:
            if self.ssh_client: self.ssh_client.close()
        except Exception: pass
        self.sftp = None
        self.ssh_client = None
        self.sftp_remote_listing = []
        if hasattr(self, "remote_status") and self.remote_status.winfo_exists():
            self.remote_status.configure(text="Не подключено", text_color=C["dim"])
        if hasattr(self, "remote_listing_frame") and self.remote_listing_frame.winfo_exists():
            self._empty_state(self.remote_listing_frame, "Нет соединения", "Подключись к консоли, чтобы просмотреть файлы.")
        if not silent:
            self._set_status("SFTP отключён.")

    def refresh_remote_listing(self):
        if not self.sftp:
            mb.showinfo(APP_NAME, "Сначала подключись к консоли.", parent=self); return
        path = self.remote_var.get().strip() or "/mnt/SDCARD"
        def worker():
            attrs = self.sftp.listdir_attr(path)
            return [{"name": a.filename, "is_dir": bool(a.st_mode and (a.st_mode & 0o170000) == 0o040000), "size": int(a.st_size or 0)} for a in attrs]
        def success(items):
            self.sftp_remote_listing = items
            self.render_remote_listing()
            self._set_status(f"Содержимое получено: {path}")
        self._run_task("Читаю список удалённых файлов…", worker, success)

    def render_remote_listing(self):
        if not hasattr(self, "remote_listing_frame") or not self.remote_listing_frame.winfo_exists(): return
        for w in self.remote_listing_frame.winfo_children(): w.destroy()
        if not self.sftp:
            self._empty_state(self.remote_listing_frame, "Нет соединения", "Подключись к консоли, чтобы просмотреть файлы."); return
        current = self.remote_var.get().rstrip("/") or "/"
        up = ctk.CTkFrame(self.remote_listing_frame, fg_color=C["panel2"], corner_radius=7)
        up.pack(fill="x", pady=2, padx=2)
        ctk.CTkLabel(up, text="↑  .. (на уровень выше)", text_color=C["accent"], font=ctk.CTkFont(size=10)).pack(anchor="w", padx=10, pady=8)
        up.bind("<Double-Button-1>", lambda _e: self.navigate_remote("/" if current == "/" else str(Path(current).parent).replace("\\", "/")))
        for item in sorted(self.sftp_remote_listing, key=lambda v: (not v["is_dir"], v["name"].casefold()))[:500]:
            row = ctk.CTkFrame(self.remote_listing_frame, fg_color=C["panel2"] if item["is_dir"] else C["panel"], corner_radius=7)
            row.pack(fill="x", padx=2, pady=2)
            label = ctk.CTkLabel(row, text=("▰  " if item["is_dir"] else "▤  ") + item["name"], text_color=C["text"], anchor="w", font=ctk.CTkFont(size=10))
            label.pack(side="left", fill="x", expand=True, padx=10, pady=8)
            if not item["is_dir"]: ctk.CTkLabel(row, text=fmt_size(item["size"]), text_color=C["dim"], font=ctk.CTkFont(size=9)).pack(side="right", padx=10)
            if item["is_dir"]:
                for w in (row, label): w.bind("<Double-Button-1>", lambda _e, n=item["name"]: self.navigate_remote((current.rstrip("/") + "/" + n) if current != "/" else "/" + n), add="+")
        if not self.sftp_remote_listing:
            ctk.CTkLabel(self.remote_listing_frame, text="Папка пуста.", text_color=C["muted"]).pack(pady=15)

    def navigate_remote(self, path: str):
        self.remote_var.set(path or "/")
        self.refresh_remote_listing()

    def change_remote_path(self):
        from tkinter import simpledialog
        new = simpledialog.askstring("Удалённый путь", "Путь на microSD консоли:", initialvalue=self.remote_var.get(), parent=self)
        if new:
            self.remote_var.set(new.strip())
            self.refresh_remote_listing()

    def upload_game_art_sftp(self, game, kind, dialog):
        if not self.sftp:
            mb.showwarning(APP_NAME, "Сначала подключись к консоли на странице Wi-Fi / SFTP.", parent=dialog); return
        local_art = asset_path_for_game(game, kind)
        if not local_art or not local_art.exists():
            mb.showwarning(APP_NAME, "Сначала установи изображение в редакторе, затем передай его на консоль.", parent=dialog); return
        root_remote = self.remote_var.get().rstrip("/")
        sd_root = self._sd_root()
        romroot = locate_roms(sd_root, self.roms_var.get().strip() or "Roms") if sd_root else None
        if not romroot:
            mb.showwarning(APP_NAME, "Сначала выбери корень microSD и просканируй библиотеку.", parent=dialog); return
        try:
            artwork_base_relative = Path(game["art_base"]).relative_to(romroot)
        except ValueError:
            mb.showerror(APP_NAME, "Не удалось определить путь игры относительно Roms. Повтори сканирование.", parent=dialog); return
        relative = Path(self.roms_var.get().strip() or "Roms") / artwork_base_relative / ".media" / ART_DIRS.get(kind, "screenshot") / f"{game['stem']}.png"
        remote_target = root_remote + "/" + relative.as_posix()
        if not mb.askyesno(APP_NAME, f"Передать изображение на консоль?\n\n{local_art}\n\n→ {remote_target}\n\nСуществующий файл будет заменён, если совпадает имя.", parent=dialog): return
        def worker():
            path = Path(remote_target).parent.as_posix()
            parts = path.split("/")
            current = "" if path.startswith("/") else "."
            for part in parts:
                if not part: continue
                current += "/" + part if current not in ("", ".") else ("/" + part if path.startswith("/") else part)
                try: self.sftp.stat(current)
                except IOError: self.sftp.mkdir(current)
            cache_dir = CFG_DIR / "sftp_upload_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            temp_png = cache_dir / (game["stem"] + ".png")
            save_as_png(local_art, temp_png)
            try:
                self.sftp.put(str(temp_png), remote_target)
            finally:
                try: temp_png.unlink()
                except OSError: pass
            return remote_target
        def success(path):
            self._set_status(f"Изображение передано на консоль: {path}")
            mb.showinfo(APP_NAME, f"Передача завершена.\n\n{path}", parent=dialog)
        self._run_task("Передаю изображение по SFTP…", worker, success)

    # ───────────────────────────── Settings ─────────────────────────────
    def build_settings(self, parent):
        panel = ctk.CTkScrollableFrame(parent, fg_color=C["panel"], corner_radius=15, border_width=1, border_color=C["line"])
        panel.grid(row=0, column=0, sticky="nsew")
        ctk.CTkLabel(panel, text="Настройка под свою карту", font=ctk.CTkFont(size=22, weight="bold"), text_color=C["text"]).pack(anchor="w", padx=22, pady=(21, 5))
        ctk.CTkLabel(panel, text="Путь к папкам соответствует NX REDUX. Не меняй названия каталогов, если не знаешь, зачем это нужно.", font=ctk.CTkFont(size=10), text_color=C["muted"], wraplength=900, justify="left").pack(anchor="w", padx=22, pady=(0, 19))
        self._settings_path_row(panel, "Корень microSD", self.sd_var, self.choose_sd, "Папка, внутри которой находится Roms")
        self._settings_text_row(panel, "Папка игр", self.roms_var, "Roms")
        self._settings_path_row(panel, "Backup на компьютере", self.backup_var, self.choose_backup_folder, "Папка для резервных копий вне microSD")
        art_info = ctk.CTkFrame(panel, fg_color="#122739", corner_radius=10, border_width=1, border_color="#24455b")
        art_info.pack(fill="x", padx=22, pady=(15, 6))
        ctk.CTkLabel(art_info, text="ПУТИ РЕСУРСОВ NX REDUX", font=ctk.CTkFont(size=9, weight="bold"), text_color=C["accent"]).pack(anchor="w", padx=13, pady=(11, 4))
        for value in [
            "Игровой скриншот: Roms/Система/.media/screenshot/ИмяИгры.png",
            "3D-обложка: .media/boxart/ИмяИгры.png  ·  2D: .media/boxart2d/ИмяИгры.png",
            "Логотип системы: Roms/Система/.media/logo.png  ·  фон: .media/bg.png",
            "Общий фон меню: bg.png в корне microSD",
        ]:
            ctk.CTkLabel(art_info, text="•  " + value, font=ctk.CTkFont(size=9), text_color=C["muted"], anchor="w", wraplength=900, justify="left").pack(anchor="w", padx=13, pady=2)
        ctk.CTkLabel(art_info, text="У NX REDUX нет универсальной папки Themes для установки любых тем. Цвета и макет выбираются на консоли в Settings → Appearance / Layouts.", font=ctk.CTkFont(size=9), text_color=C["yellow"], wraplength=900, justify="left").pack(anchor="w", padx=13, pady=(6, 12))
        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.pack(anchor="w", padx=22, pady=(17, 8))
        self.button(actions, "Сохранить настройки", lambda: self._save_cfg(notify=True), "primary", height=40).pack(side="left")
        self.button(actions, "Открыть руководство NX REDUX", lambda: webbrowser.open("https://nxredux.com/handheld/settings/appearance/"), "secondary", height=40).pack(side="left", padx=8)
        ctk.CTkLabel(panel, text="TrimUI Companion не изменяет .system/ и не прошивает устройство. Перед большими изменениями делай резервную копию.", font=ctk.CTkFont(size=9), text_color=C["dim"], wraplength=900, justify="left").pack(anchor="w", padx=22, pady=(13, 20))

    def _settings_text_row(self, parent, label, variable, placeholder=""):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=22, pady=6)
        ctk.CTkLabel(row, text=label, width=180, anchor="w", text_color=C["muted"], font=ctk.CTkFont(size=10)).pack(side="left")
        ctk.CTkEntry(row, textvariable=variable, height=36, placeholder_text=placeholder).pack(side="left", fill="x", expand=True)

    def _settings_path_row(self, parent, label, variable, command, placeholder=""):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=22, pady=6)
        ctk.CTkLabel(row, text=label, width=180, anchor="w", text_color=C["muted"], font=ctk.CTkFont(size=10)).pack(side="left")
        ctk.CTkEntry(row, textvariable=variable, height=36, placeholder_text=placeholder).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.button(row, "Обзор…", command, "secondary", width=85, height=36).pack(side="left")

    # ───────────────────────────── Shared helpers ─────────────────────────────
    def choose_sd(self):
        chosen = fd.askdirectory(parent=self, title="Выбери корень microSD NX REDUX — папку, внутри которой находится Roms")
        if not chosen: return
        self.sd_var.set(chosen)
        self._save_cfg()
        self._update_device_badge()
        self.scan_async(silent=True)

    def open_folder(self, path):
        p = Path(path).expanduser()
        if not p.exists():
            if not mb.askyesno(APP_NAME, f"Папка ещё не существует:\n{p}\n\nСоздать её?", parent=self): return
            try: p.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                mb.showerror(APP_NAME, str(exc), parent=self); return
        try: reveal_path(p)
        except Exception as exc: mb.showerror(APP_NAME, str(exc), parent=self)

    def _empty_state(self, parent, title, subtitle):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=15, pady=20)
        ctk.CTkLabel(frame, text="◇", font=ctk.CTkFont(size=35, weight="bold"), text_color="#36566d").pack(pady=(10, 7))
        ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(size=14, weight="bold"), text_color=C["text"]).pack()
        ctk.CTkLabel(frame, text=subtitle, font=ctk.CTkFont(size=10), text_color=C["muted"], wraplength=470, justify="center").pack(pady=(5, 10))

    def _show_text_dialog(self, title, text, width=700, height=520):
        win = ctk.CTkToplevel(self)
        win.title(title); win.geometry(f"{width}x{height}"); win.configure(fg_color=C["bg"]); win.transient(self)
        ctk.CTkLabel(win, text=title, font=ctk.CTkFont(size=18, weight="bold"), text_color=C["text"]).pack(anchor="w", padx=17, pady=(16, 8))
        box = ctk.CTkTextbox(win, fg_color=C["panel"], text_color=C["muted"], font=ctk.CTkFont(size=10), wrap="none")
        box.pack(fill="both", expand=True, padx=15, pady=(0, 15))
        box.insert("1.0", text); box.configure(state="disabled")

    def _on_close(self):
        self.stop_event.set()
        self.disconnect_sftp(silent=True)
        try: self._save_cfg()
        except Exception: pass
        self.destroy()


def main():
    app = TrimUIApp()
    app.mainloop()


if __name__ == "__main__":
    main()
