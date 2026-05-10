# music_usb.py

import base64
import ctypes
import hashlib
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import pygame
import vlc
import tkinter as tk
from tkinter import messagebox
from tkinter import scrolledtext
from tkinter import ttk

try:
    from mutagen import File as MutagenFile
except Exception:
    MutagenFile = None


DOWNLOAD_FOLDER = Path("downloads")
APP_DATA_FOLDER = Path("usbify_data")
CACHE_FOLDER = APP_DATA_FOLDER / "cache"
COVER_CACHE_FOLDER = CACHE_FOLDER / "covers"
PREVIEW_CACHE_FOLDER = CACHE_FOLDER / "preview"
DB_FILE = APP_DATA_FOLDER / "usbify.db"
APP_STATE_FILE = APP_DATA_FOLDER / "app_state.json"

LEGACY_QUEUE_FILE = APP_DATA_FOLDER / "queue.json"
LEGACY_HISTORY_FILE = APP_DATA_FOLDER / "history.json"
LEGACY_FAVORITES_FILE = APP_DATA_FOLDER / "favorites.json"
LEGACY_DOWNLOAD_STATE_FILE = APP_DATA_FOLDER / "download_state.json"

SUPPORTED_DOMAINS = [
    "spotify.com",
    "youtube.com",
    "youtu.be",
    "soundcloud.com",
    "music.youtube.com",
]

PLAYABLE_EXTENSIONS = {".mp3", ".wav", ".ogg", ".mp4", ".aac", ".m4a"}
SCAN_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".mp4"}
NOTIFICATION_SONG_LIMIT = 55
MAX_HISTORY_ITEMS = 300
MAX_RECENT_SONGS = 15
DOWNLOAD_RETRY_LIMIT = 3
USB_MONITOR_INTERVAL = 3
LIBRARY_SCAN_LIMIT = 1000
APP_ID = "USBIFY.Player"

VIDEO_FORMATS = {
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "Best": "bestvideo+bestaudio/best",
}

SPOTIFY_BG = "#121212"
SPOTIFY_PANEL = "#181818"
SPOTIFY_BLACK = "#000000"
SPOTIFY_GREEN = "#1ed760"
SPOTIFY_MUTED = "#b3b3b3"


def resource_path(name):
    bundle_base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_base / name


def ensure_app_folders():
    DOWNLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    APP_DATA_FOLDER.mkdir(parents=True, exist_ok=True)
    CACHE_FOLDER.mkdir(parents=True, exist_ok=True)
    COVER_CACHE_FOLDER.mkdir(parents=True, exist_ok=True)
    PREVIEW_CACHE_FOLDER.mkdir(parents=True, exist_ok=True)


def safe_read_json(path, default):
    try:
        if not path.exists():
            return default
        import json

        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def safe_write_json(path, data):
    try:
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        temp_path.replace(path)
    except Exception:
        pass


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def normalize_media_name(name):
    stem = Path(name).stem.lower()
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"[^\w\s]", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem


def parse_artist_title(name):
    stem = Path(name).stem
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return artist.strip() or "Unknown artist", title.strip() or stem
    return "Unknown artist", stem


def get_startupinfo():
    if os.name != "nt":
        return None

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def find_ffmpeg_location():
    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    path_match = shutil.which(exe_name)
    if path_match:
        return str(Path(path_match).resolve().parent)

    search_dirs = [
        Path(__file__).resolve().parent,
        Path(__file__).resolve().parent / "ffmpeg",
        Path(__file__).resolve().parent / "ffmpeg" / "bin",
        resource_path("."),
        resource_path("ffmpeg"),
        resource_path("ffmpeg/bin"),
    ]

    for folder in search_dirs:
        candidate = folder / exe_name
        if candidate.exists():
            return str(folder.resolve())

    return None


def get_icon_path():
    for icon_name in ("Logo.ico", "USBIFY.ico", "USBFIY.ico"):
        candidate = Path(icon_name)
        if candidate.exists():
            return candidate.resolve()

        bundled = resource_path(icon_name)
        if bundled.exists():
            return bundled.resolve()

    return None


def validate_url(url):
    if url.startswith("ytsearch1:"):
        return True

    return any(domain in url for domain in SUPPORTED_DOMAINS)


def format_seconds(seconds):
    total_seconds = max(0, int(seconds))
    minutes = total_seconds // 60
    remaining_seconds = total_seconds % 60
    return f"{minutes:02}:{remaining_seconds:02}"


def compute_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def supported_file(path):
    return path.is_file() and path.suffix.lower() in SCAN_EXTENSIONS


def is_path_under(path, parent):
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def extract_metadata(path):
    path = Path(path)
    artist, title = parse_artist_title(path.name)
    metadata = {
        "path": str(path.resolve()),
        "title": title,
        "artist": artist,
        "album": "",
        "genre": "Unknown",
        "duration": 0.0,
        "file_size": path.stat().st_size,
        "mtime": path.stat().st_mtime,
    }

    if not MutagenFile:
        return metadata

    try:
        audio = MutagenFile(str(path), easy=True)
        if audio is not None:
            metadata["title"] = (audio.get("title") or [metadata["title"]])[0]
            metadata["artist"] = (audio.get("artist") or [metadata["artist"]])[0]
            metadata["album"] = (audio.get("album") or [""])[0]
            metadata["genre"] = (audio.get("genre") or ["Unknown"])[0]

            if getattr(audio, "info", None) and getattr(audio.info, "length", None):
                metadata["duration"] = float(audio.info.length)
    except Exception:
        pass

    return metadata


def extract_cover_art(song_path):
    if not MutagenFile:
        return None

    song_path = Path(song_path)
    cached_prefix = hashlib.sha1(str(song_path.resolve()).encode("utf-8")).hexdigest()
    existing_candidates = list(COVER_CACHE_FOLDER.glob(f"{cached_prefix}.*"))
    if existing_candidates:
        return existing_candidates[0]

    try:
        audio = MutagenFile(str(song_path))
        if not audio:
            return None

        image_bytes = None
        extension = None

        tags = getattr(audio, "tags", None)
        if tags:
            for tag_value in getattr(tags, "values", lambda: [])():
                mime = getattr(tag_value, "mime", "")
                data = getattr(tag_value, "data", None)
                if data:
                    image_bytes = data
                    extension = ".png" if "png" in mime.lower() else ".jpg"
                    break

            if image_bytes is None and "covr" in tags:
                covr = tags["covr"][0]
                image_bytes = bytes(covr)
                extension = ".jpg"

        if not image_bytes or not extension:
            return None

        cover_path = COVER_CACHE_FOLDER / f"{cached_prefix}{extension}"
        with open(cover_path, "wb") as handle:
            handle.write(image_bytes)
        return cover_path
    except Exception:
        return None


def parse_lrc_file(lrc_path):
    entries = []
    pattern = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,2}))?\](.*)")

    try:
        with open(lrc_path, "r", encoding="utf-8-sig") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                match = pattern.match(line)
                if not match:
                    continue

                minutes = int(match.group(1))
                seconds = int(match.group(2))
                hundredths = int(match.group(3) or 0)
                text = match.group(4).strip() or "..."
                timestamp = minutes * 60 + seconds + (hundredths / 100)
                entries.append((timestamp, text))
    except Exception:
        return []

    return sorted(entries, key=lambda item: item[0])


def get_usb_drive():
    try:
        import psutil
    except Exception:
        return None

    for partition in psutil.disk_partitions():
        drive = partition.device.upper()

        if drive.startswith("C:"):
            continue

        try:
            usage = psutil.disk_usage(partition.mountpoint)
            if usage.total < 10 * 1024 * 1024 * 1024:
                continue
            return partition.device
        except Exception:
            continue

    return None


class LibraryDatabase:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.init_schema()

    def init_schema(self):
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS songs (
                    path TEXT PRIMARY KEY,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    genre TEXT,
                    duration REAL DEFAULT 0,
                    file_size INTEGER DEFAULT 0,
                    mtime REAL DEFAULT 0,
                    sha256 TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    usb_drive TEXT DEFAULT '',
                    last_scanned TEXT DEFAULT '',
                    exists_flag INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS favorites (
                    path TEXT PRIMARY KEY,
                    added_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    title TEXT,
                    artist TEXT,
                    played_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS queue_items (
                    position INTEGER PRIMARY KEY,
                    path TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS playlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS playlist_entries (
                    playlist_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (playlist_id, position)
                );

                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL DEFAULT 0,
                    last_output TEXT DEFAULT '',
                    retries INTEGER DEFAULT 0,
                    last_error TEXT DEFAULT '',
                    updated_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_songs_title ON songs(title);
                CREATE INDEX IF NOT EXISTS idx_songs_artist ON songs(artist);
                CREATE INDEX IF NOT EXISTS idx_songs_sha256 ON songs(sha256);
                CREATE INDEX IF NOT EXISTS idx_history_played_at ON history(played_at);
                """
            )
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    def upsert_song(self, record):
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO songs (
                    path, title, artist, album, genre, duration,
                    file_size, mtime, sha256, source, usb_drive, last_scanned, exists_flag
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(path) DO UPDATE SET
                    title=excluded.title,
                    artist=excluded.artist,
                    album=excluded.album,
                    genre=excluded.genre,
                    duration=excluded.duration,
                    file_size=excluded.file_size,
                    mtime=excluded.mtime,
                    sha256=excluded.sha256,
                    source=excluded.source,
                    usb_drive=excluded.usb_drive,
                    last_scanned=excluded.last_scanned,
                    exists_flag=1
                """,
                (
                    record["path"],
                    record["title"],
                    record["artist"],
                    record["album"],
                    record["genre"],
                    record["duration"],
                    record["file_size"],
                    record["mtime"],
                    record["sha256"],
                    record["source"],
                    record["usb_drive"],
                    record["last_scanned"],
                ),
            )
            self.conn.commit()

    def mark_missing_outside(self, valid_paths, source=None):
        with self.lock:
            if source:
                self.conn.execute(
                    "UPDATE songs SET exists_flag = 0 WHERE source = ? AND path NOT IN ({})".format(
                        ",".join("?" for _ in valid_paths) if valid_paths else "''"
                    ),
                    [source, *valid_paths] if valid_paths else [source],
                )
            else:
                self.conn.execute(
                    "UPDATE songs SET exists_flag = 0 WHERE path NOT IN ({})".format(
                        ",".join("?" for _ in valid_paths) if valid_paths else "''"
                    ),
                    valid_paths,
                )
            self.conn.commit()

    def get_song(self, path):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM songs WHERE path = ?",
                (str(Path(path).resolve()),),
            ).fetchone()
            return dict(row) if row else None

    def search_songs(self, query="", usb_only=False, usb_drive="", limit=LIBRARY_SCAN_LIMIT):
        query = query.strip()
        params = []
        where_parts = ["exists_flag = 1"]

        if usb_only and usb_drive:
            prefix = str((Path(usb_drive) / "MusicUSB" / "Music").resolve())
            where_parts.append("path LIKE ?")
            params.append(prefix + "%")

        if query:
            where_parts.append(
                "(title LIKE ? OR artist LIKE ? OR album LIKE ? OR genre LIKE ? OR path LIKE ?)"
            )
            like = f"%{query}%"
            params.extend([like, like, like, like, like])

        sql = f"""
            SELECT *
            FROM songs
            WHERE {' AND '.join(where_parts)}
            ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE, path COLLATE NOCASE
            LIMIT ?
        """
        params.append(limit)

        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def list_recent_songs(self, limit=LIBRARY_SCAN_LIMIT):
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT *
                FROM songs
                WHERE exists_flag = 1
                ORDER BY last_scanned DESC, artist COLLATE NOCASE, title COLLATE NOCASE
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def set_favorite(self, path, enabled):
        path = str(Path(path).resolve())
        with self.lock:
            if enabled:
                self.conn.execute(
                    "INSERT OR REPLACE INTO favorites(path, added_at) VALUES (?, ?)",
                    (path, now_iso()),
                )
            else:
                self.conn.execute("DELETE FROM favorites WHERE path = ?", (path,))
            self.conn.commit()

    def is_favorite(self, path):
        path = str(Path(path).resolve())
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM favorites WHERE path = ?",
                (path,),
            ).fetchone()
            return bool(row)

    def list_favorites(self):
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT s.*
                FROM favorites f
                JOIN songs s ON s.path = f.path
                WHERE s.exists_flag = 1
                ORDER BY s.artist COLLATE NOCASE, s.title COLLATE NOCASE
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def record_history(self, record):
        with self.lock:
            self.conn.execute(
                "INSERT INTO history(path, title, artist, played_at) VALUES (?, ?, ?, ?)",
                (record["path"], record["title"], record["artist"], now_iso()),
            )
            self.conn.execute(
                """
                DELETE FROM history
                WHERE id NOT IN (
                    SELECT id
                    FROM history
                    ORDER BY played_at DESC
                    LIMIT ?
                )
                """,
                (MAX_HISTORY_ITEMS,),
            )
            self.conn.commit()

    def list_history(self, limit=MAX_HISTORY_ITEMS):
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT *
                FROM history
                ORDER BY played_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def clear_history(self):
        with self.lock:
            self.conn.execute("DELETE FROM history")
            self.conn.commit()

    def save_queue(self, paths):
        with self.lock:
            self.conn.execute("DELETE FROM queue_items")
            for index, path in enumerate(paths):
                self.conn.execute(
                    "INSERT INTO queue_items(position, path) VALUES (?, ?)",
                    (index, str(Path(path).resolve())),
                )
            self.conn.commit()

    def load_queue(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT path FROM queue_items ORDER BY position"
            ).fetchall()
            return [row["path"] for row in rows]

    def create_playlist(self, name):
        name = name.strip()
        if not name:
            return False

        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO playlists(name, created_at) VALUES (?, ?)",
                (name, now_iso()),
            )
            self.conn.commit()
        return True

    def list_playlists(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM playlists ORDER BY name COLLATE NOCASE"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_playlist(self, playlist_id):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM playlists WHERE id = ?",
                (playlist_id,),
            ).fetchone()
            return dict(row) if row else None

    def add_song_to_playlist(self, playlist_id, path):
        path = str(Path(path).resolve())
        with self.lock:
            next_position = self.conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next_pos FROM playlist_entries WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()["next_pos"]
            self.conn.execute(
                """
                INSERT INTO playlist_entries(playlist_id, position, path, added_at)
                VALUES (?, ?, ?, ?)
                """,
                (playlist_id, next_position, path, now_iso()),
            )
            self.conn.commit()

    def replace_playlist_with_queue(self, playlist_id, queue_paths):
        with self.lock:
            self.conn.execute(
                "DELETE FROM playlist_entries WHERE playlist_id = ?",
                (playlist_id,),
            )
            for index, path in enumerate(queue_paths):
                self.conn.execute(
                    """
                    INSERT INTO playlist_entries(playlist_id, position, path, added_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (playlist_id, index, str(Path(path).resolve()), now_iso()),
                )
            self.conn.commit()

    def list_playlist_songs(self, playlist_id):
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT s.*, pe.position
                FROM playlist_entries pe
                JOIN songs s ON s.path = pe.path
                WHERE pe.playlist_id = ? AND s.exists_flag = 1
                ORDER BY pe.position
                """,
                (playlist_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def create_download(self, url, mode, quality):
        created_at = now_iso()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO downloads(url, mode, quality, status, progress, last_output, retries, last_error, updated_at, created_at)
                VALUES (?, ?, ?, 'pending', 0, '', 0, '', ?, ?)
                """,
                (url, mode, quality, created_at, created_at),
            )
            download_id = self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.conn.commit()
            return download_id

    def update_download(self, download_id, **fields):
        if not fields:
            return

        fields["updated_at"] = now_iso()
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [download_id]

        with self.lock:
            self.conn.execute(
                f"UPDATE downloads SET {columns} WHERE id = ?",
                values,
            )
            self.conn.commit()

    def get_pending_downloads(self):
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT *
                FROM downloads
                WHERE status IN ('pending', 'retrying', 'failed')
                ORDER BY updated_at DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_last_download(self):
        with self.lock:
            row = self.conn.execute(
                """
                SELECT *
                FROM downloads
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
            return dict(row) if row else None

    def duplicate_groups(self):
        with self.lock:
            hashes = self.conn.execute(
                """
                SELECT sha256
                FROM songs
                WHERE exists_flag = 1 AND sha256 != ''
                GROUP BY sha256
                HAVING COUNT(*) > 1
                """
            ).fetchall()

            groups = []
            for row in hashes:
                members = self.conn.execute(
                    """
                    SELECT *
                    FROM songs
                    WHERE exists_flag = 1 AND sha256 = ?
                    ORDER BY source DESC, mtime ASC, path COLLATE NOCASE
                    """,
                    (row["sha256"],),
                ).fetchall()
                groups.append([dict(member) for member in members])
            return groups


class FloatingMiniPlayer:
    def __init__(self, app):
        self.app = app
        self.window = None
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self.cover_image = None

    def ensure_window(self):
        if self.window is not None:
            return

        self.window = tk.Toplevel(self.app.root)
        self.window.title("USBIFY Mini Player")
        self.window.geometry("320x150+80+80")
        self.window.configure(bg=SPOTIFY_PANEL)
        self.window.attributes("-topmost", True)
        self.window.withdraw()

        self.header = tk.Frame(self.window, bg="#0f0f0f", height=26)
        self.header.pack(fill="x")

        self.header_label = tk.Label(
            self.header,
            text="USBIFY MINI",
            fg="white",
            bg="#0f0f0f",
            font=("Segoe UI", 9, "bold"),
        )
        self.header_label.pack(side="left", padx=8)

        self.close_button = tk.Button(
            self.header,
            text="X",
            command=self.hide,
            bg="#0f0f0f",
            fg="white",
            relief="flat",
            bd=0,
            activebackground="#0f0f0f",
            activeforeground=SPOTIFY_GREEN,
            cursor="hand2",
        )
        self.close_button.pack(side="right", padx=6)

        for widget in (self.header, self.header_label):
            widget.bind("<ButtonPress-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.drag)

        self.body = tk.Frame(self.window, bg=SPOTIFY_PANEL)
        self.body.pack(fill="both", expand=True, padx=12, pady=10)

        self.cover_label = tk.Label(
            self.body,
            text="♪",
            width=6,
            height=4,
            fg="black",
            bg=SPOTIFY_GREEN,
            font=("Segoe UI", 18, "bold"),
        )
        self.cover_label.pack(side="left", padx=(0, 10))

        self.info_frame = tk.Frame(self.body, bg=SPOTIFY_PANEL)
        self.info_frame.pack(side="left", fill="both", expand=True)

        self.song_label = tk.Label(
            self.info_frame,
            text="No music playing",
            fg="white",
            bg=SPOTIFY_PANEL,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        )
        self.song_label.pack(fill="x")

        self.artist_label = tk.Label(
            self.info_frame,
            text="USBIFY Player",
            fg=SPOTIFY_MUTED,
            bg=SPOTIFY_PANEL,
            font=("Segoe UI", 8),
            anchor="w",
        )
        self.artist_label.pack(fill="x")

        self.time_label = tk.Label(
            self.info_frame,
            text="00:00 / 00:00",
            fg=SPOTIFY_MUTED,
            bg=SPOTIFY_PANEL,
            font=("Segoe UI", 8),
            anchor="w",
        )
        self.time_label.pack(fill="x", pady=(3, 8))

        controls = tk.Frame(self.info_frame, bg=SPOTIFY_PANEL)
        controls.pack(fill="x")

        tk.Button(
            controls,
            text="PLAY",
            command=self.app.play_selected_song,
            bg=SPOTIFY_GREEN,
            fg="black",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
            cursor="hand2",
        ).pack(side="left", padx=(0, 4))

        tk.Button(
            controls,
            text="PAUSE",
            command=self.app.pause_song,
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
            cursor="hand2",
        ).pack(side="left", padx=4)

        tk.Button(
            controls,
            text="NEXT",
            command=self.app.next_song,
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
            cursor="hand2",
        ).pack(side="left", padx=4)

    def start_drag(self, event):
        self.drag_offset_x = event.x_root - self.window.winfo_x()
        self.drag_offset_y = event.y_root - self.window.winfo_y()

    def drag(self, event):
        x = event.x_root - self.drag_offset_x
        y = event.y_root - self.drag_offset_y
        self.window.geometry(f"+{x}+{y}")

    def show(self):
        self.ensure_window()
        self.window.deiconify()
        self.update()

    def hide(self):
        if self.window is not None:
            self.window.withdraw()

    def toggle(self):
        self.ensure_window()
        if self.window.state() == "withdrawn":
            self.show()
        else:
            self.hide()

    def update(self):
        if self.window is None or self.window.state() == "withdrawn":
            return

        metadata = self.app.current_metadata or {}
        self.song_label.config(text=metadata.get("title", "No music playing"))
        self.artist_label.config(text=metadata.get("artist", "USBIFY Player"))
        self.time_label.config(text=self.app.time_label.cget("text"))

        cover_path = self.app.current_cover_path
        if cover_path and Path(cover_path).suffix.lower() == ".png":
            try:
                self.cover_image = tk.PhotoImage(file=str(cover_path))
                self.cover_label.config(image=self.cover_image, text="")
                return
            except Exception:
                self.cover_image = None

        self.cover_label.config(image="", text="♪")


class USBifyApp:
    def __init__(self):
        ensure_app_folders()

        self.shutdown_event = threading.Event()
        self.db = LibraryDatabase(DB_FILE)

        self.audio_ready = False
        self.audio_error = None
        self.current_song_path = None
        self.current_song = None
        self.current_metadata = None
        self.current_cover_path = None
        self.current_lyrics = []
        self.current_lyric_index = -1
        self.current_usb_drive = get_usb_drive()
        self.current_library_items = []
        self.current_playlist_items = []
        self.current_history_items = []
        self.current_favorite_items = []
        self.current_queue_paths = []
        self.sidebar_recent_items = []
        self.current_preview_thread = None
        self.current_download_process = None
        self.progress_job = None
        self.search_job = None
        self.paused = False
        self.stop_requested = False
        self.song_length = 0.0
        self.last_progress_position = 0.0
        self.playback_started_at = None
        self.playback_base_position = 0.0
        self.download_in_progress = False
        self.scan_in_progress = False
        self.sync_in_progress = False
        self.stats_data = {"songs": 0, "gb": 0.0, "time_saved": 0}
        self.playlist_order = []
        self.current_index = -1
        self.last_artist_played = ""
        self.background_animation_job = None
        self.background_animation_phase = 0
        self.cover_animation_job = None
        self.cover_animation_phase = 0
        self.song_table_items = {}
        self.app_state = safe_read_json(APP_STATE_FILE, {})

        self.initialize_audio()

        self.video_instance = vlc.Instance("--avcodec-hw=none", "--vout=directdraw")
        self.video_player = self.video_instance.media_player_new()
        self.video_window = None
        self.video_positions = {}

        self.root = tk.Tk()
        self.root.title("USBIFY")
        self.root.geometry("1650x950")
        self.root.configure(bg="#0b0b0b")
        self.root.minsize(1450, 850)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.fullscreen = False
        self.auto_delete_duplicates_var = tk.BooleanVar(value=False)
        self.auto_sync_var = tk.BooleanVar(value=True)
        self.listen_while_download_var = tk.BooleanVar(value=True)
        self.auto_resume_downloads_var = tk.BooleanVar(value=True)
        self.gaming_mode_var = tk.BooleanVar(value=False)

        self.configure_window()
        self.migrate_legacy_state()

        self.build_ui()

        if not hasattr(self, "video_panel"):
            self.video_panel = tk.Frame(
                self.song_info_frame,
                bg="black",
                width=760,
                height=260,
            )
            self.video_panel.pack(in_=self.song_info_frame, side="left", padx=20, pady=10, fill="both", expand=True)
            self.video_panel.pack_propagate(False)
        self.bind_events()

        self.mini_player = FloatingMiniPlayer(self)
        self.load_queue_from_db()
        self.refresh_playlists_ui()
        self.refresh_history_ui()
        self.refresh_favorites_ui()
        self.sync_favorite_button()
        self.update_stats_label()

        self.log("[+] USBIFY loaded")
        self.log("[+] SQLite library online")
        self.log("[+] Advanced sync / dedupe / karaoke modules ready")

        if self.audio_error:
            self.log(f"[!] Audio init error: {self.audio_error}")

        self.refresh_library_view()
        self.root.after(180, self.start_background_services)
        self.root.after(650, self.restore_last_song)

    def initialize_audio(self):
        try:
            pygame.mixer.init()
            self.audio_ready = True
        except Exception as exc:
            self.audio_ready = False
            self.audio_error = str(exc)

    def configure_window(self):
        self.root.bind("<F11>", self.toggle_fullscreen)
        self.root.bind("<Escape>", self.exit_fullscreen)

        if os.name == "nt":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
            except Exception:
                pass

        icon_path = get_icon_path()
        if icon_path:
            try:
                self.root.iconbitmap(default=str(icon_path))
            except Exception:
                pass

    def migrate_legacy_state(self):
        try:
            queue_data = safe_read_json(LEGACY_QUEUE_FILE, [])
            if queue_data and not self.db.load_queue():
                queue_paths = []
                for item in queue_data:
                    if isinstance(item, str):
                        resolved = self.resolve_song_path_from_name(item)
                        if resolved:
                            queue_paths.append(str(resolved.resolve()))
                self.db.save_queue(queue_paths)

            favorites_data = safe_read_json(LEGACY_FAVORITES_FILE, [])
            for item in favorites_data:
                if isinstance(item, str):
                    resolved = self.resolve_song_path_from_name(item)
                    if resolved:
                        self.db.set_favorite(resolved, True)

            history_data = safe_read_json(LEGACY_HISTORY_FILE, [])
            for item in history_data:
                if isinstance(item, dict) and item.get("name"):
                    resolved = self.resolve_song_path_from_name(item["name"])
                    if resolved:
                        metadata = self.get_metadata(resolved)
                        self.db.record_history(metadata)
        except Exception:
            pass

    def build_ui(self):
        self.sidebar = tk.Frame(self.root, bg=SPOTIFY_BLACK, width=280)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        self.logo_canvas = tk.Canvas(
            self.sidebar,
            width=220,
            height=76,
            bg=SPOTIFY_BLACK,
            highlightthickness=0,
        )
        self.logo_canvas.pack(anchor="w", padx=25, pady=(22, 14))
        self.logo_canvas.create_text(
            0,
            12,
            anchor="nw",
            text="USBIFY",
            fill=SPOTIFY_GREEN,
            font=("Segoe UI", 26, "bold"),
        )
        self.logo_canvas.create_text(
            2,
            50,
            anchor="nw",
            text="Professional Portable Music Manager",
            fill=SPOTIFY_MUTED,
            font=("Segoe UI", 8),
        )

        self.online_search_entry = tk.Entry(
            self.sidebar,
            bg=SPOTIFY_PANEL,
            fg="white",
            relief="flat",
            font=("Segoe UI", 11),
            insertbackground="white",
            bd=12,
        )
        self.online_search_entry.pack(fill="x", padx=20, pady=(0, 12))

        self.search_button = tk.Button(
            self.sidebar,
            text="SEARCH MUSIC",
            command=self.search_online_music,
            bg=SPOTIFY_GREEN,
            fg="black",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        self.search_button.pack(fill="x", padx=20)

        self.url_label = tk.Label(
            self.sidebar,
            text="Paste URL",
            fg=SPOTIFY_MUTED,
            bg=SPOTIFY_BLACK,
            font=("Segoe UI", 10),
        )
        self.url_label.pack(anchor="w", padx=20, pady=(20, 5))

        self.url_entry = tk.Entry(
            self.sidebar,
            bg=SPOTIFY_PANEL,
            fg="white",
            relief="flat",
            font=("Segoe UI", 10),
            insertbackground="white",
            bd=12,
        )
        self.url_entry.pack(fill="x", padx=20)

        self.mode_frame = tk.Frame(self.sidebar, bg=SPOTIFY_BLACK)
        self.mode_frame.pack(fill="x", padx=20, pady=16)

        self.mode_var = tk.StringVar(value="audio")
        self.audio_mode = tk.Radiobutton(
            self.mode_frame,
            text="MP3",
            variable=self.mode_var,
            value="audio",
            bg=SPOTIFY_BLACK,
            fg="white",
            selectcolor=SPOTIFY_PANEL,
            activebackground=SPOTIFY_BLACK,
            activeforeground="white",
        )
        self.audio_mode.pack(anchor="w")

        self.video_mode = tk.Radiobutton(
            self.mode_frame,
            text="MP4",
            variable=self.mode_var,
            value="video",
            bg=SPOTIFY_BLACK,
            fg="white",
            selectcolor=SPOTIFY_PANEL,
            activebackground=SPOTIFY_BLACK,
            activeforeground="white",
        )
        self.video_mode.pack(anchor="w")

        self.quality_label = tk.Label(
            self.sidebar,
            text="Video Quality",
            fg=SPOTIFY_MUTED,
            bg=SPOTIFY_BLACK,
            font=("Segoe UI", 10),
        )
        self.quality_label.pack(anchor="w", padx=20, pady=(0, 5))

        self.quality_var = tk.StringVar(value="1080p")
        self.quality_menu = ttk.Combobox(
            self.sidebar,
            textvariable=self.quality_var,
            values=["1080p", "720p", "480p", "Best"],
            state="readonly",
        )
        self.quality_menu.pack(fill="x", padx=20)

        self.download_button = tk.Button(
            self.sidebar,
            text="DOWNLOAD TO USB",
            command=self.start_download,
            bg=SPOTIFY_GREEN,
            fg="black",
            relief="flat",
            font=("Segoe UI", 11, "bold"),
            cursor="hand2",
        )
        self.download_button.pack(fill="x", padx=20, pady=(18, 8))

        self.resume_button = tk.Button(
            self.sidebar,
            text="RESUME SMART DOWNLOADS",
            command=self.resume_last_download,
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        self.resume_button.pack(fill="x", padx=20, pady=(0, 8))

        self.scan_button = tk.Button(
            self.sidebar,
            text="SCAN SQLITE LIBRARY",
            command=self.start_library_scan,
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        self.scan_button.pack(fill="x", padx=20, pady=(0, 8))

        self.sync_button = tk.Button(
            self.sidebar,
            text="SYNC USB NOW",
            command=self.start_usb_sync,
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        self.sync_button.pack(fill="x", padx=20, pady=(0, 8))

        self.dedupe_button = tk.Button(
            self.sidebar,
            text="SCAN DUPLICATES (SHA256)",
            command=self.start_duplicate_scan,
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        self.dedupe_button.pack(fill="x", padx=20, pady=(0, 8))

        self.mini_button = tk.Button(
            self.sidebar,
            text="TOGGLE MINI PLAYER",
            command=lambda: self.mini_player.toggle(),
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        self.mini_button.pack(fill="x", padx=20, pady=(0, 14))

        self.options_frame = tk.Frame(self.sidebar, bg=SPOTIFY_BLACK)
        self.options_frame.pack(fill="x", padx=20, pady=(0, 8))

        self.auto_delete_duplicates_check = tk.Checkbutton(
            self.options_frame,
            text="Auto delete local duplicates",
            variable=self.auto_delete_duplicates_var,
            bg=SPOTIFY_BLACK,
            fg="white",
            selectcolor=SPOTIFY_PANEL,
            activebackground=SPOTIFY_BLACK,
            activeforeground="white",
        )
        self.auto_delete_duplicates_check.pack(anchor="w")

        self.auto_sync_check = tk.Checkbutton(
            self.options_frame,
            text="Auto sync when USB connects",
            variable=self.auto_sync_var,
            bg=SPOTIFY_BLACK,
            fg="white",
            selectcolor=SPOTIFY_PANEL,
            activebackground=SPOTIFY_BLACK,
            activeforeground="white",
        )
        self.auto_sync_check.pack(anchor="w")

        self.listen_download_check = tk.Checkbutton(
            self.options_frame,
            text="Listen while downloading",
            variable=self.listen_while_download_var,
            bg=SPOTIFY_BLACK,
            fg="white",
            selectcolor=SPOTIFY_PANEL,
            activebackground=SPOTIFY_BLACK,
            activeforeground="white",
        )
        self.listen_download_check.pack(anchor="w")

        self.auto_resume_check = tk.Checkbutton(
            self.options_frame,
            text="Auto resume interrupted downloads",
            variable=self.auto_resume_downloads_var,
            bg=SPOTIFY_BLACK,
            fg="white",
            selectcolor=SPOTIFY_PANEL,
            activebackground=SPOTIFY_BLACK,
            activeforeground="white",
        )
        self.auto_resume_check.pack(anchor="w")

        self.main_content = tk.Frame(self.root, bg=SPOTIFY_BG)
        self.main_content.pack(side="left", fill="both", expand=True)
        self.main_content.pack_propagate(False)

        self.playlist_title = tk.Label(
            self.main_content,
            text="USB Music Library",
            fg="white",
            bg=SPOTIFY_BG,
            font=("Segoe UI", 28, "bold"),
        )
        self.playlist_title.pack(anchor="w", padx=30, pady=(25, 10))

        self.search_row = tk.Frame(self.main_content, bg=SPOTIFY_BG)
        self.search_row.pack(fill="x", padx=25, pady=(0, 10))

        self.library_search_entry = tk.Entry(
            self.search_row,
            bg=SPOTIFY_PANEL,
            fg="white",
            relief="flat",
            font=("Segoe UI", 11),
            insertbackground="white",
            bd=10,
        )
        self.library_search_entry.pack(side="left", fill="x", expand=True, padx=(0, 12))

        self.search_scope_label = tk.Label(
            self.search_row,
            text="Instant SQLite Search",
            fg=SPOTIFY_MUTED,
            bg=SPOTIFY_BG,
            font=("Segoe UI", 10),
        )
        self.search_scope_label.pack(side="left")

        self.songs_frame = tk.Frame(self.main_content, bg=SPOTIFY_BG)
        self.songs_frame.pack(fill="both", expand=True, padx=25, pady=10)

        self.library_frame = tk.Frame(self.songs_frame, bg=SPOTIFY_BG)
        self.library_frame.pack(side="left", fill="both", expand=True, padx=(0, 15))

        self.library_actions = tk.Frame(self.library_frame, bg=SPOTIFY_BG)
        self.library_actions.pack(fill="x", pady=(0, 10))

        self.library_play_button = tk.Button(
            self.library_actions,
            text="PLAY",
            command=self.play_selected_media,
            bg=SPOTIFY_GREEN,
            fg="black",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )
        self.library_play_button.pack(side="left", padx=(0, 8))

        self.add_queue_button = tk.Button(
            self.library_actions,
            text="ADD TO QUEUE",
            command=self.add_selected_to_queue,
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )
        self.add_queue_button.pack(side="left", padx=(0, 8))

        self.favorite_selected_button = tk.Button(
            self.library_actions,
            text="HEART",
            command=self.toggle_selected_favorite,
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )
        self.favorite_selected_button.pack(side="left", padx=(0, 8))

        self.playlist_target_var = tk.StringVar(value="")
        self.playlist_target_menu = ttk.Combobox(
            self.library_actions,
            textvariable=self.playlist_target_var,
            state="readonly",
            width=18,
        )
        self.playlist_target_menu.pack(side="left", padx=(0, 8))

        self.add_playlist_button = tk.Button(
            self.library_actions,
            text="ADD TO PLAYLIST",
            command=self.add_selected_to_playlist,
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )
        self.add_playlist_button.pack(side="left", padx=(0, 8))

        self.refresh_button = tk.Button(
            self.library_actions,
            text="REFRESH",
            command=self.refresh_library_view,
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )
        self.refresh_button.pack(side="left")

        self.usb_listbox = tk.Listbox(
            self.library_frame,
            bg=SPOTIFY_PANEL,
            fg="white",
            relief="flat",
            selectbackground=SPOTIFY_GREEN,
            selectforeground="black",
            font=("Segoe UI", 11),
            bd=0,
        )
        self.usb_listbox.pack(fill="both", expand=True)

        self.side_panel = tk.Frame(self.songs_frame, bg=SPOTIFY_BG, width=420)
        self.side_panel.pack(side="right", fill="y")
        self.side_panel.pack_propagate(False)

        self.side_title = tk.Label(
            self.side_panel,
            text="Smart Panels",
            fg="white",
            bg=SPOTIFY_BG,
            font=("Segoe UI", 15, "bold"),
        )
        self.side_title.pack(anchor="w", pady=(0, 10))

        self.notebook = ttk.Notebook(self.side_panel)
        self.notebook.pack(fill="both", expand=True)

        self.queue_tab = tk.Frame(self.notebook, bg=SPOTIFY_BG)
        self.history_tab = tk.Frame(self.notebook, bg=SPOTIFY_BG)
        self.favorites_tab = tk.Frame(self.notebook, bg=SPOTIFY_BG)
        self.karaoke_tab = tk.Frame(self.notebook, bg=SPOTIFY_BG)
        self.playlists_tab = tk.Frame(self.notebook, bg=SPOTIFY_BG)

        self.notebook.add(self.queue_tab, text="Queue")
        self.notebook.add(self.history_tab, text="History")
        self.notebook.add(self.favorites_tab, text="Favorites")
        self.notebook.add(self.karaoke_tab, text="Karaoke")
        self.notebook.add(self.playlists_tab, text="Playlists")

        self.queue_listbox = tk.Listbox(
            self.queue_tab,
            bg=SPOTIFY_PANEL,
            fg="white",
            relief="flat",
            selectbackground=SPOTIFY_GREEN,
            selectforeground="black",
            font=("Segoe UI", 10),
            bd=0,
        )
        self.queue_listbox.pack(fill="both", expand=True, pady=(0, 10))

        queue_actions = tk.Frame(self.queue_tab, bg=SPOTIFY_BG)
        queue_actions.pack(fill="x")

        tk.Button(
            queue_actions,
            text="PLAY",
            command=self.play_selected_queue_song,
            bg=SPOTIFY_GREEN,
            fg="black",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).grid(row=0, column=0, padx=(0, 6), pady=(0, 6), sticky="ew")

        tk.Button(
            queue_actions,
            text="REMOVE",
            command=self.remove_selected_queue_song,
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).grid(row=0, column=1, padx=6, pady=(0, 6), sticky="ew")

        tk.Button(
            queue_actions,
            text="UP",
            command=lambda: self.move_queue_selection(-1),
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).grid(row=1, column=0, padx=(0, 6), pady=(0, 6), sticky="ew")

        tk.Button(
            queue_actions,
            text="DOWN",
            command=lambda: self.move_queue_selection(1),
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).grid(row=1, column=1, padx=6, pady=(0, 6), sticky="ew")

        tk.Button(
            queue_actions,
            text="SAVE AS PLAYLIST",
            command=self.save_queue_as_playlist,
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).grid(row=2, column=0, padx=(0, 6), pady=(0, 6), sticky="ew")

        tk.Button(
            queue_actions,
            text="CLEAR",
            command=self.clear_queue,
            bg="#7a2020",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).grid(row=2, column=1, padx=6, pady=(0, 6), sticky="ew")

        queue_actions.grid_columnconfigure(0, weight=1)
        queue_actions.grid_columnconfigure(1, weight=1)

        self.history_listbox = tk.Listbox(
            self.history_tab,
            bg=SPOTIFY_PANEL,
            fg="white",
            relief="flat",
            selectbackground=SPOTIFY_GREEN,
            selectforeground="black",
            font=("Segoe UI", 10),
            bd=0,
        )
        self.history_listbox.pack(fill="both", expand=True, pady=(0, 10))

        history_actions = tk.Frame(self.history_tab, bg=SPOTIFY_BG)
        history_actions.pack(fill="x")

        tk.Button(
            history_actions,
            text="PLAY",
            command=self.play_selected_history_song,
            bg=SPOTIFY_GREEN,
            fg="black",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="left", expand=True, fill="x", padx=(0, 6))

        tk.Button(
            history_actions,
            text="CLEAR HISTORY",
            command=self.clear_history,
            bg="#7a2020",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="left", expand=True, fill="x")

        self.favorites_listbox = tk.Listbox(
            self.favorites_tab,
            bg=SPOTIFY_PANEL,
            fg="white",
            relief="flat",
            selectbackground=SPOTIFY_GREEN,
            selectforeground="black",
            font=("Segoe UI", 10),
            bd=0,
        )
        self.favorites_listbox.pack(fill="both", expand=True, pady=(0, 10))

        favorite_actions = tk.Frame(self.favorites_tab, bg=SPOTIFY_BG)
        favorite_actions.pack(fill="x")

        tk.Button(
            favorite_actions,
            text="PLAY",
            command=self.play_selected_favorite_song,
            bg=SPOTIFY_GREEN,
            fg="black",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="left", expand=True, fill="x", padx=(0, 6))

        tk.Button(
            favorite_actions,
            text="TOGGLE HEART",
            command=self.toggle_selected_favorite_from_panel,
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="left", expand=True, fill="x")

        self.karaoke_status_label = tk.Label(
            self.karaoke_tab,
            text="No .lrc loaded",
            fg=SPOTIFY_MUTED,
            bg=SPOTIFY_BG,
            anchor="w",
            font=("Segoe UI", 9),
        )
        self.karaoke_status_label.pack(fill="x", pady=(0, 8))

        self.karaoke_text = tk.Text(
            self.karaoke_tab,
            bg=SPOTIFY_PANEL,
            fg="white",
            relief="flat",
            font=("Segoe UI", 10),
            height=18,
            wrap="word",
            bd=0,
        )
        self.karaoke_text.pack(fill="both", expand=True)
        self.karaoke_text.tag_configure("current", foreground=SPOTIFY_GREEN, font=("Segoe UI", 10, "bold"))
        self.karaoke_text.tag_configure("normal", foreground="white")

        if not hasattr(self, "karaoke_text"):
            self.karaoke_text = None

        playlists_top = tk.Frame(self.playlists_tab, bg=SPOTIFY_BG)
        playlists_top.pack(fill="x", pady=(0, 8))

        self.new_playlist_entry = tk.Entry(
            playlists_top,
            bg=SPOTIFY_PANEL,
            fg="white",
            relief="flat",
            font=("Segoe UI", 9),
            insertbackground="white",
            bd=8,
        )
        self.new_playlist_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        tk.Button(
            playlists_top,
            text="CREATE",
            command=self.create_playlist,
            bg=SPOTIFY_GREEN,
            fg="black",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="left")

        self.playlists_listbox = tk.Listbox(
            self.playlists_tab,
            bg=SPOTIFY_PANEL,
            fg="white",
            relief="flat",
            selectbackground=SPOTIFY_GREEN,
            selectforeground="black",
            font=("Segoe UI", 10),
            bd=0,
            height=8,
        )
        self.playlists_listbox.pack(fill="x", pady=(0, 8))

        self.playlist_songs_listbox = tk.Listbox(
            self.playlists_tab,
            bg=SPOTIFY_PANEL,
            fg="white",
            relief="flat",
            selectbackground=SPOTIFY_GREEN,
            selectforeground="black",
            font=("Segoe UI", 9),
            bd=0,
        )
        self.playlist_songs_listbox.pack(fill="both", expand=True, pady=(0, 8))

        playlist_actions = tk.Frame(self.playlists_tab, bg=SPOTIFY_BG)
        playlist_actions.pack(fill="x")

        tk.Button(
            playlist_actions,
            text="LOAD TO QUEUE",
            command=self.load_selected_playlist_to_queue,
            bg="#2b2b2b",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="left", expand=True, fill="x", padx=(0, 6))

        tk.Button(
            playlist_actions,
            text="PLAY SONG",
            command=self.play_selected_playlist_song,
            bg=SPOTIFY_GREEN,
            fg="black",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="left", expand=True, fill="x")

        self.console = scrolledtext.ScrolledText(
            self.main_content,
            bg="#0d0d0d",
            fg=SPOTIFY_GREEN,
            relief="flat",
            font=("Consolas", 10),
            height=8,
        )
        self.console.pack(fill="x", padx=25, pady=(0, 15))

        self.progress_bar = ttk.Progressbar(self.main_content, mode="indeterminate")
        self.progress_bar.pack(fill="x", padx=25, pady=(0, 12))

        self.stats_label = tk.Label(
            self.main_content,
            text="Songs: 0 | GB: 0 | Minutes saved: 0",
            fg=SPOTIFY_GREEN,
            bg=SPOTIFY_BG,
            font=("Segoe UI", 10, "bold"),
        )
        self.stats_label.pack(anchor="w", padx=30, pady=(0, 10))

        self.status_label = tk.Label(
            self.main_content,
            text="Idle",
            fg=SPOTIFY_GREEN,
            bg=SPOTIFY_BG,
            font=("Segoe UI", 10, "bold"),
        )
        self.status_label.pack(anchor="w", padx=30, pady=(0, 10))

        self.bottom_player = tk.Frame(self.root, bg=SPOTIFY_PANEL, height=120)
        self.bottom_player.place(relx=0, rely=1.0, relwidth=1, anchor="sw", height=120)
        self.bottom_player.lift()
        self.bottom_player.pack_propagate(False)

        self.song_info_frame = tk.Frame(self.bottom_player, bg=SPOTIFY_PANEL)
        self.song_info_frame.pack(side="left", padx=20)

        self.cover_label = tk.Label(
            self.song_info_frame,
            text="♪",
            width=6,
            height=4,
            fg="black",
            bg=SPOTIFY_GREEN,
            font=("Segoe UI", 18, "bold"),
        )
        self.cover_label.pack(side="left", pady=16)

        self.video_panel = tk.Frame(
            self.song_info_frame,
            bg="black",
            width=900,
            height=320,
        )

        self.video_panel.pack(in_=self.song_info_frame, side="left", padx=20, pady=10, fill="both", expand=True)
        self.video_panel.pack_propagate(False)


        self.video_panel = tk.Frame(
            self.song_info_frame,
            bg="black",
            width=420,
            height=240,
        )

        self.video_panel.pack(in_=self.song_info_frame, side="left", padx=20, pady=10, fill="both", expand=True)
        self.video_panel.pack_propagate(False)

        self.video_panel = tk.Frame(
            self.song_info_frame,
            bg="black",
            width=420,
            height=240,
        )
        self.video_panel.pack(in_=self.song_info_frame, side="left", padx=20, pady=10, fill="both", expand=True)
        self.video_panel.pack_propagate(False)

        self.cover_image = None

        self.song_text_frame = tk.Frame(self.song_info_frame, bg=SPOTIFY_PANEL)
        self.song_text_frame.pack(side="left", padx=12)

        self.now_playing_label = tk.Label(
            self.song_text_frame,
            text="No music playing",
            fg="white",
            bg=SPOTIFY_PANEL,
            font=("Segoe UI", 10, "bold"),
        )
        self.now_playing_label.pack(anchor="w")

        self.artist_label = tk.Label(
            self.song_text_frame,
            text="USBIFY Player",
            fg=SPOTIFY_MUTED,
            bg=SPOTIFY_PANEL,
            font=("Segoe UI", 8),
        )
        self.artist_label.pack(anchor="w")

        self.time_label = tk.Label(
            self.song_text_frame,
            text="00:00 / 00:00",
            fg=SPOTIFY_MUTED,
            bg=SPOTIFY_PANEL,
            font=("Segoe UI", 8),
        )
        self.time_label.pack(anchor="w")

        self.favorite_button = tk.Button(
            self.song_info_frame,
            text="♡",
            command=self.toggle_current_favorite,
            bg=SPOTIFY_PANEL,
            fg=SPOTIFY_GREEN,
            relief="flat",
            font=("Segoe UI", 18, "bold"),
            cursor="hand2",
            bd=0,
            activebackground=SPOTIFY_PANEL,
            activeforeground=SPOTIFY_GREEN,
        )
        self.favorite_button.pack(side="left", padx=(5, 0))

        self.center_controls = tk.Frame(self.bottom_player, bg=SPOTIFY_PANEL)
        self.center_controls.pack(side="left", expand=True)

        controls = tk.Frame(self.center_controls, bg=SPOTIFY_PANEL)
        controls.pack(pady=(10, 5))

        button_style = {
            "bg": SPOTIFY_GREEN,
            "fg": "black",
            "relief": "flat",
            "font": ("Segoe UI", 10, "bold"),
            "cursor": "hand2",
            "width": 9,
            "bd": 0,
        }

        tk.Button(controls, text="SHUFFLE", command=self.play_random_song, **button_style).grid(row=0, column=0, padx=5)
        tk.Button(controls, text="PLAY", command=self.play_selected_media, **button_style).grid(row=0, column=1, padx=5)
        tk.Button(controls, text="PAUSE", command=self.pause_song, **button_style).grid(row=0, column=2, padx=5)
        tk.Button(controls, text="NEXT", command=self.next_song, **button_style).grid(row=0, column=3, padx=5)
        tk.Button(controls, text="STOP", command=self.stop_song, **button_style).grid(row=0, column=4, padx=5)

        self.volume_frame = tk.Frame(self.bottom_player, bg=SPOTIFY_PANEL)
        self.volume_frame.pack(side="right", padx=20)

        self.volume_label = tk.Label(
            self.volume_frame,
            text="VOL",
            fg="white",
            bg=SPOTIFY_PANEL,
            font=("Segoe UI", 10, "bold"),
        )
        self.volume_label.pack(side="left")

        self.volume_slider = tk.Scale(
            self.volume_frame,
            from_=0,
            to=100,
            orient="horizontal",
            bg=SPOTIFY_PANEL,
            fg="white",
            troughcolor="#333333",
            highlightthickness=0,
            length=120,
            command=self.set_volume,
        )
        self.volume_slider.set(70)
        self.volume_slider.pack(side="left", padx=10)

        if self.audio_ready:
            pygame.mixer.music.set_volume(0.7)


    def open_video_player(self, video_path):
        if not Path(video_path).exists():
            self.log("[!] Video not found")
            return

        try:
            if self.video_window and self.video_window.winfo_exists():
                self.video_window.destroy()
        except Exception:
            pass

        self.video_window = tk.Toplevel(self.root)
        self.video_window.title("USBIFY VIDEO PLAYER")
        self.video_window.geometry("1000x700")
        self.video_window.configure(bg="black")

        video_frame = tk.Frame(self.video_window, bg="black")
        video_frame.pack(fill="both", expand=True)

        self.video_window.update()

        media = self.video_instance.media_new(str(video_path))
        self.video_player.set_media(media)

        handle = video_frame.winfo_id()

        if os.name == "nt":
            self.video_player.set_hwnd(handle)
        else:
            self.video_player.set_xwindow(handle)

        self.cover_label.pack_forget()
        self.video_panel.pack(in_=self.song_info_frame, side="left", padx=20, pady=10, fill="both", expand=True)

        self.video_player.play()

        controls = tk.Frame(self.video_window, bg="#111111")
        controls.pack(fill="x")

        tk.Button(
            controls,
            text="PLAY",
            command=self.video_player.play,
            bg="#1ed760",
            fg="black",
            relief="flat",
        ).pack(side="left", padx=5, pady=5)

        tk.Button(
            controls,
            text="PAUSE",
            command=self.video_player.pause,
            bg="#333333",
            fg="white",
            relief="flat",
        ).pack(side="left", padx=5, pady=5)

        tk.Button(
            controls,
            text="STOP",
            command=self.video_player.stop,
            bg="#333333",
            fg="white",
            relief="flat",
        ).pack(side="left", padx=5, pady=5)

    def play_selected_media(self):
        item = self.get_selected_library_item()

        if not item:
            return

        path = item["path"]

        if str(path).lower().endswith(".mp4"):
            self.play_video_fullscreen(path)
        else:
            self.play_selected_song()


    def play_video_inside_panel(self, video_path):
        if not hasattr(self, "video_panel"):
            self.video_panel = tk.Frame(
                self.song_info_frame,
                bg="black",
                width=760,
                height=260,
            )
            self.video_panel.pack(in_=self.song_info_frame, side="left", padx=20, pady=10, fill="both", expand=True)
            self.video_panel.pack_propagate(False)

        try:
            self.video_panel.update()

            media = self.video_instance.media_new(str(video_path))
            self.video_player.set_media(media)

            handle = self.video_panel.winfo_id()

            if os.name == "nt":
                self.video_player.set_hwnd(handle)
            else:
                self.video_player.set_xwindow(handle)

            self.video_player.play()

            self.log(f"[+] Playing embedded video: {Path(video_path).name}")

        except Exception as exc:
            self.log(f"[!] Video error: {exc}")



    def open_video_fullscreen(self, video_path):
        try:
            fullscreen_window = tk.Toplevel(self.root)
            fullscreen_window.attributes("-fullscreen", True)
            fullscreen_window.configure(bg="black")

            frame = tk.Frame(fullscreen_window, bg="black")
            frame.pack(fill="both", expand=True)

            fullscreen_window.update()

            media = self.video_instance.media_new(str(video_path))
            player = self.video_instance.media_player_new()
            player.set_media(media)

            handle = frame.winfo_id()

            if os.name == "nt":
                player.set_hwnd(handle)
            else:
                player.set_xwindow(handle)

            player.play()

            fullscreen_window.bind("<Escape>", lambda e: fullscreen_window.destroy())

        except Exception as exc:
            self.log(f"[!] Fullscreen error: {exc}")



    def get_video_library_items(self):
        items = []
        for item in getattr(self, "library_items", []):
            path = str(item.get("path", "")).lower()
            if path.endswith(".mp4"):
                items.append(item)
        return items

    
    def show_videos_only(self):
        try:
            self.usb_listbox.delete(*self.usb_listbox.get_children())

            videos = []

            for item in getattr(self, "library_items", []):
                path = str(item.get("path", "")).lower()

                if path.endswith(".mp4"):
                    videos.append(item)

            self.filtered_video_items = videos

            for idx, item in enumerate(videos, start=1):
                title = item.get("title", Path(item.get("path", "")).stem)
                artist = item.get("artist", "Unknown artist")
                duration = item.get("duration", "00:00")

                self.usb_listbox.insert(
                    "",
                    "end",
                    values=(
                        idx,
                        f"■  {title}  ·  {artist}",
                        "MP4 Video",
                        item.get("date_added", ""),
                        duration,
                    ),
                )

            self.title_label.config(text="Videos")

            self.log(f"[+] Loaded {len(videos)} MP4 videos")

        except Exception as exc:
            self.log(f"[!] Video library error: {exc}")


    
    def play_video_fullscreen(self, video_path):
        try:
            fullscreen = tk.Toplevel(self.root)
            fullscreen.attributes("-fullscreen", True)
            fullscreen.configure(bg="black")

            top_bar = tk.Frame(fullscreen, bg="#111111", height=40)
            top_bar.pack(fill="x", side="top")

            info_frame = tk.Frame(top_bar, bg="#111111")
            info_frame.pack(side="top", pady=5)

            time_label = tk.Label(
                info_frame,
                text="00:00/00:00",
                fg="white",
                bg="#111111",
                font=("Segoe UI", 12, "bold")
            )
            time_label.pack(side="left", padx=(0, 25))

            volume_label = tk.Label(
                info_frame,
                text="VOL 100",
                fg="#00ff66",
                bg="#111111",
                font=("Segoe UI", 11, "bold")
            )
            volume_label.pack(side="left")

            frame = tk.Frame(fullscreen, bg="black")
            frame.pack(fill="both", expand=True)

            fullscreen.update()

            instance = vlc.Instance("--avcodec-hw=none", "--vout=directdraw")
            player = instance.media_player_new()

            media = instance.media_new(str(video_path))
            player.set_media(media)

            handle = frame.winfo_id()

            if os.name == "nt":
                player.set_hwnd(handle)
            else:
                player.set_xwindow(handle)

            player.play()

            saved_position = self.video_positions.get(video_path, 0)

            if saved_position > 0:
                player.set_time(saved_position)

            def update_time():
                try:
                    current_ms = max(player.get_time(), 0)
                    total_ms = max(player.get_length(), 0)

                    self.video_positions[video_path] = current_ms

                    current_sec = current_ms // 1000
                    total_sec = total_ms // 1000

                    current_text = f"{current_sec//60:02}:{current_sec%60:02}"
                    total_text = f"{total_sec//60:02}:{total_sec%60:02}"

                    time_label.config(text=f"{current_text}/{total_text}")

                    fullscreen.after(500, update_time)
                except:
                    pass

            update_time()

            def forward_10(event=None):
                player.set_time(player.get_time() + 10000)

            def back_10(event=None):
                player.set_time(max(player.get_time() - 10000, 0))

            fullscreen.bind("<Right>", forward_10)
            fullscreen.bind("<Left>", back_10)

            def toggle_pause(event=None):
                if player.is_playing():
                    player.pause()
                else:
                    player.play()

            fullscreen.bind("<space>", toggle_pause)

            current_volume = 100
            player.audio_set_volume(current_volume)

            def volume_up(event=None):
                nonlocal current_volume
                current_volume = min(current_volume + 5, 200)
                player.audio_set_volume(current_volume)
                volume_label.config(text=f"VOL {current_volume}")

            def volume_down(event=None):
                nonlocal current_volume
                current_volume = max(current_volume - 5, 0)
                player.audio_set_volume(current_volume)
                volume_label.config(text=f"VOL {current_volume}")

            fullscreen.bind("<Up>", volume_up)
            fullscreen.bind("<Down>", volume_down)

            def close_video(event=None):
                try:
                    self.video_positions[video_path] = player.get_time()
                    player.stop()
                except:
                    pass

                fullscreen.destroy()

            fullscreen.bind("<Escape>", close_video)


        except Exception as exc:
            self.log(f"[!] Fullscreen video error: {exc}")

    def bind_events(self):
        self.usb_listbox.bind("<Double-Button-1>", lambda _: self.play_selected_media())
        self.queue_listbox.bind("<Double-Button-1>", lambda _: self.play_selected_queue_song())
        self.queue_listbox.bind("<Delete>", lambda _: self.remove_selected_queue_song())
        self.history_listbox.bind("<Double-Button-1>", lambda _: self.play_selected_history_song())
        self.favorites_listbox.bind("<Double-Button-1>", lambda _: self.play_selected_favorite_song())
        self.playlists_listbox.bind("<<ListboxSelect>>", lambda _: self.refresh_selected_playlist_songs())
        self.playlist_songs_listbox.bind("<Double-Button-1>", lambda _: self.play_selected_playlist_song())
        self.library_search_entry.bind("<KeyRelease>", self.schedule_search_refresh)

    def ui_call(self, callback):
        self.root.after(0, callback)

    def log(self, text):
        self.console.insert(tk.END, text + "\n")
        self.console.see(tk.END)

    def set_status(self, text):
        self.status_label.config(text=text)

    def set_status_async(self, text):
        self.ui_call(lambda: self.set_status(text))

    def set_progress_indeterminate(self):
        self.progress_bar.config(mode="indeterminate")
        self.progress_bar.start(10)

    def set_progress_determinate(self, maximum):
        self.progress_bar.stop()
        self.progress_bar.config(mode="determinate", maximum=max(1, maximum), value=0)

    def set_progress_value(self, value):
        self.progress_bar["value"] = value

    def stop_progress(self):
        self.progress_bar.stop()
        self.progress_bar.config(mode="indeterminate", value=0)

    def toggle_fullscreen(self, event=None):
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)

    def exit_fullscreen(self, event=None):
        self.fullscreen = False
        self.root.attributes("-fullscreen", False)

    def notify_windows(self, title, message, image_path=None):
        if getattr(self, "gaming_mode_var", None) and self.gaming_mode_var.get():
            return
        if os.name != "nt":
            return

        threading.Thread(
            target=self._notify_windows_worker,
            args=(title, message, image_path),
            daemon=True,
        ).start()

    def _notify_windows_worker(self, title, message, image_path=None):
        try:
            escaped_title = escape(title)
            escaped_message = escape(message)
            image_xml = ""
            candidate = image_path or get_icon_path()

            if candidate:
                candidate_path = Path(candidate).resolve().as_uri()
                image_xml = (
                    f'<image placement="appLogoOverride" hint-crop="circle" src="{candidate_path}"/>'
                )

            script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@"
<toast>
  <visual>
    <binding template="ToastGeneric">
      {image_xml}
      <text>{escaped_title}</text>
      <text>{escaped_message}</text>
    </binding>
  </visual>
</toast>
"@)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("{APP_ID}").Show($toast)
"""
            encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
            subprocess.Popen(
                ["powershell", "-NoProfile", "-EncodedCommand", encoded],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=get_startupinfo(),
            )
        except Exception:
            pass

    def start_background_services(self):
        threading.Thread(target=self.usb_monitor_loop, daemon=True).start()
        self.start_library_scan(startup=True)

        if self.auto_resume_downloads_var.get():
            self.root.after(2500, self.resume_pending_downloads)

    def usb_monitor_loop(self):
        last_drive = self.current_usb_drive

        while not self.shutdown_event.is_set():
            current_drive = get_usb_drive()
            if current_drive != last_drive:
                last_drive = current_drive
                self.current_usb_drive = current_drive
                if current_drive:
                    self.ui_call(lambda drive=current_drive: self.on_usb_connected(drive))
                else:
                    self.ui_call(self.on_usb_disconnected)
            time.sleep(USB_MONITOR_INTERVAL)

    def on_usb_connected(self, drive):
        self.log(f"[+] USB connected: {drive}")
        self.set_status(f"USB detected: {drive}")
        self.notify_windows("USBIFY", f"USB detected on {drive}")
        self.start_library_scan(startup=False)
        self.refresh_library_view()
        if self.auto_sync_var.get():
            self.start_usb_sync()

    def on_usb_disconnected(self):
        self.log("[*] USB disconnected")
        self.set_status("USB disconnected")
        self.refresh_library_view()

    def get_usb_music_folder(self):
        if not self.current_usb_drive:
            return None

        music_folder = Path(self.current_usb_drive) / "MusicUSB" / "Music"
        music_folder.mkdir(parents=True, exist_ok=True)
        return music_folder

    def get_scan_targets(self):
        targets = [DOWNLOAD_FOLDER]
        home_music = Path.home() / "Music"
        if home_music.exists():
            targets.append(home_music)

        usb_music = self.get_usb_music_folder()
        if usb_music:
            targets.append(usb_music)

        unique_targets = []
        seen = set()
        for path in targets:
            resolved = str(path.resolve())
            if resolved not in seen:
                unique_targets.append(path)
                seen.add(resolved)
        return unique_targets

    def start_library_scan(self, startup=False):
        if self.scan_in_progress:
            return

        self.scan_in_progress = True
        threading.Thread(
            target=self.scan_library_worker,
            args=(startup,),
            daemon=True,
        ).start()

    def scan_library_worker(self, startup=False):
        try:
            self.ui_call(self.set_progress_indeterminate)
            self.set_status_async("Scanning SQLite music library...")
            targets = self.get_scan_targets()
            scanned_count = 0

            for folder in targets:
                if not folder.exists():
                    continue

                source_label = "usb" if self.current_usb_drive and is_path_under(folder, Path(self.current_usb_drive)) else (
                    "downloads" if is_path_under(folder, DOWNLOAD_FOLDER) else "library"
                )

                self.ui_call(lambda path=str(folder): self.log(f"[*] Scanning: {path}"))

                valid_paths = []
                for path in folder.rglob("*"):
                    if self.shutdown_event.is_set():
                        return

                    if not supported_file(path):
                        continue

                    metadata = extract_metadata(path)
                    existing = self.db.get_song(path)

                    if existing and existing["mtime"] == metadata["mtime"] and existing["file_size"] == metadata["file_size"] and existing["sha256"]:
                        sha256 = existing["sha256"]
                    else:
                        sha256 = compute_sha256(path)

                    metadata["sha256"] = sha256
                    metadata["source"] = source_label
                    metadata["usb_drive"] = self.current_usb_drive or ""
                    metadata["last_scanned"] = now_iso()
                    self.db.upsert_song(metadata)

                    valid_paths.append(str(path.resolve()))
                    scanned_count += 1

                if valid_paths:
                    self.db.mark_missing_outside(valid_paths, source=source_label)

            self.ui_call(lambda: self.log(f"[+] SQLite library scanned: {scanned_count} file(s)"))
            self.set_status_async("Library scan complete")
            self.ui_call(self.refresh_library_view)
            self.ui_call(self.refresh_history_ui)
            self.ui_call(self.refresh_favorites_ui)
            self.ui_call(self.refresh_playlists_ui)
        except Exception as exc:
            self.ui_call(lambda: self.log(f"[!] Library scan error: {exc}"))
            self.set_status_async("Library scan failed")
        finally:
            self.scan_in_progress = False
            self.ui_call(self.stop_progress)

    def search_online_music(self):
        query = self.online_search_entry.get().strip()
        if not query:
            return

        self.url_entry.delete(0, tk.END)
        self.url_entry.insert(0, f"ytsearch1:{query}")
        self.log(f"[+] Search prepared: {query}")

    def schedule_search_refresh(self, event=None):
        if self.search_job is not None:
            self.root.after_cancel(self.search_job)
        self.search_job = self.root.after(250, self.refresh_library_view)

    def refresh_library_view(self):
        query = self.library_search_entry.get().strip()

        if query:
            rows = self.db.search_songs(query=query, usb_only=False)
        elif self.current_usb_drive:
            rows = self.db.search_songs(query="", usb_only=True, usb_drive=self.current_usb_drive)
        else:
            rows = self.db.list_recent_songs()

        self.current_library_items = rows
        self.usb_listbox.delete(0, tk.END)

        for row in rows:
            display = self.format_song_display(row)
            self.usb_listbox.insert(tk.END, display)

    def format_song_display(self, row):
        title = row.get("title") or Path(row["path"]).stem
        artist = row.get("artist") or "Unknown artist"
        genre = row.get("genre") or "Unknown"
        return f"{artist} - {title} [{genre}]"

    def get_selected_library_item(self):
        selection = self.usb_listbox.curselection()
        if not selection:
            return None
        return self.current_library_items[selection[0]]

    def resolve_song_path_from_name(self, song_name):
        rows = self.db.search_songs(query=Path(song_name).stem, usb_only=False, limit=50)
        normalized_target = normalize_media_name(song_name)

        for row in rows:
            if normalize_media_name(Path(row["path"]).name) == normalized_target:
                return Path(row["path"])

        usb_music = self.get_usb_music_folder()
        if usb_music:
            for path in usb_music.glob("*"):
                if normalize_media_name(path.name) == normalized_target:
                    return path

        return None

    def get_metadata(self, song_path):
        song_path = Path(song_path).resolve()
        row = self.db.get_song(song_path)
        if row:
            if float(row.get("duration") or 0) > 0:
                return row

            refreshed = extract_metadata(song_path)
            refreshed["sha256"] = row.get("sha256") or compute_sha256(song_path)
            refreshed["source"] = row.get("source") or "runtime"
            refreshed["usb_drive"] = row.get("usb_drive") or self.current_usb_drive or ""
            refreshed["last_scanned"] = now_iso()
            self.db.upsert_song(refreshed)
            updated = self.db.get_song(song_path)
            if updated:
                return updated
            return row

        metadata = extract_metadata(song_path)
        metadata["sha256"] = compute_sha256(song_path)
        metadata["source"] = "runtime"
        metadata["usb_drive"] = self.current_usb_drive or ""
        metadata["last_scanned"] = now_iso()
        self.db.upsert_song(metadata)
        return self.db.get_song(song_path)

    def load_queue_from_db(self):
        self.current_queue_paths = self.db.load_queue()
        self.refresh_queue_ui()

    def refresh_queue_ui(self):
        self.queue_listbox.delete(0, tk.END)
        for path in self.current_queue_paths:
            metadata = self.get_metadata(path)
            self.queue_listbox.insert(tk.END, self.format_song_display(metadata))

    def refresh_history_ui(self):
        self.current_history_items = self.db.list_history()
        self.history_listbox.delete(0, tk.END)
        for item in self.current_history_items:
            label = f"{item.get('artist') or 'Unknown artist'} - {item.get('title') or Path(item['path']).stem} | {item['played_at']}"
            self.history_listbox.insert(tk.END, label)

    def refresh_favorites_ui(self):
        self.current_favorite_items = self.db.list_favorites()
        self.favorites_listbox.delete(0, tk.END)
        for item in self.current_favorite_items:
            self.favorites_listbox.insert(tk.END, self.format_song_display(item))

    def refresh_playlists_ui(self):
        playlists = self.db.list_playlists()
        self.playlists_listbox.delete(0, tk.END)
        names = []
        for playlist in playlists:
            self.playlists_listbox.insert(tk.END, playlist["name"])
            names.append(playlist["name"])
        self.playlist_target_menu["values"] = names
        if names and not self.playlist_target_var.get():
            self.playlist_target_var.set(names[0])

    def get_selected_queue_path(self):
        selection = self.queue_listbox.curselection()
        if not selection:
            return None, None
        index = selection[0]
        return index, self.current_queue_paths[index]

    def add_selected_to_queue(self):
        item = self.get_selected_library_item()
        if not item:
            self.log("[!] No song selected to queue")
            return

        self.current_queue_paths.append(item["path"])
        self.db.save_queue(self.current_queue_paths)
        self.refresh_queue_ui()
        self.log(f"[+] Added to queue: {self.format_song_display(item)}")

    def play_selected_queue_song(self):
        index, path = self.get_selected_queue_path()
        if path is None:
            self.log("[!] No queued song selected")
            return

        self.current_queue_paths.pop(index)
        self.db.save_queue(self.current_queue_paths)
        self.refresh_queue_ui()
        self.play_song_path(path, source="queue")

    def remove_selected_queue_song(self):
        index, path = self.get_selected_queue_path()
        if path is None:
            return

        metadata = self.get_metadata(path)
        self.current_queue_paths.pop(index)
        self.db.save_queue(self.current_queue_paths)
        self.refresh_queue_ui()
        self.log(f"[-] Removed from queue: {self.format_song_display(metadata)}")

    def move_queue_selection(self, direction):
        index, path = self.get_selected_queue_path()
        if path is None:
            return

        new_index = index + direction
        if new_index < 0 or new_index >= len(self.current_queue_paths):
            return

        self.current_queue_paths[index], self.current_queue_paths[new_index] = (
            self.current_queue_paths[new_index],
            self.current_queue_paths[index],
        )
        self.db.save_queue(self.current_queue_paths)
        self.refresh_queue_ui()
        self.queue_listbox.selection_set(new_index)

    def clear_queue(self):
        self.current_queue_paths = []
        self.db.save_queue(self.current_queue_paths)
        self.refresh_queue_ui()
        self.log("[*] Queue cleared")

    def sync_favorite_button(self):
        if self.current_song_path and self.db.is_favorite(self.current_song_path):
            self.favorite_button.config(text="♥")
        else:
            self.favorite_button.config(text="♡")

    def toggle_favorite_for_path(self, path):
        path = str(Path(path).resolve())
        enabled = not self.db.is_favorite(path)
        self.db.set_favorite(path, enabled)
        metadata = self.get_metadata(path)
        action = "Added favorite" if enabled else "Removed favorite"
        self.log(f"[{'+' if enabled else '-'}] {action}: {self.format_song_display(metadata)}")
        self.refresh_favorites_ui()
        self.sync_favorite_button()

    def toggle_selected_favorite(self):
        item = self.get_selected_library_item()
        if not item:
            self.log("[!] No selected song to favorite")
            return
        self.toggle_favorite_for_path(item["path"])

    def toggle_current_favorite(self):
        if self.current_song_path:
            self.toggle_favorite_for_path(self.current_song_path)
            return

        item = self.get_selected_library_item()
        if item:
            self.toggle_favorite_for_path(item["path"])
            return

        self.log("[!] No song available to favorite")

    def toggle_selected_favorite_from_panel(self):
        selection = self.favorites_listbox.curselection()
        if not selection:
            return
        item = self.current_favorite_items[selection[0]]
        self.toggle_favorite_for_path(item["path"])

    def play_selected_song(self):
        item = self.get_selected_library_item()
        if not item:
            self.log("[!] No song selected")
            return
        self.play_song_path(item["path"], source="library")

    def play_selected_history_song(self):
        selection = self.history_listbox.curselection()
        if not selection:
            self.log("[!] No history item selected")
            return
        item = self.current_history_items[selection[0]]
        self.play_song_path(item["path"], source="history")

    def play_selected_favorite_song(self):
        selection = self.favorites_listbox.curselection()
        if not selection:
            self.log("[!] No favorite selected")
            return
        item = self.current_favorite_items[selection[0]]
        self.play_song_path(item["path"], source="favorites")

    def clear_history(self):
        self.db.clear_history()
        self.refresh_history_ui()
        self.log("[*] History cleared")

    def create_playlist(self):
        name = self.new_playlist_entry.get().strip()
        if not name:
            return
        self.db.create_playlist(name)
        self.new_playlist_entry.delete(0, tk.END)
        self.refresh_playlists_ui()
        self.log(f"[+] Playlist created: {name}")

    def get_playlist_by_name(self, name):
        for playlist in self.db.list_playlists():
            if playlist["name"] == name:
                return playlist
        return None

    def add_selected_to_playlist(self):
        item = self.get_selected_library_item()
        if not item:
            self.log("[!] No song selected for playlist")
            return

        playlist_name = self.playlist_target_var.get().strip()
        if not playlist_name:
            self.log("[!] No playlist selected")
            return

        playlist = self.get_playlist_by_name(playlist_name)
        if not playlist:
            self.log("[!] Playlist not found")
            return

        self.db.add_song_to_playlist(playlist["id"], item["path"])
        self.refresh_selected_playlist_songs()
        self.log(f"[+] Added to playlist {playlist_name}: {self.format_song_display(item)}")

    def save_queue_as_playlist(self):
        if not self.current_queue_paths:
            self.log("[!] Queue is empty")
            return

        name = self.new_playlist_entry.get().strip() or f"Queue {datetime.now().strftime('%H-%M-%S')}"
        self.db.create_playlist(name)
        playlist = self.get_playlist_by_name(name)
        if playlist:
            self.db.replace_playlist_with_queue(playlist["id"], self.current_queue_paths)
            self.refresh_playlists_ui()
            self.playlist_target_var.set(name)
            self.log(f"[+] Queue saved as playlist: {name}")

    def refresh_selected_playlist_songs(self):
        selection = self.playlists_listbox.curselection()
        self.playlist_songs_listbox.delete(0, tk.END)
        self.current_playlist_items = []

        if not selection:
            return

        name = self.playlists_listbox.get(selection[0])
        playlist = self.get_playlist_by_name(name)
        if not playlist:
            return

        self.current_playlist_items = self.db.list_playlist_songs(playlist["id"])
        for item in self.current_playlist_items:
            self.playlist_songs_listbox.insert(tk.END, self.format_song_display(item))

    def load_selected_playlist_to_queue(self):
        selection = self.playlists_listbox.curselection()
        if not selection:
            return

        name = self.playlists_listbox.get(selection[0])
        playlist = self.get_playlist_by_name(name)
        if not playlist:
            return

        songs = self.db.list_playlist_songs(playlist["id"])
        self.current_queue_paths = [song["path"] for song in songs]
        self.db.save_queue(self.current_queue_paths)
        self.refresh_queue_ui()
        self.log(f"[+] Playlist loaded into queue: {name}")

    def play_selected_playlist_song(self):
        selection = self.playlist_songs_listbox.curselection()
        if not selection:
            return
        item = self.current_playlist_items[selection[0]]
        self.play_song_path(item["path"], source="playlist")

    def set_volume(self, value):
        if not self.audio_ready:
            return
        pygame.mixer.music.set_volume(float(value) / 100)

    def update_cover_display(self):
        self.cover_image = None
        cover_path = self.current_cover_path

        if cover_path and Path(cover_path).suffix.lower() == ".png":
            try:
                self.cover_image = tk.PhotoImage(file=str(cover_path))
                self.cover_label.config(image=self.cover_image, text="")
                return
            except Exception:
                self.cover_image = None

        self.cover_label.config(image="", text="♪")

    def load_karaoke_for_song(self, song_path):
        song_path = Path(song_path)
        candidates = [
            song_path.with_suffix(".lrc"),
            DOWNLOAD_FOLDER / f"{song_path.stem}.lrc",
        ]

        lrc_entries = []
        lrc_path = None
        for candidate in candidates:
            if candidate.exists():
                lrc_entries = parse_lrc_file(candidate)
                lrc_path = candidate
                if lrc_entries:
                    break

        self.current_lyrics = lrc_entries
        self.current_lyric_index = -1
        self.karaoke_text.config(state="normal")
        self.karaoke_text.delete("1.0", tk.END)

        if not lrc_entries:
            self.karaoke_status_label.config(text="No .lrc loaded")
            self.karaoke_text.insert(tk.END, "No synchronized lyrics found for this song.")
            self.karaoke_text.config(state="disabled")
            return

        self.karaoke_status_label.config(text=f"Karaoke: {lrc_path.name}")

        for _, line in lrc_entries:
            self.karaoke_text.insert(tk.END, line + "\n", "normal")

        self.karaoke_text.config(state="disabled")

    def update_karaoke(self, seconds):
        if not self.current_lyrics:
            return

        new_index = -1
        for index, (timestamp, _) in enumerate(self.current_lyrics):
            if seconds >= timestamp:
                new_index = index
            else:
                break

        if new_index == self.current_lyric_index or new_index < 0:
            return

        self.current_lyric_index = new_index
        self.karaoke_text.config(state="normal")
        self.karaoke_text.tag_remove("current", "1.0", tk.END)
        self.karaoke_text.tag_add("current", f"{new_index + 1}.0", f"{new_index + 1}.end")
        self.karaoke_text.see(f"{max(1, new_index)}.0")
        self.karaoke_text.config(state="disabled")

    def play_song_path(self, song_path, source="library"):
        if not self.audio_ready:
            self.log("[!] Audio player is not available")
            return

        song_path = Path(song_path)
        if not song_path.exists():
            self.log(f"[!] File not found: {song_path}")
            return

        try:
            pygame.mixer.music.stop()
            pygame.mixer.music.load(str(song_path))
            pygame.mixer.music.play()

            self.stop_requested = False
            self.paused = False
            self.current_song_path = str(song_path.resolve())
            self.current_song = song_path.name
            self.current_metadata = self.get_metadata(song_path)
            self.current_cover_path = extract_cover_art(song_path)
            self.song_length = float(self.current_metadata.get("duration") or 0.0)
            self.last_progress_position = 0.0
            self.last_artist_played = self.current_metadata.get("artist", "")

            self.now_playing_label.config(text=self.current_metadata.get("title", self.current_song))
            self.artist_label.config(text=self.current_metadata.get("artist", "USBIFY Player"))
            self.time_label.config(text=f"00:00 / {format_seconds(self.song_length)}")
            self.update_cover_display()
            self.load_karaoke_for_song(song_path)
            self.db.record_history(self.current_metadata)
            self.refresh_history_ui()
            self.sync_favorite_button()
            self.schedule_progress_update()
            self.mini_player.update()

            self.log(f"[+] Playing: {self.format_song_display(self.current_metadata)}")
            self.notify_windows(
                self.current_metadata.get("title", "Now playing")[:NOTIFICATION_SONG_LIMIT],
                self.current_metadata.get("artist", "USBIFY Player"),
                self.current_cover_path,
            )
        except Exception as exc:
            self.log(f"[!] Playback error: {exc}")

    def pause_song(self):
        if not self.audio_ready:
            return

        try:
            if self.paused:
                pygame.mixer.music.unpause()
                self.paused = False
                self.playback_started_at = time.monotonic()
                self.log("[*] Music resumed")
            else:
                self.playback_base_position = self.get_current_playback_position()
                self.last_progress_position = self.playback_base_position
                self.playback_started_at = None
                pygame.mixer.music.pause()
                self.paused = True
                self.log("[*] Music paused")
            self.schedule_progress_update()
            self.mini_player.update()
        except Exception as exc:
            self.log(f"[!] Pause error: {exc}")

    def stop_song(self):
        if not self.audio_ready:
            return

        try:
            self.stop_requested = True
            self.paused = False
            pygame.mixer.music.stop()
            if self.progress_job is not None:
                self.root.after_cancel(self.progress_job)
                self.progress_job = None

            self.current_song_path = None
            self.current_song = None
            self.current_metadata = None
            self.current_cover_path = None
            self.song_length = 0.0
            self.last_progress_position = 0.0
            self.playback_base_position = 0.0
            self.playback_started_at = None
            self.current_lyrics = []
            self.current_lyric_index = -1

            self.now_playing_label.config(text="No music playing")
            self.artist_label.config(text="USBIFY Player")
            self.time_label.config(text="00:00 / 00:00")
            self.karaoke_status_label.config(text="No .lrc loaded")
            self.karaoke_text.config(state="normal")
            self.karaoke_text.delete("1.0", tk.END)
            self.karaoke_text.insert(tk.END, "No synchronized lyrics found for this song.")
            self.karaoke_text.config(state="disabled")
            self.update_cover_display()
            self.sync_favorite_button()
            self.mini_player.update()
            self.log("[*] Music stopped")
        except Exception as exc:
            self.log(f"[!] Stop error: {exc}")

    def schedule_progress_update(self):
        if self.progress_job is not None:
            self.root.after_cancel(self.progress_job)
        self.progress_job = self.root.after(500, self.update_music_progress)

    def update_music_progress(self):
        self.progress_job = None

        if not self.current_song_path:
            self.time_label.config(text="00:00 / 00:00")
            self.mini_player.update()
            return

        try:
            if self.paused:
                current = self.last_progress_position
            elif pygame.mixer.music.get_busy():
                current = self.get_current_playback_position()
                self.last_progress_position = current
            else:
                if not self.stop_requested:
                    self.next_song(auto_advance=True)
                return

            self.time_label.config(
                text=f"{format_seconds(current)} / {format_seconds(self.song_length)}"
            )
            self.update_karaoke(current)
            self.mini_player.update()
            self.progress_job = self.root.after(500, self.update_music_progress)
        except Exception as exc:
            self.log(f"[!] Progress error: {exc}")

    def build_smart_shuffle_order(self, rows):
        history = self.db.list_history(limit=MAX_RECENT_SONGS)
        recent_paths = [item["path"] for item in history]
        recent_artists = [item.get("artist", "") for item in history[:5]]
        play_counts = Counter(item["path"] for item in self.db.list_history(limit=MAX_HISTORY_ITEMS))
        genre_counts = Counter(item.get("genre", "Unknown") for item in rows)
        weighted = []

        for row in rows:
            path = row["path"]
            artist = row.get("artist", "")
            genre = row.get("genre", "Unknown")
            favorite_bonus = 12 if self.db.is_favorite(path) else 0
            recent_penalty = 30 if path in recent_paths else 0
            artist_penalty = 22 if artist and artist == self.last_artist_played else 0
            sequence_penalty = 16 if artist and artist in recent_artists else 0
            play_penalty = play_counts[path] * 3
            genre_bonus = max(0, 8 - genre_counts[genre])
            novelty_bonus = 10 if path not in play_counts else 0
            score = 100 + favorite_bonus + genre_bonus + novelty_bonus + random.uniform(0, 12)
            score -= recent_penalty + artist_penalty + sequence_penalty + play_penalty
            weighted.append((score, random.random(), row))

        weighted.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in weighted]

    def play_random_song(self):
        if self.current_queue_paths:
            next_path = self.current_queue_paths.pop(0)
            self.db.save_queue(self.current_queue_paths)
            self.refresh_queue_ui()
            self.play_song_path(next_path, source="queue")
            return

        if self.current_usb_drive:
            rows = self.db.search_songs(query="", usb_only=True, usb_drive=self.current_usb_drive)
        else:
            rows = self.db.list_recent_songs()

        if not rows:
            self.log("[!] No songs available")
            return

        if not self.playlist_order:
            self.playlist_order = self.build_smart_shuffle_order(rows)
            self.current_index = 0
        else:
            self.current_index += 1
            if self.current_index >= len(self.playlist_order):
                self.playlist_order = self.build_smart_shuffle_order(rows)
                self.current_index = 0

        next_item = self.playlist_order[self.current_index]
        self.play_song_path(next_item["path"], source="smart-shuffle")

    def next_song(self, auto_advance=False):
        if auto_advance:
            self.log("[*] Auto advancing to next song")
        self.play_random_song()

    def build_download_command(self, url):
        output_template = str(DOWNLOAD_FOLDER / "%(title)s.%(ext)s")
        ffmpeg_location = find_ffmpeg_location()
        command = [
            "yt-dlp",
            "--newline",
            "--continue",
            "--part",
            "--no-overwrites",
            "--windows-filenames",
            "--js-runtimes",
            "node",
            "--remote-components",
            "ejs:github",
            "-o",
            output_template,
        ]
        if ffmpeg_location:
            command.extend(["--ffmpeg-location", ffmpeg_location])

        if self.mode_var.get() == "audio":
            command.extend(["-x", "--audio-format", "mp3"])
        else:
            command.extend(
                [
                    "-f",
                    VIDEO_FORMATS.get(self.quality_var.get(), VIDEO_FORMATS["Best"]),
                    "--merge-output-format",
                    "mp4",
                ]
            )

        command.append(url)
        return command

    def start_download(self):
        if self.download_in_progress:
            self.log("[!] A download is already running")
            return

        url = self.url_entry.get().strip()
        if not validate_url(url):
            messagebox.showerror("Error", "Invalid URL")
            return

        download_id = self.db.create_download(url, self.mode_var.get(), self.quality_var.get())
        self.download_in_progress = True
        threading.Thread(
            target=self.download_worker,
            args=(download_id, url, self.mode_var.get(), self.quality_var.get(), False),
            daemon=True,
        ).start()

    def resume_last_download(self):
        if self.download_in_progress:
            self.log("[!] A download is already running")
            return

        download = self.db.get_last_download()
        if not download:
            messagebox.showinfo("Resume", "No previous download found")
            return

        self.download_in_progress = True
        self.url_entry.delete(0, tk.END)
        self.url_entry.insert(0, download["url"])
        self.mode_var.set(download["mode"])
        self.quality_var.set(download["quality"])

        threading.Thread(
            target=self.download_worker,
            args=(download["id"], download["url"], download["mode"], download["quality"], True),
            daemon=True,
        ).start()

    def resume_pending_downloads(self):
        if self.download_in_progress:
            return

        pending_downloads = self.db.get_pending_downloads()
        if not pending_downloads:
            return

        download = pending_downloads[0]
        self.log(f"[*] Auto-resuming download: {download['url']}")
        self.download_in_progress = True
        threading.Thread(
            target=self.download_worker,
            args=(download["id"], download["url"], download["mode"], download["quality"], True),
            daemon=True,
        ).start()

    def download_worker(self, download_id, url, mode, quality, resumed):
        preview_started = False

        try:
            self.ui_call(self.set_progress_indeterminate)
            self.set_status_async("Resuming download..." if resumed else "Preparing download...")
            if not find_ffmpeg_location():
                message = (
                    "FFmpeg is required to convert audio/video. "
                    "Install FFmpeg or place ffmpeg.exe next to music_usb.py / the app exe."
                )
                self.db.update_download(download_id, status="failed", last_error=message)
                self.ui_call(lambda: self.log(f"[!] {message}"))
                self.ui_call(lambda: messagebox.showerror("FFmpeg missing", message))
                self.set_status_async("FFmpeg missing")
                return

            for attempt in range(1, DOWNLOAD_RETRY_LIMIT + 1):
                fatal_download_error = None
                self.db.update_download(
                    download_id,
                    status="retrying" if attempt > 1 else "pending",
                    retries=attempt - 1,
                    last_error="",
                )

                command = self.build_download_command(url)
                self.current_download_process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    startupinfo=get_startupinfo(),
                )

                self.ui_call(lambda: self.log(f"[*] Download attempt {attempt}/{DOWNLOAD_RETRY_LIMIT}"))

                for raw_line in self.current_download_process.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue

                    self.ui_call(lambda text=line: self.log(text))
                    line_lower = line.lower()
                    if "ffmpeg" in line_lower and "not found" in line_lower:
                        fatal_download_error = "FFmpeg was not found. Install FFmpeg or put ffmpeg.exe in the app folder."

                    percent_match = re.search(r"(\d+(?:\.\d+)?)%", line)
                    if percent_match:
                        percent_value = float(percent_match.group(1))
                        self.db.update_download(download_id, progress=percent_value, last_output=line)
                        self.set_status_async(f"Downloading... {percent_value:.1f}%")

                    destination_match = re.search(r"Destination:\s(.+)$", line)
                    if destination_match:
                        destination = destination_match.group(1).strip()
                        self.db.update_download(download_id, last_output=destination)

                    if self.listen_while_download_var.get() and not preview_started and mode == "audio":
                        preview_started = True
                        self.start_preview_listener(download_id)

                self.current_download_process.wait()

                if self.current_download_process.returncode == 0:
                    self.db.update_download(download_id, status="completed", progress=100)
                    self.set_status_async("Download complete")
                    self.ui_call(lambda: self.log("[+] Download complete"))
                    self.ui_call(self.stop_progress)
                    self.start_library_scan(startup=False)
                    self.start_usb_sync(triggered_by_download=True)
                    self.notify_windows("USBIFY", "Download complete")
                    return

                if fatal_download_error:
                    self.db.update_download(download_id, status="failed", last_error=fatal_download_error)
                    self.ui_call(lambda: self.log(f"[!] {fatal_download_error}"))
                    self.ui_call(lambda: messagebox.showerror("FFmpeg missing", fatal_download_error))
                    self.set_status_async("FFmpeg missing")
                    return

                self.db.update_download(
                    download_id,
                    status="failed",
                    last_error=f"Attempt {attempt} failed",
                )

                if attempt < DOWNLOAD_RETRY_LIMIT:
                    self.set_status_async(f"Retrying download... ({attempt}/{DOWNLOAD_RETRY_LIMIT})")
                    self.ui_call(lambda: self.log("[*] Network or download error. Retrying automatically..."))
                    time.sleep(min(6, attempt * 2))
                else:
                    self.ui_call(lambda: messagebox.showerror("Error", "Download failed after retries"))
                    self.notify_windows("USBIFY", "Download failed after retries")
                    self.set_status_async("Download failed")
        except Exception as exc:
            self.db.update_download(download_id, status="failed", last_error=str(exc))
            self.ui_call(lambda: self.log(f"[!] Download error: {exc}"))
            self.set_status_async("Download error")
        finally:
            self.download_in_progress = False
            self.current_download_process = None
            self.ui_call(self.stop_progress)

    def find_preview_candidate(self):
        partials = sorted(DOWNLOAD_FOLDER.glob("*.part"), key=lambda item: item.stat().st_mtime, reverse=True)
        for path in partials:
            lower_name = path.name.lower()
            for extension in (".mp3.part", ".ogg.part", ".wav.part"):
                if lower_name.endswith(extension):
                    return path, extension.replace(".part", "")
        return None, None

    def start_preview_listener(self, download_id):
        def worker():
            start_time = time.time()
            while time.time() - start_time < 90 and not self.shutdown_event.is_set():
                preview_source, real_extension = self.find_preview_candidate()
                if preview_source and preview_source.exists() and preview_source.stat().st_size > 2 * 1024 * 1024:
                    preview_target = PREVIEW_CACHE_FOLDER / f"preview_buffer{real_extension}"
                    try:
                        shutil.copy2(preview_source, preview_target)
                        if not self.current_song_path:
                            self.ui_call(lambda path=str(preview_target): self.play_song_path(path, source="preview-buffer"))
                            self.ui_call(lambda: self.log("[*] Listening while downloading from preview buffer"))
                        return
                    except Exception:
                        pass
                time.sleep(3)

        self.current_preview_thread = threading.Thread(target=worker, daemon=True)
        self.current_preview_thread.start()

    def get_sync_candidates(self):
        candidates = []
        for path in DOWNLOAD_FOLDER.rglob("*"):
            if supported_file(path):
                candidates.append(path)
        return sorted(candidates, key=lambda item: item.name.casefold())

    def start_usb_sync(self, triggered_by_download=False):
        if self.sync_in_progress:
            return

        if not self.current_usb_drive:
            if not triggered_by_download:
                self.log("[!] No USB detected for sync")
            return

        self.sync_in_progress = True
        threading.Thread(
            target=self.usb_sync_worker,
            args=(triggered_by_download,),
            daemon=True,
        ).start()

    def usb_sync_worker(self, triggered_by_download=False):
        try:
            usb_music = self.get_usb_music_folder()
            if not usb_music:
                return

            candidates = self.get_sync_candidates()
            if not candidates:
                self.ui_call(lambda: self.log("[*] No local songs available to sync"))
                return

            self.ui_call(lambda: self.set_progress_determinate(len(candidates)))
            self.set_status_async("Syncing USB...")
            self.ui_call(lambda: self.log(f"[*] Starting USB sync to {usb_music}"))

            existing_usb_files = {}
            for file_path in usb_music.glob("*"):
                if supported_file(file_path):
                    try:
                        existing_usb_files[compute_sha256(file_path)] = file_path
                    except Exception:
                        continue

            copied = 0
            skipped = 0
            for index, source_path in enumerate(candidates, start=1):
                metadata = self.get_metadata(source_path)
                source_hash = metadata.get("sha256") or compute_sha256(source_path)
                destination = usb_music / source_path.name

                if source_hash in existing_usb_files:
                    skipped += 1
                    self.ui_call(
                        lambda name=source_path.name: self.log(
                            f"[-] USB duplicate skipped: {name}"
                        )
                    )
                elif destination.exists():
                    dest_hash = compute_sha256(destination)
                    if dest_hash == source_hash:
                        skipped += 1
                    else:
                        shutil.copy2(source_path, destination)
                        copied += 1
                        existing_usb_files[source_hash] = destination
                else:
                    shutil.copy2(source_path, destination)
                    copied += 1
                    existing_usb_files[source_hash] = destination

                self.ui_call(lambda value=index: self.set_progress_value(value))

            self.stats_data["songs"] += copied
            self.stats_data["gb"] += round(sum(path.stat().st_size for path in candidates) / (1024 ** 3), 2)
            self.stats_data["time_saved"] += copied * 3
            self.ui_call(self.update_stats_label)
            self.ui_call(lambda: self.log(f"[+] USB sync complete. Copied: {copied}, skipped: {skipped}"))
            self.set_status_async("USB sync complete")
            self.start_library_scan(startup=False)
            if copied and not triggered_by_download:
                self.notify_windows("USBIFY", f"USB sync complete. Copied {copied} song(s).")
        except Exception as exc:
            self.ui_call(lambda: self.log(f"[!] USB sync error: {exc}"))
            self.set_status_async("USB sync failed")
        finally:
            self.sync_in_progress = False
            self.ui_call(self.stop_progress)

    def start_duplicate_scan(self):
        threading.Thread(target=self.duplicate_scan_worker, daemon=True).start()

    def duplicate_scan_worker(self):
        try:
            self.ui_call(self.set_progress_indeterminate)
            self.set_status_async("Scanning duplicates by SHA256...")
            self.scan_library_worker(startup=False)
            groups = self.db.duplicate_groups()

            if not groups:
                self.ui_call(lambda: self.log("[+] No duplicates detected by SHA256"))
                self.set_status_async("No duplicates found")
                return

            removed = 0
            for group in groups:
                keeper = self.choose_duplicate_keeper(group)
                self.ui_call(
                    lambda keep=keeper: self.log(
                        f"[*] Keeping duplicate master: {Path(keep['path']).name}"
                    )
                )

                for item in group:
                    if item["path"] == keeper["path"]:
                        continue

                    self.ui_call(
                        lambda path=item["path"]: self.log(
                            f"[-] Duplicate found: {Path(path).name}"
                        )
                    )

                    if self.auto_delete_duplicates_var.get() and item["source"] != "usb":
                        try:
                            Path(item["path"]).unlink(missing_ok=True)
                            removed += 1
                            self.ui_call(
                                lambda path=item["path"]: self.log(
                                    f"[+] Auto deleted duplicate: {Path(path).name}"
                                )
                            )
                        except Exception as exc:
                            self.ui_call(
                                lambda path=item["path"], error=str(exc): self.log(
                                    f"[!] Failed to delete duplicate {Path(path).name}: {error}"
                                )
                            )

            self.set_status_async("Duplicate scan complete")
            self.ui_call(lambda: self.log(f"[+] Duplicate scan complete. Removed: {removed}"))
            self.start_library_scan(startup=False)
        except Exception as exc:
            self.ui_call(lambda: self.log(f"[!] Duplicate scan error: {exc}"))
            self.set_status_async("Duplicate scan failed")
        finally:
            self.ui_call(self.stop_progress)

    def choose_duplicate_keeper(self, group):
        def score(item):
            keep_usb_bonus = 100 if item["source"] == "usb" else 0
            favorite_bonus = 10 if self.db.is_favorite(item["path"]) else 0
            return keep_usb_bonus + favorite_bonus - item.get("mtime", 0)

        return sorted(group, key=score, reverse=True)[0]

    def setup_styles(self):
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        self.style.configure(
            "Spotify.Treeview",
            background=SPOTIFY_PANEL,
            fieldbackground=SPOTIFY_PANEL,
            foreground="white",
            rowheight=38,
            borderwidth=0,
            relief="flat",
            font=("Segoe UI", 10),
        )
        self.style.map(
            "Spotify.Treeview",
            background=[("selected", SPOTIFY_GREEN)],
            foreground=[("selected", "black")],
        )
        self.style.configure(
            "Spotify.Treeview.Heading",
            background="#161616",
            foreground=SPOTIFY_MUTED,
            relief="flat",
            borderwidth=0,
            font=("Segoe UI", 9, "bold"),
        )
        self.style.configure(
            "Spotify.TNotebook",
            background=SPOTIFY_BG,
            borderwidth=0,
        )
        self.style.configure(
            "Spotify.TNotebook.Tab",
            background=SPOTIFY_PANEL,
            foreground=SPOTIFY_MUTED,
            padding=(12, 7),
            font=("Segoe UI", 9, "bold"),
        )
        self.style.map(
            "Spotify.TNotebook.Tab",
            background=[("selected", "#202020"), ("active", "#242424")],
            foreground=[("selected", "white"), ("active", "white")],
        )
        self.style.configure(
            "Spotify.Horizontal.TProgressbar",
            troughcolor="#232323",
            background=SPOTIFY_GREEN,
            bordercolor="#232323",
            lightcolor=SPOTIFY_GREEN,
            darkcolor=SPOTIFY_GREEN,
        )

    def style_action_button(self, widget, primary=False):
        base = SPOTIFY_GREEN if primary else "#242424"
        hover = "#35d56d" if primary else "#323232"
        fg = "black" if primary else "white"
        widget.config(
            bg=base,
            fg=fg,
            bd=0,
            relief="flat",
            activebackground=hover,
            activeforeground=fg,
            cursor="hand2",
        )
        widget.bind("<Enter>", lambda _e, w=widget, c=hover: w.config(bg=c))
        widget.bind("<Leave>", lambda _e, w=widget, c=base: w.config(bg=c))

    def create_sidebar_nav_item(self, text):
        item = tk.Label(
            self.sidebar_nav,
            text=text,
            fg="white",
            bg="#151515",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
            padx=14,
            pady=10,
            cursor="hand2",
        )
        item.pack(fill="x", pady=3)
        item.bind("<Enter>", lambda _e, w=item: w.config(bg="#242424"))
        item.bind("<Leave>", lambda _e, w=item: w.config(bg="#151515"))
        return item

    def build_song_row_values(self, row, index):
        title = row.get("title") or Path(row["path"]).stem
        artist = row.get("artist") or "Unknown artist"
        album = row.get("album") or "Single"
        added = (row.get("last_scanned") or row.get("played_at") or now_iso())[:10]
        duration = format_seconds(float(row.get("duration") or 0))
        return (index, f"■  {title}  ·  {artist}", album, added, duration)

    def derive_ambience_colors(self):
        seed = self.current_song_path or (self.current_metadata or {}).get("path") or "usbify"
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
        red = int(digest[0:2], 16)
        green = int(digest[2:4], 16)
        blue = int(digest[4:6], 16)
        color_a = f"#{max(22, red // 2):02x}{max(22, green // 3):02x}{max(22, blue // 3):02x}"
        color_b = f"#{max(26, red // 3):02x}{max(26, green // 2):02x}{max(26, blue // 2):02x}"
        return color_a, color_b

    def apply_hero_ambience(self):
        color_a, color_b = self.derive_ambience_colors()
        self.header_card.config(bg=color_a)
        self.hero_glow_left.config(bg=color_a)
        self.hero_glow_right.config(bg=color_b)
        self.hero_cover_frame.config(bg=color_b)
        self.hero_cover_label.config(bg=color_b)
        self.hero_text_frame.config(bg=color_a)
        self.header_subtitle_label.config(bg=color_a)
        self.header_title_label.config(bg=color_a)
        self.header_meta_label.config(bg=color_a)
        self.hero_right_panel.config(bg=color_a)
        self.hero_right_title.config(bg=color_a)
        if hasattr(self, "artist_logo_frame"):
            self.artist_logo_frame.config(bg=SPOTIFY_GREEN)

    def start_background_animation(self):
        if self.background_animation_job is not None:
            try:
                self.root.after_cancel(self.background_animation_job)
            except Exception:
                pass
        self.background_animation_phase = 0
        self.animate_background_step()

    def animate_background_step(self):
        self.background_animation_phase = (self.background_animation_phase + 1) % 24
        color_a, color_b = self.derive_ambience_colors()
        palette = [color_a, color_b, "#17301f", "#16263a"]
        left_color = palette[self.background_animation_phase % len(palette)]
        right_color = palette[(self.background_animation_phase + 1) % len(palette)]
        self.hero_glow_left.config(bg=left_color)
        self.hero_glow_right.config(bg=right_color)
        self.background_animation_job = self.root.after(900, self.animate_background_step)

    def start_cover_animation(self):
        if self.cover_animation_job is not None:
            try:
                self.root.after_cancel(self.cover_animation_job)
            except Exception:
                pass
        self.cover_animation_phase = 0
        self.animate_cover_step()

    def animate_cover_step(self):
        if not self.current_song_path:
            return

        self.cover_animation_phase = (self.cover_animation_phase + 1) % 12
        pulse_colors = ["#1DB954", "#22c55e", "#16a34a", "#22c55e"]
        pulse = pulse_colors[self.cover_animation_phase % len(pulse_colors)]
        self.cover_frame.config(bg=pulse)
        self.hero_cover_frame.config(bg=pulse)
        self.cover_animation_job = self.root.after(380, self.animate_cover_step)

    def update_header_summary(self, focus_item=None):
        item = focus_item or (self.current_library_items[0] if self.current_library_items else None)
        total_songs = len(self.current_library_items)
        total_duration = sum(float(song.get("duration") or 0) for song in self.current_library_items)
        if item:
            title = item.get("album") or item.get("title") or "Your Music"
            subtitle = item.get("artist") or "USB Library"
        else:
            title = "Your Music"
            subtitle = "USB Library" if self.current_usb_drive else "Local Collection"

        self.header_subtitle_label.config(text=subtitle.upper()[:36])
        self.header_title_label.config(text=title[:34])
        self.header_meta_label.config(text=f"USBIFY • {total_songs} canciones • {format_seconds(total_duration)}")

    def update_artist_logo(self, artist_name):
        artist_name = (artist_name or "USBIFY").strip()
        initials = "".join(part[0] for part in artist_name.split()[:2] if part).upper() or "U"
        if hasattr(self, "artist_logo_label"):
            self.artist_logo_label.config(text=initials)
        if hasattr(self, "artist_name_label"):
            self.artist_name_label.config(text=artist_name[:24])

    def save_app_state(self):
        safe_write_json(
            APP_STATE_FILE,
            {
                "last_song_path": self.current_song_path,
                "last_position": round(self.last_progress_position, 2),
                "paused": self.paused,
                "saved_at": now_iso(),
            },
        )

    def restore_last_song(self):
        song_path = self.app_state.get("last_song_path")
        if not song_path:
            return

        path = Path(song_path)
        if not path.exists():
            return

        position = float(self.app_state.get("last_position") or 0.0)
        self.log(f"[*] Restoring last song: {path.name}")
        self.play_song_path(path, source="resume", resume_position=position)

    def build_ui(self):
        self.setup_styles()

        self.sidebar = tk.Frame(self.root, bg="#0d0d0d", width=270)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        self.sidebar_logo = tk.Frame(self.sidebar, bg="#0d0d0d")
        self.sidebar_logo.pack(fill="x", padx=20, pady=(22, 16))
        tk.Label(self.sidebar_logo, text="USBIFY", fg=SPOTIFY_GREEN, bg="#0d0d0d", font=("Segoe UI", 27, "bold")).pack(anchor="w")
        tk.Label(self.sidebar_logo, text="Spotify-inspired desktop player", fg=SPOTIFY_MUTED, bg="#0d0d0d", font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))

        self.sidebar_nav = tk.Frame(self.sidebar, bg="#0d0d0d")
        self.sidebar_nav.pack(fill="x", padx=16, pady=(0, 14))
        self.nav_library = self.create_sidebar_nav_item("Biblioteca")
        self.nav_playlists = self.create_sidebar_nav_item("Playlists")
        self.nav_albums = self.create_sidebar_nav_item("Álbumes")
        self.nav_artists = self.create_sidebar_nav_item("Artistas")

        self.online_search_entry = tk.Entry(self.sidebar, bg=SPOTIFY_PANEL, fg="white", relief="flat", font=("Segoe UI", 11), insertbackground="white", bd=10)
        self.online_search_entry.pack(fill="x", padx=18, pady=(0, 8))
        self.search_button = tk.Button(self.sidebar, text="SEARCH MUSIC", command=self.search_online_music, font=("Segoe UI", 10, "bold"), pady=7)
        self.search_button.pack(fill="x", padx=18, pady=(0, 8))
        self.style_action_button(self.search_button, primary=True)

        self.url_entry = tk.Entry(self.sidebar, bg=SPOTIFY_PANEL, fg="white", relief="flat", font=("Segoe UI", 10), insertbackground="white", bd=10)
        self.url_entry.pack(fill="x", padx=18, pady=(0, 8))
        self.mode_frame = tk.Frame(self.sidebar, bg="#0d0d0d")
        self.mode_frame.pack(fill="x", padx=18, pady=(2, 10))
        self.mode_var = tk.StringVar(value="audio")
        self.audio_mode = tk.Radiobutton(self.mode_frame, text="MP3", variable=self.mode_var, value="audio", bg="#0d0d0d", fg="white", selectcolor=SPOTIFY_PANEL, activebackground="#0d0d0d", activeforeground="white")
        self.audio_mode.pack(side="left")
        self.video_mode = tk.Radiobutton(self.mode_frame, text="MP4", variable=self.mode_var, value="video", bg="#0d0d0d", fg="white", selectcolor=SPOTIFY_PANEL, activebackground="#0d0d0d", activeforeground="white")
        self.video_mode.pack(side="left", padx=(10, 0))
        self.quality_var = tk.StringVar(value="1080p")
        self.quality_menu = ttk.Combobox(self.sidebar, textvariable=self.quality_var, values=["1080p", "720p", "480p", "Best"], state="readonly")
        self.quality_menu.pack(fill="x", padx=18, pady=(0, 10))

        self.download_button = tk.Button(self.sidebar, text="DOWNLOAD TO USB", command=self.start_download, font=("Segoe UI", 10, "bold"), pady=8)
        self.download_button.pack(fill="x", padx=18, pady=(0, 8))
        self.style_action_button(self.download_button, primary=True)
        self.resume_button = tk.Button(self.sidebar, text="RESUME", command=self.resume_last_download, font=("Segoe UI", 10, "bold"), pady=7)
        self.resume_button.pack(fill="x", padx=18, pady=(0, 8))
        self.style_action_button(self.resume_button)
        self.scan_button = tk.Button(self.sidebar, text="SCAN", command=self.start_library_scan, font=("Segoe UI", 10, "bold"), pady=7)
        self.scan_button.pack(fill="x", padx=18, pady=(0, 8))
        self.style_action_button(self.scan_button)
        self.sync_button = tk.Button(self.sidebar, text="SYNC USB", command=self.start_usb_sync, font=("Segoe UI", 10, "bold"), pady=7)
        self.sync_button.pack(fill="x", padx=18, pady=(0, 8))
        self.style_action_button(self.sync_button)
        self.dedupe_button = tk.Button(self.sidebar, text="SHA256 DEDUPE", command=self.start_duplicate_scan, font=("Segoe UI", 10, "bold"), pady=7)
        self.dedupe_button.pack(fill="x", padx=18, pady=(0, 8))
        self.style_action_button(self.dedupe_button)
        self.mini_button = tk.Button(self.sidebar, text="MINI PLAYER", command=lambda: self.mini_player.toggle(), font=("Segoe UI", 10, "bold"), pady=7)
        self.mini_button.pack(fill="x", padx=18, pady=(0, 14))
        self.style_action_button(self.mini_button)

        self.options_frame = tk.Frame(self.sidebar, bg="#0d0d0d")
        self.options_frame.pack(fill="x", padx=18, pady=(0, 8))
        self.auto_delete_duplicates_check = tk.Checkbutton(self.options_frame, text="Auto delete duplicates", variable=self.auto_delete_duplicates_var, bg="#0d0d0d", fg="white", selectcolor=SPOTIFY_PANEL, activebackground="#0d0d0d", activeforeground="white")
        self.auto_delete_duplicates_check.pack(anchor="w")
        self.auto_sync_check = tk.Checkbutton(self.options_frame, text="Auto sync USB", variable=self.auto_sync_var, bg="#0d0d0d", fg="white", selectcolor=SPOTIFY_PANEL, activebackground="#0d0d0d", activeforeground="white")
        self.auto_sync_check.pack(anchor="w")
        self.listen_download_check = tk.Checkbutton(self.options_frame, text="Listen while download", variable=self.listen_while_download_var, bg="#0d0d0d", fg="white", selectcolor=SPOTIFY_PANEL, activebackground="#0d0d0d", activeforeground="white")
        self.listen_download_check.pack(anchor="w")
        self.auto_resume_check = tk.Checkbutton(self.options_frame, text="Auto resume", variable=self.auto_resume_downloads_var, bg="#0d0d0d", fg="white", selectcolor=SPOTIFY_PANEL, activebackground="#0d0d0d", activeforeground="white")
        self.auto_resume_check.pack(anchor="w")
        self.gaming_mode_check = tk.Checkbutton(self.options_frame, text="Gaming mode no notifications", variable=self.gaming_mode_var, bg="#0d0d0d", fg="white", selectcolor=SPOTIFY_PANEL, activebackground="#0d0d0d", activeforeground="white")
        self.gaming_mode_check.pack(anchor="w")

        tk.Label(self.sidebar, text="Playlists", fg=SPOTIFY_MUTED, bg="#0d0d0d", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=20, pady=(10, 6))
        self.sidebar_playlist_listbox = tk.Listbox(self.sidebar, bg="#121212", fg="white", selectbackground="#282828", selectforeground="white", relief="flat", bd=0, highlightthickness=0, font=("Segoe UI", 10), height=8)
        self.sidebar_playlist_listbox.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        tk.Label(self.sidebar, text="Escuchado recientemente", fg=SPOTIFY_MUTED, bg="#0d0d0d", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=20, pady=(4, 6))
        self.sidebar_recent_listbox = tk.Listbox(self.sidebar, bg="#121212", fg="white", selectbackground="#282828", selectforeground="white", relief="flat", bd=0, highlightthickness=0, font=("Segoe UI", 9), height=7)
        self.sidebar_recent_listbox.pack(fill="x", padx=18, pady=(0, 16))

        self.main_content = tk.Frame(self.root, bg=SPOTIFY_BG)
        self.main_content.pack(side="left", fill="both", expand=True)
        self.main_content.pack_propagate(False)
        self.playlist_title = tk.Label(self.main_content, text="Spotify Desktop Style Library", fg="white", bg=SPOTIFY_BG, font=("Segoe UI", 27, "bold"))
        self.playlist_title.pack(anchor="w", padx=28, pady=(22, 10))

        self.top_search_row = tk.Frame(self.main_content, bg=SPOTIFY_BG)
        self.top_search_row.pack(fill="x", padx=28, pady=(0, 12))
        self.library_search_entry = tk.Entry(self.top_search_row, bg="#1a1a1a", fg="white", relief="flat", font=("Segoe UI", 11), insertbackground="white", bd=10)
        self.library_search_entry.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.top_search_hint = tk.Label(self.top_search_row, text="Fast desktop mode", fg=SPOTIFY_MUTED, bg=SPOTIFY_BG, font=("Segoe UI", 10))
        self.top_search_hint.pack(side="left")

        self.header_card = tk.Frame(self.main_content, bg="#1a1a1a", height=240)
        self.header_card.pack(fill="x", padx=28, pady=(0, 14))
        self.header_card.pack_propagate(False)
        self.hero_glow_left = tk.Frame(self.header_card, bg="#1f2d1f", width=18)
        self.hero_glow_left.pack(side="left", fill="y")
        self.hero_glow_right = tk.Frame(self.header_card, bg="#1b1f2d", width=18)
        self.hero_glow_right.pack(side="right", fill="y")
        self.hero_cover_frame = tk.Frame(self.header_card, bg=SPOTIFY_GREEN, width=200, height=200)
        self.hero_cover_frame.pack(side="left", padx=24, pady=20)
        self.hero_cover_frame.pack_propagate(False)
        self.hero_cover_label = tk.Label(self.hero_cover_frame, text="♪", fg="black", bg=SPOTIFY_GREEN, font=("Segoe UI", 44, "bold"))
        self.hero_cover_label.pack(fill="both", expand=True)
        self.hero_text_frame = tk.Frame(self.header_card, bg="#1a1a1a")
        self.hero_text_frame.pack(side="left", fill="both", expand=True, padx=(0, 16), pady=24)
        self.header_subtitle_label = tk.Label(self.hero_text_frame, text="USB LIBRARY", fg=SPOTIFY_MUTED, bg="#1a1a1a", font=("Segoe UI", 10, "bold"))
        self.header_subtitle_label.pack(anchor="w")
        self.header_title_label = tk.Label(self.hero_text_frame, text="Your Music", fg="white", bg="#1a1a1a", font=("Segoe UI", 32, "bold"))
        self.header_title_label.pack(anchor="w", pady=(8, 10))
        self.header_meta_label = tk.Label(self.hero_text_frame, text="USBIFY • 0 canciones • 00:00", fg=SPOTIFY_MUTED, bg="#1a1a1a", font=("Segoe UI", 10))
        self.header_meta_label.pack(anchor="w")
        self.hero_right_panel = tk.Frame(self.header_card, bg="#1a1a1a", width=230)
        self.hero_right_panel.pack(side="right", fill="y", padx=20, pady=20)
        self.hero_right_panel.pack_propagate(False)
        self.hero_right_title = tk.Label(self.hero_right_panel, text="Quick Controls", fg="white", bg="#1a1a1a", font=("Segoe UI", 12, "bold"))
        self.hero_right_title.pack(anchor="w")
        self.artist_logo_frame = tk.Frame(self.hero_right_panel, bg=SPOTIFY_GREEN, width=54, height=54)
        self.artist_logo_frame.pack(anchor="w", pady=(12, 8))
        self.artist_logo_frame.pack_propagate(False)
        self.artist_logo_label = tk.Label(self.artist_logo_frame, text="U", fg="black", bg=SPOTIFY_GREEN, font=("Segoe UI", 18, "bold"))
        self.artist_logo_label.pack(fill="both", expand=True)
        self.artist_name_label = tk.Label(self.hero_right_panel, text="USBIFY Artist", fg=SPOTIFY_MUTED, bg="#1a1a1a", font=("Segoe UI", 9))
        self.artist_name_label.pack(anchor="w", pady=(0, 6))
        self.hero_play_button = tk.Button(self.hero_right_panel, text="PLAY", command=self.play_selected_media, font=("Segoe UI", 11, "bold"), pady=10)
        self.hero_play_button.pack(fill="x", pady=(14, 8))
        self.style_action_button(self.hero_play_button, primary=True)
        self.hero_shuffle_button = tk.Button(self.hero_right_panel, text="SMART SHUFFLE", command=self.play_random_song, font=("Segoe UI", 10, "bold"), pady=8)
        self.hero_shuffle_button.pack(fill="x", pady=(0, 8))
        self.style_action_button(self.hero_shuffle_button)
        self.hero_resume_button = tk.Button(self.hero_right_panel, text="RESUME LAST SONG", command=self.restore_last_song, font=("Segoe UI", 10, "bold"), pady=8)
        self.hero_resume_button.pack(fill="x")
        self.style_action_button(self.hero_resume_button)

        self.content_row = tk.Frame(self.main_content, bg=SPOTIFY_BG)
        self.content_row.pack(fill="both", expand=True, padx=28, pady=(0, 10))
        self.library_panel = tk.Frame(self.content_row, bg=SPOTIFY_PANEL)
        self.library_panel.pack(side="left", fill="both", expand=True, padx=(0, 14))
        self.library_actions = tk.Frame(self.library_panel, bg=SPOTIFY_PANEL)
        self.library_actions.pack(fill="x", padx=16, pady=(14, 10))
        self.library_play_button = tk.Button(self.library_actions, text="PLAY", command=self.play_selected_media, font=("Segoe UI", 9, "bold"), padx=16, pady=8)
        self.library_play_button.pack(side="left", padx=(0, 8))
        self.style_action_button(self.library_play_button, primary=True)
        self.add_queue_button = tk.Button(self.library_actions, text="ADD TO QUEUE", command=self.add_selected_to_queue, font=("Segoe UI", 9, "bold"), padx=16, pady=8)
        self.add_queue_button.pack(side="left", padx=(0, 8))
        self.style_action_button(self.add_queue_button)
        self.favorite_selected_button = tk.Button(self.library_actions, text="HEART", command=self.toggle_selected_favorite, font=("Segoe UI", 9, "bold"), padx=16, pady=8)
        self.favorite_selected_button.pack(side="left", padx=(0, 8))
        self.style_action_button(self.favorite_selected_button)
        self.playlist_target_var = tk.StringVar(value="")
        self.playlist_target_menu = ttk.Combobox(self.library_actions, textvariable=self.playlist_target_var, state="readonly", width=16)
        self.playlist_target_menu.pack(side="left", padx=(0, 8))
        self.add_playlist_button = tk.Button(self.library_actions, text="ADD TO PLAYLIST", command=self.add_selected_to_playlist, font=("Segoe UI", 9, "bold"), padx=16, pady=8)
        self.add_playlist_button.pack(side="left", padx=(0, 8))
        self.style_action_button(self.add_playlist_button)
        self.refresh_button = tk.Button(self.library_actions, text="REFRESH", command=self.refresh_library_view, font=("Segoe UI", 9, "bold"), padx=16, pady=8)
        self.refresh_button.pack(side="left")
        self.style_action_button(self.refresh_button)

        self.song_table_frame = tk.Frame(self.library_panel, bg=SPOTIFY_PANEL)
        self.song_table_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.song_table_scroll = ttk.Scrollbar(self.song_table_frame, orient="vertical")
        self.song_table_scroll.pack(side="right", fill="y")
        self.song_table = ttk.Treeview(self.song_table_frame, style="Spotify.Treeview", columns=("num", "title", "album", "added", "duration"), show="headings", yscrollcommand=self.song_table_scroll.set, selectmode="browse")
        self.song_table.pack(side="left", fill="both", expand=True)
        self.song_table_scroll.config(command=self.song_table.yview)
        self.song_table.heading("num", text="#")
        self.song_table.heading("title", text="Título")
        self.song_table.heading("album", text="Álbum")
        self.song_table.heading("added", text="Fecha añadida")
        self.song_table.heading("duration", text="Duración")
        self.song_table.column("num", width=42, anchor="center", stretch=False)
        self.song_table.column("title", width=440, anchor="w")
        self.song_table.column("album", width=190, anchor="w")
        self.song_table.column("added", width=110, anchor="center", stretch=False)
        self.song_table.column("duration", width=80, anchor="center", stretch=False)

        self.side_panel = tk.Frame(self.content_row, bg=SPOTIFY_PANEL, width=370)
        self.side_panel.pack(side="right", fill="y")
        self.side_panel.pack_propagate(False)
        self.side_title = tk.Label(self.side_panel, text="Smart Panels", fg="white", bg=SPOTIFY_PANEL, font=("Segoe UI", 15, "bold"))
        self.side_title.pack(anchor="w", padx=14, pady=(14, 10))
        self.notebook = ttk.Notebook(self.side_panel, style="Spotify.TNotebook")
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.queue_tab = tk.Frame(self.notebook, bg=SPOTIFY_BG)
        self.history_tab = tk.Frame(self.notebook, bg=SPOTIFY_BG)
        self.favorites_tab = tk.Frame(self.notebook, bg=SPOTIFY_BG)
        self.karaoke_tab = tk.Frame(self.notebook, bg=SPOTIFY_BG)
        self.playlists_tab = tk.Frame(self.notebook, bg=SPOTIFY_BG)
        self.notebook.add(self.queue_tab, text="Queue")
        self.notebook.add(self.history_tab, text="History")
        self.notebook.add(self.favorites_tab, text="Favorites")
        self.notebook.add(self.karaoke_tab, text="Karaoke")
        self.notebook.add(self.playlists_tab, text="Playlists")

        self.queue_listbox = tk.Listbox(self.queue_tab, bg=SPOTIFY_PANEL, fg="white", selectbackground="#282828", selectforeground="white", relief="flat", bd=0, highlightthickness=0, font=("Segoe UI", 10))
        self.queue_listbox.pack(fill="both", expand=True, padx=8, pady=(8, 8))
        self.history_listbox = tk.Listbox(self.history_tab, bg=SPOTIFY_PANEL, fg="white", selectbackground="#282828", selectforeground="white", relief="flat", bd=0, highlightthickness=0, font=("Segoe UI", 10))
        self.history_listbox.pack(fill="both", expand=True, padx=8, pady=(8, 8))
        self.favorites_listbox = tk.Listbox(self.favorites_tab, bg=SPOTIFY_PANEL, fg="white", selectbackground="#282828", selectforeground="white", relief="flat", bd=0, highlightthickness=0, font=("Segoe UI", 10))
        self.favorites_listbox.pack(fill="both", expand=True, padx=8, pady=(8, 8))
        self.karaoke_status_label = tk.Label(self.karaoke_tab, text="No .lrc loaded", fg=SPOTIFY_MUTED, bg=SPOTIFY_BG, anchor="w", font=("Segoe UI", 9))
        self.karaoke_status_label.pack(fill="x", padx=8, pady=(8, 6))
        self.karaoke_text = tk.Text(self.karaoke_tab, bg=SPOTIFY_PANEL, fg="white", relief="flat", font=("Segoe UI", 10), wrap="word", bd=0)
        self.karaoke_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.karaoke_text.tag_configure("current", foreground=SPOTIFY_GREEN, font=("Segoe UI", 10, "bold"))
        self.karaoke_text.tag_configure("normal", foreground="white")

        if not hasattr(self, "karaoke_text"):
            self.karaoke_text = None

        playlists_top = tk.Frame(self.playlists_tab, bg=SPOTIFY_BG)
        playlists_top.pack(fill="x", padx=8, pady=(8, 8))
        self.new_playlist_entry = tk.Entry(playlists_top, bg=SPOTIFY_PANEL, fg="white", relief="flat", font=("Segoe UI", 9), insertbackground="white", bd=8)
        self.new_playlist_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.create_playlist_button = tk.Button(playlists_top, text="CREATE", command=self.create_playlist, font=("Segoe UI", 9, "bold"), pady=6)
        self.create_playlist_button.pack(side="left")
        self.style_action_button(self.create_playlist_button, primary=True)
        self.playlists_listbox = tk.Listbox(self.playlists_tab, bg=SPOTIFY_PANEL, fg="white", selectbackground="#282828", selectforeground="white", relief="flat", bd=0, highlightthickness=0, font=("Segoe UI", 10), height=7)
        self.playlists_listbox.pack(fill="x", padx=8, pady=(0, 8))
        self.playlist_songs_listbox = tk.Listbox(self.playlists_tab, bg=SPOTIFY_PANEL, fg="white", selectbackground="#282828", selectforeground="white", relief="flat", bd=0, highlightthickness=0, font=("Segoe UI", 9))
        self.playlist_songs_listbox.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        queue_actions = tk.Frame(self.queue_tab, bg=SPOTIFY_BG)
        queue_actions.pack(fill="x", padx=8, pady=(0, 8))
        self.queue_play_button = tk.Button(queue_actions, text="PLAY", command=self.play_selected_queue_song, font=("Segoe UI", 9, "bold"), pady=6)
        self.queue_play_button.grid(row=0, column=0, padx=(0, 6), pady=(0, 6), sticky="ew")
        self.style_action_button(self.queue_play_button, primary=True)
        self.queue_remove_button = tk.Button(queue_actions, text="REMOVE", command=self.remove_selected_queue_song, font=("Segoe UI", 9, "bold"), pady=6)
        self.queue_remove_button.grid(row=0, column=1, padx=6, pady=(0, 6), sticky="ew")
        self.style_action_button(self.queue_remove_button)
        self.queue_up_button = tk.Button(queue_actions, text="UP", command=lambda: self.move_queue_selection(-1), font=("Segoe UI", 9, "bold"), pady=6)
        self.queue_up_button.grid(row=1, column=0, padx=(0, 6), pady=(0, 6), sticky="ew")
        self.style_action_button(self.queue_up_button)
        self.queue_down_button = tk.Button(queue_actions, text="DOWN", command=lambda: self.move_queue_selection(1), font=("Segoe UI", 9, "bold"), pady=6)
        self.queue_down_button.grid(row=1, column=1, padx=6, pady=(0, 6), sticky="ew")
        self.style_action_button(self.queue_down_button)
        self.queue_save_button = tk.Button(queue_actions, text="SAVE", command=self.save_queue_as_playlist, font=("Segoe UI", 9, "bold"), pady=6)
        self.queue_save_button.grid(row=2, column=0, padx=(0, 6), pady=(0, 6), sticky="ew")
        self.style_action_button(self.queue_save_button)
        self.queue_clear_button = tk.Button(queue_actions, text="CLEAR", command=self.clear_queue, font=("Segoe UI", 9, "bold"), pady=6)
        self.queue_clear_button.grid(row=2, column=1, padx=6, pady=(0, 6), sticky="ew")
        self.style_action_button(self.queue_clear_button)
        queue_actions.grid_columnconfigure(0, weight=1)
        queue_actions.grid_columnconfigure(1, weight=1)

        history_actions = tk.Frame(self.history_tab, bg=SPOTIFY_BG)
        history_actions.pack(fill="x", padx=8, pady=(0, 8))
        self.history_play_button = tk.Button(history_actions, text="PLAY", command=self.play_selected_history_song, font=("Segoe UI", 9, "bold"), pady=6)
        self.history_play_button.pack(side="left", expand=True, fill="x", padx=(0, 6))
        self.style_action_button(self.history_play_button, primary=True)
        self.history_clear_button = tk.Button(history_actions, text="CLEAR HISTORY", command=self.clear_history, font=("Segoe UI", 9, "bold"), pady=6)
        self.history_clear_button.pack(side="left", expand=True, fill="x")
        self.style_action_button(self.history_clear_button)

        favorite_actions = tk.Frame(self.favorites_tab, bg=SPOTIFY_BG)
        favorite_actions.pack(fill="x", padx=8, pady=(0, 8))
        self.favorites_play_button = tk.Button(favorite_actions, text="PLAY", command=self.play_selected_favorite_song, font=("Segoe UI", 9, "bold"), pady=6)
        self.favorites_play_button.pack(side="left", expand=True, fill="x", padx=(0, 6))
        self.style_action_button(self.favorites_play_button, primary=True)
        self.favorites_toggle_button = tk.Button(favorite_actions, text="TOGGLE HEART", command=self.toggle_selected_favorite_from_panel, font=("Segoe UI", 9, "bold"), pady=6)
        self.favorites_toggle_button.pack(side="left", expand=True, fill="x")
        self.style_action_button(self.favorites_toggle_button)

        playlist_actions = tk.Frame(self.playlists_tab, bg=SPOTIFY_BG)
        playlist_actions.pack(fill="x", padx=8, pady=(0, 8))
        self.playlist_load_button = tk.Button(playlist_actions, text="LOAD TO QUEUE", command=self.load_selected_playlist_to_queue, font=("Segoe UI", 9, "bold"), pady=6)
        self.playlist_load_button.pack(side="left", expand=True, fill="x", padx=(0, 6))
        self.style_action_button(self.playlist_load_button)
        self.playlist_play_button = tk.Button(playlist_actions, text="PLAY SONG", command=self.play_selected_playlist_song, font=("Segoe UI", 9, "bold"), pady=6)
        self.playlist_play_button.pack(side="left", expand=True, fill="x")
        self.style_action_button(self.playlist_play_button, primary=True)

        self.console = scrolledtext.ScrolledText(self.main_content, bg="#0d0d0d", fg=SPOTIFY_GREEN, relief="flat", font=("Consolas", 9), height=4, bd=0)
        self.console.pack(fill="x", padx=28, pady=(0, 8))
        self.progress_bar = ttk.Progressbar(self.main_content, mode="indeterminate", style="Spotify.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x", padx=28, pady=(0, 8))
        self.stats_label = tk.Label(self.main_content, text="Songs: 0 | GB: 0 | Minutes saved: 0", fg=SPOTIFY_GREEN, bg=SPOTIFY_BG, font=("Segoe UI", 10, "bold"))
        self.stats_label.pack(anchor="w", padx=30, pady=(0, 4))
        self.status_label = tk.Label(self.main_content, text="Ready", fg=SPOTIFY_MUTED, bg=SPOTIFY_BG, font=("Segoe UI", 10))
        self.status_label.pack(anchor="w", padx=30, pady=(0, 10))

        self.bottom_player = tk.Frame(self.root, bg="#090909", height=118)
        self.bottom_player.place(relx=0, rely=1.0, relwidth=1, anchor="sw", height=118)
        self.bottom_player.lift()
        self.bottom_player.pack_propagate(False)
        self.song_info_frame = tk.Frame(self.bottom_player, bg="#090909")
        self.song_info_frame.pack(side="left", padx=18)
        self.cover_frame = tk.Frame(self.song_info_frame, bg=SPOTIFY_GREEN, width=68, height=68)
        self.cover_frame.pack(side="left", pady=18)
        self.cover_frame.pack_propagate(False)
        self.cover_label = tk.Label(self.cover_frame, text="♪", fg="black", bg=SPOTIFY_GREEN, font=("Segoe UI", 20, "bold"))
        self.cover_label.pack(fill="both", expand=True)
        self.cover_image = None
        self.song_text_frame = tk.Frame(self.song_info_frame, bg="#090909")
        self.song_text_frame.pack(side="left", padx=12)
        self.now_playing_label = tk.Label(self.song_text_frame, text="No music playing", fg="white", bg="#090909", font=("Segoe UI", 10, "bold"))
        self.now_playing_label.pack(anchor="w")
        self.artist_label = tk.Label(self.song_text_frame, text="USBIFY Player", fg=SPOTIFY_MUTED, bg="#090909", font=("Segoe UI", 8))
        self.artist_label.pack(anchor="w")
        self.time_label = tk.Label(self.song_text_frame, text="00:00 / 00:00", fg=SPOTIFY_MUTED, bg="#090909", font=("Segoe UI", 8))
        self.time_label.pack(anchor="w")
        self.favorite_button = tk.Button(self.song_info_frame, text="♡", command=self.toggle_current_favorite, font=("Segoe UI", 18, "bold"))
        self.favorite_button.pack(side="left", padx=(6, 0))
        self.style_action_button(self.favorite_button)
        self.favorite_button.config(bg="#090909", fg=SPOTIFY_GREEN, activebackground="#171717", activeforeground=SPOTIFY_GREEN)

        self.center_controls = tk.Frame(self.bottom_player, bg="#090909")
        self.center_controls.pack(side="left", expand=True, fill="both", padx=18)
        self.controls_row = tk.Frame(self.center_controls, bg="#090909")
        self.controls_row.pack(pady=(10, 4))
        self.shuffle_button = tk.Button(self.controls_row, text="SHUFFLE", command=self.play_random_song, font=("Segoe UI", 9, "bold"), padx=14, pady=6)
        self.shuffle_button.grid(row=0, column=0, padx=4)
        self.style_action_button(self.shuffle_button)
        self.prev_button = tk.Button(self.controls_row, text="PREVIOUS", command=self.play_random_song, font=("Segoe UI", 9, "bold"), padx=14, pady=6)
        self.prev_button.grid(row=0, column=1, padx=4)
        self.style_action_button(self.prev_button)
        self.play_button = tk.Button(self.controls_row, text="PLAY", command=self.play_selected_media, font=("Segoe UI", 10, "bold"), padx=18, pady=8)
        self.play_button.grid(row=0, column=2, padx=4)
        self.style_action_button(self.play_button, primary=True)
        self.pause_button = tk.Button(self.controls_row, text="PAUSE", command=self.pause_song, font=("Segoe UI", 9, "bold"), padx=14, pady=6)
        self.pause_button.grid(row=0, column=3, padx=4)
        self.style_action_button(self.pause_button)
        self.next_button = tk.Button(self.controls_row, text="NEXT", command=self.next_song, font=("Segoe UI", 9, "bold"), padx=14, pady=6)
        self.next_button.grid(row=0, column=4, padx=4)
        self.style_action_button(self.next_button)
        self.repeat_button = tk.Button(self.controls_row, text="REPEAT", command=self.play_random_song, font=("Segoe UI", 9, "bold"), padx=14, pady=6)
        self.repeat_button.grid(row=0, column=5, padx=4)
        self.style_action_button(self.repeat_button)
        self.song_progress = ttk.Progressbar(self.center_controls, mode="determinate", maximum=100, value=0, style="Spotify.Horizontal.TProgressbar")
        self.song_progress.pack(fill="x", pady=(6, 8))

        self.volume_frame = tk.Frame(self.bottom_player, bg="#090909")
        self.volume_frame.pack(side="right", padx=20)
        self.volume_label = tk.Label(self.volume_frame, text="VOL", fg="white", bg="#090909", font=("Segoe UI", 10, "bold"))
        self.volume_label.pack(side="left")
        self.volume_slider = tk.Scale(self.volume_frame, from_=0, to=100, orient="horizontal", bg="#090909", fg="white", troughcolor="#333333", highlightthickness=0, length=120, command=self.set_volume)
        self.volume_slider.set(70)
        self.volume_slider.pack(side="left", padx=10)

        if self.audio_ready:
            pygame.mixer.music.set_volume(0.7)

        self.apply_hero_ambience()
        self.update_header_summary()
        self.start_background_animation()


    def open_video_player(self, video_path):
        if not Path(video_path).exists():
            self.log("[!] Video not found")
            return

        try:
            if self.video_window and self.video_window.winfo_exists():
                self.video_window.destroy()
        except Exception:
            pass

        self.video_window = tk.Toplevel(self.root)
        self.video_window.title("USBIFY VIDEO PLAYER")
        self.video_window.geometry("1000x700")
        self.video_window.configure(bg="black")

        video_frame = tk.Frame(self.video_window, bg="black")
        video_frame.pack(fill="both", expand=True)

        self.video_window.update()

        media = self.video_instance.media_new(str(video_path))
        self.video_player.set_media(media)

        handle = video_frame.winfo_id()

        if os.name == "nt":
            self.video_player.set_hwnd(handle)
        else:
            self.video_player.set_xwindow(handle)

        self.video_player.play()

        controls = tk.Frame(self.video_window, bg="#111111")
        controls.pack(fill="x")

        tk.Button(
            controls,
            text="PLAY",
            command=self.video_player.play,
            bg="#1ed760",
            fg="black",
            relief="flat",
        ).pack(side="left", padx=5, pady=5)

        tk.Button(
            controls,
            text="PAUSE",
            command=self.video_player.pause,
            bg="#333333",
            fg="white",
            relief="flat",
        ).pack(side="left", padx=5, pady=5)

        tk.Button(
            controls,
            text="STOP",
            command=self.video_player.stop,
            bg="#333333",
            fg="white",
            relief="flat",
        ).pack(side="left", padx=5, pady=5)

    def play_selected_media(self):
        item = self.get_selected_library_item()

        if not item:
            return

        path = item["path"]

        if str(path).lower().endswith(".mp4"):
            self.play_video_fullscreen(path)
        else:
            self.play_selected_song()


    def play_video_inside_panel(self, video_path):
        try:
            self.video_panel.update()

            media = self.video_instance.media_new(str(video_path))
            self.video_player.set_media(media)

            handle = self.video_panel.winfo_id()

            if os.name == "nt":
                self.video_player.set_hwnd(handle)
            else:
                self.video_player.set_xwindow(handle)

            self.video_player.play()

            self.log(f"[+] Playing embedded video: {Path(video_path).name}")

        except Exception as exc:
            self.log(f"[!] Video error: {exc}")



    def open_video_fullscreen(self, video_path):
        try:
            fullscreen_window = tk.Toplevel(self.root)
            fullscreen_window.attributes("-fullscreen", True)
            fullscreen_window.configure(bg="black")

            frame = tk.Frame(fullscreen_window, bg="black")
            frame.pack(fill="both", expand=True)

            fullscreen_window.update()

            media = self.video_instance.media_new(str(video_path))
            player = self.video_instance.media_player_new()
            player.set_media(media)

            handle = frame.winfo_id()

            if os.name == "nt":
                player.set_hwnd(handle)
            else:
                player.set_xwindow(handle)

            player.play()

            fullscreen_window.bind("<Escape>", lambda e: fullscreen_window.destroy())

        except Exception as exc:
            self.log(f"[!] Fullscreen error: {exc}")



    def get_video_library_items(self):
        items = []
        for item in getattr(self, "library_items", []):
            path = str(item.get("path", "")).lower()
            if path.endswith(".mp4"):
                items.append(item)
        return items

    
    def show_videos_only(self):
        try:
            self.usb_listbox.delete(*self.usb_listbox.get_children())

            videos = []

            for item in getattr(self, "library_items", []):
                path = str(item.get("path", "")).lower()

                if path.endswith(".mp4"):
                    videos.append(item)

            self.filtered_video_items = videos

            for idx, item in enumerate(videos, start=1):
                title = item.get("title", Path(item.get("path", "")).stem)
                artist = item.get("artist", "Unknown artist")
                duration = item.get("duration", "00:00")

                self.usb_listbox.insert(
                    "",
                    "end",
                    values=(
                        idx,
                        f"■  {title}  ·  {artist}",
                        "MP4 Video",
                        item.get("date_added", ""),
                        duration,
                    ),
                )

            self.title_label.config(text="Videos")

            self.log(f"[+] Loaded {len(videos)} MP4 videos")

        except Exception as exc:
            self.log(f"[!] Video library error: {exc}")


    
    def play_video_fullscreen(self, video_path):
        try:
            fullscreen = tk.Toplevel(self.root)
            fullscreen.attributes("-fullscreen", True)
            fullscreen.configure(bg="black")

            top_bar = tk.Frame(fullscreen, bg="#111111", height=40)
            top_bar.pack(fill="x", side="top")

            info_frame = tk.Frame(top_bar, bg="#111111")
            info_frame.pack(side="top", pady=5)

            time_label = tk.Label(
                info_frame,
                text="00:00/00:00",
                fg="white",
                bg="#111111",
                font=("Segoe UI", 12, "bold")
            )
            time_label.pack(side="left", padx=(0, 25))

            volume_label = tk.Label(
                info_frame,
                text="VOL 100",
                fg="#00ff66",
                bg="#111111",
                font=("Segoe UI", 11, "bold")
            )
            volume_label.pack(side="left")

            frame = tk.Frame(fullscreen, bg="black")
            frame.pack(fill="both", expand=True)

            fullscreen.update()

            instance = vlc.Instance("--avcodec-hw=none", "--vout=directdraw")
            player = instance.media_player_new()

            media = instance.media_new(str(video_path))
            player.set_media(media)

            handle = frame.winfo_id()

            if os.name == "nt":
                player.set_hwnd(handle)
            else:
                player.set_xwindow(handle)

            player.play()

            saved_position = self.video_positions.get(video_path, 0)

            if saved_position > 0:
                player.set_time(saved_position)

            def update_time():
                try:
                    current_ms = max(player.get_time(), 0)
                    total_ms = max(player.get_length(), 0)

                    self.video_positions[video_path] = current_ms

                    current_sec = current_ms // 1000
                    total_sec = total_ms // 1000

                    current_text = f"{current_sec//60:02}:{current_sec%60:02}"
                    total_text = f"{total_sec//60:02}:{total_sec%60:02}"

                    time_label.config(text=f"{current_text}/{total_text}")

                    fullscreen.after(500, update_time)
                except:
                    pass

            update_time()

            def forward_10(event=None):
                player.set_time(player.get_time() + 10000)

            def back_10(event=None):
                player.set_time(max(player.get_time() - 10000, 0))

            fullscreen.bind("<Right>", forward_10)
            fullscreen.bind("<Left>", back_10)

            def toggle_pause(event=None):
                if player.is_playing():
                    player.pause()
                else:
                    player.play()

            fullscreen.bind("<space>", toggle_pause)

            current_volume = 100
            player.audio_set_volume(current_volume)

            def volume_up(event=None):
                nonlocal current_volume
                current_volume = min(current_volume + 5, 200)
                player.audio_set_volume(current_volume)
                volume_label.config(text=f"VOL {current_volume}")

            def volume_down(event=None):
                nonlocal current_volume
                current_volume = max(current_volume - 5, 0)
                player.audio_set_volume(current_volume)
                volume_label.config(text=f"VOL {current_volume}")

            fullscreen.bind("<Up>", volume_up)
            fullscreen.bind("<Down>", volume_down)

            def close_video(event=None):
                try:
                    self.video_positions[video_path] = player.get_time()
                    player.stop()
                except:
                    pass

                fullscreen.destroy()

            fullscreen.bind("<Escape>", close_video)


        except Exception as exc:
            self.log(f"[!] Fullscreen video error: {exc}")

    def bind_events(self):
        self.song_table.bind("<Double-Button-1>", lambda _: self.play_selected_song())
        self.queue_listbox.bind("<Double-Button-1>", lambda _: self.play_selected_queue_song())
        self.queue_listbox.bind("<Delete>", lambda _: self.remove_selected_queue_song())
        self.history_listbox.bind("<Double-Button-1>", lambda _: self.play_selected_history_song())
        self.favorites_listbox.bind("<Double-Button-1>", lambda _: self.play_selected_favorite_song())
        self.playlists_listbox.bind("<<ListboxSelect>>", lambda _: self.refresh_selected_playlist_songs())
        self.playlist_songs_listbox.bind("<Double-Button-1>", lambda _: self.play_selected_playlist_song())
        self.sidebar_playlist_listbox.bind("<<ListboxSelect>>", lambda _: self.sync_sidebar_playlist_selection())
        self.library_search_entry.bind("<KeyRelease>", self.schedule_search_refresh)

    def sync_sidebar_playlist_selection(self):
        selection = self.sidebar_playlist_listbox.curselection()
        if not selection:
            return
        index = selection[0]
        self.notebook.select(self.playlists_tab)
        self.playlists_listbox.selection_clear(0, tk.END)
        self.playlists_listbox.selection_set(index)
        self.refresh_selected_playlist_songs()

    def start_background_services(self):
        threading.Thread(target=self.usb_monitor_loop, daemon=True).start()
        self.root.after(120, lambda: self.start_library_scan(startup=True))
        if self.auto_resume_downloads_var.get():
            self.root.after(2400, self.resume_pending_downloads)

    def refresh_library_view(self):
        query = self.library_search_entry.get().strip()
        if query:
            rows = self.db.search_songs(query=query, usb_only=False)
        elif self.current_usb_drive:
            rows = self.db.search_songs(query="", usb_only=True, usb_drive=self.current_usb_drive)
        else:
            rows = self.db.list_recent_songs()

        self.current_library_items = rows
        self.song_table_items = {}
        self.song_table.delete(*self.song_table.get_children())
        active_path = str(Path(self.current_song_path).resolve()) if self.current_song_path else None

        for index, row in enumerate(rows, start=1):
            iid = str(index - 1)
            tags = ("playing",) if active_path and str(Path(row["path"]).resolve()) == active_path else ()
            self.song_table.insert("", "end", iid=iid, values=self.build_song_row_values(row, index), tags=tags)
            self.song_table_items[iid] = row

        self.song_table.tag_configure("playing", foreground=SPOTIFY_GREEN)
        self.update_header_summary(rows[0] if rows else None)

    def get_selected_library_item(self):
        selection = self.song_table.selection()
        if not selection:
            return None
        return self.song_table_items.get(selection[0])

    def refresh_playlists_ui(self):
        playlists = self.db.list_playlists()
        if hasattr(self, "sidebar_playlist_listbox"):
            self.sidebar_playlist_listbox.delete(0, tk.END)
        names = []
        for playlist in playlists:
            if hasattr(self, "sidebar_playlist_listbox"):
                self.sidebar_playlist_listbox.insert(tk.END, playlist["name"])
            names.append(playlist["name"])
        self.playlist_target_menu["values"] = names
        if names and not self.playlist_target_var.get():
            self.playlist_target_var.set(names[0])

    def update_cover_display(self):
        self.cover_image = None
        cover_path = self.current_cover_path
        if cover_path:
            try:
                self.cover_image = tk.PhotoImage(file=str(cover_path))
                self.cover_label.config(image=self.cover_image, text="")
                self.hero_cover_label.config(image=self.cover_image, text="")
                return
            except Exception:
                self.cover_image = None

        self.cover_label.config(image="", text="♪")
        self.hero_cover_label.config(image="", text="♪")

    def get_current_playback_position(self):
        current = float(self.playback_base_position or 0.0)
        if not self.paused and self.playback_started_at is not None:
            current += max(0.0, time.monotonic() - self.playback_started_at)
        if self.song_length > 0:
            current = min(current, self.song_length)
        return current

    def play_song_path(self, song_path, source="library", resume_position=0.0):
        if not self.audio_ready:
            self.log("[!] Audio player is not available")
            return

        song_path = Path(song_path)
        if not song_path.exists():
            self.log(f"[!] File not found: {song_path}")
            return

        try:
            pygame.mixer.music.stop()
            pygame.mixer.music.load(str(song_path))
            if resume_position and resume_position > 0:
                try:
                    pygame.mixer.music.play(start=resume_position)
                except Exception:
                    pygame.mixer.music.play()
            else:
                pygame.mixer.music.play()

            self.stop_requested = False
            self.paused = False
            self.current_song_path = str(song_path.resolve())
            self.current_song = song_path.name
            self.current_metadata = self.get_metadata(song_path)
            self.current_cover_path = extract_cover_art(song_path)
            self.song_length = float(self.current_metadata.get("duration") or 0.0)
            self.last_progress_position = max(0.0, float(resume_position or 0.0))
            self.playback_base_position = self.last_progress_position
            self.playback_started_at = time.monotonic()
            self.last_artist_played = self.current_metadata.get("artist", "")

            self.now_playing_label.config(text=self.current_metadata.get("title", self.current_song))
            self.artist_label.config(text=self.current_metadata.get("artist", "USBIFY Player"))
            self.time_label.config(text=f"{format_seconds(self.last_progress_position)} / {format_seconds(self.song_length)}")
            self.song_progress.configure(maximum=max(self.song_length, 1), value=self.last_progress_position)
            self.update_cover_display()
            self.apply_hero_ambience()
            self.update_artist_logo(self.current_metadata.get("artist", "USBIFY Artist"))
            self.load_karaoke_for_song(song_path)
            self.db.record_history(self.current_metadata)
            self.refresh_history_ui()
            self.refresh_library_view()
            self.sync_favorite_button()
            self.start_cover_animation()
            self.save_app_state()
            self.schedule_progress_update()
            self.mini_player.update()

            self.log(f"[+] Playing: {self.format_song_display(self.current_metadata)}")
            self.notify_windows(
                self.current_metadata.get("title", "Now playing")[:NOTIFICATION_SONG_LIMIT],
                self.current_metadata.get("artist", "USBIFY Player"),
                self.current_cover_path,
            )
        except Exception as exc:
            self.log(f"[!] Playback error: {exc}")

    def stop_song(self):
        if not self.audio_ready:
            return

        try:
            self.stop_requested = True
            self.paused = False
            pygame.mixer.music.stop()
            if self.progress_job is not None:
                self.root.after_cancel(self.progress_job)
                self.progress_job = None
            if self.cover_animation_job is not None:
                self.root.after_cancel(self.cover_animation_job)
                self.cover_animation_job = None

            self.current_song_path = None
            self.current_song = None
            self.current_metadata = None
            self.current_cover_path = None
            self.song_length = 0.0
            self.last_progress_position = 0.0
            self.current_lyrics = []
            self.current_lyric_index = -1

            self.now_playing_label.config(text="No music playing")
            self.artist_label.config(text="USBIFY Player")
            self.update_artist_logo("USBIFY Artist")
            self.time_label.config(text="00:00 / 00:00")
            self.song_progress.configure(value=0)
            self.karaoke_status_label.config(text="No .lrc loaded")
            self.karaoke_text.config(state="normal")
            self.karaoke_text.delete("1.0", tk.END)
            self.karaoke_text.insert(tk.END, "No synchronized lyrics found for this song.")
            self.karaoke_text.config(state="disabled")
            self.update_cover_display()
            self.apply_hero_ambience()
            self.sync_favorite_button()
            self.refresh_library_view()
            self.save_app_state()
            self.mini_player.update()
            self.log("[*] Music stopped")
        except Exception as exc:
            self.log(f"[!] Stop error: {exc}")

    def update_music_progress(self):
        self.progress_job = None
        if not self.current_song_path:
            self.time_label.config(text="00:00 / 00:00")
            self.song_progress.configure(value=0)
            self.mini_player.update()
            return

        try:
            if self.paused:
                current = self.last_progress_position
            elif pygame.mixer.music.get_busy():
                current = max(0.0, pygame.mixer.music.get_pos() / 1000)
                if current > 0:
                    self.last_progress_position = current
                else:
                    current = self.last_progress_position
            else:
                if not self.stop_requested:
                    self.next_song(auto_advance=True)
                return

            self.time_label.config(text=f"{format_seconds(current)} / {format_seconds(self.song_length)}")
            self.song_progress["value"] = current
            self.update_karaoke(current)
            self.save_app_state()
            self.mini_player.update()
            self.progress_job = self.root.after(500, self.update_music_progress)
        except Exception as exc:
            self.log(f"[!] Progress error: {exc}")

    def on_close(self):
        self.shutdown_event.set()
        self.save_app_state()

        try:
            if self.current_download_process is not None:
                self.current_download_process.terminate()
        except Exception:
            pass

        try:
            if self.progress_job is not None:
                self.root.after_cancel(self.progress_job)
        except Exception:
            pass

        try:
            if self.cover_animation_job is not None:
                self.root.after_cancel(self.cover_animation_job)
        except Exception:
            pass

        try:
            if self.audio_ready:
                pygame.mixer.music.stop()
                pygame.mixer.quit()
        except Exception:
            pass

        try:
            self.db.close()
        except Exception:
            pass

        self.root.destroy()

    def update_stats_label(self):
        self.stats_label.config(
            text=(
                f"Songs: {self.stats_data['songs']} | "
                f"GB: {self.stats_data['gb']:.2f} | "
                f"Minutes saved: {self.stats_data['time_saved']}"
            )
        )

    def on_close(self):
        self.shutdown_event.set()

        try:
            if self.current_download_process is not None:
                self.current_download_process.terminate()
        except Exception:
            pass

        try:
            if self.progress_job is not None:
                self.root.after_cancel(self.progress_job)
        except Exception:
            pass

        try:
            if self.audio_ready:
                pygame.mixer.music.stop()
                pygame.mixer.quit()
        except Exception:
            pass

        try:
            self.db.close()
        except Exception:
            pass

        self.root.destroy()

    def set_main_view(self, view_name, playlist_id=None):
        self.current_view = view_name
        self.current_playlist_id = playlist_id
        self.refresh_library_view()

    def build_ui(self):
        self.setup_styles()
        self.current_view = "library"
        self.current_playlist_id = None
        self.current_table_items = {}

        self.sidebar = tk.Frame(self.root, bg="#0d0d0d", width=270)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        self.sidebar_logo = tk.Frame(self.sidebar, bg="#0d0d0d")
        self.sidebar_logo.pack(fill="x", padx=20, pady=(22, 16))
        tk.Label(self.sidebar_logo, text="USBIFY", fg=SPOTIFY_GREEN, bg="#0d0d0d", font=("Segoe UI", 27, "bold")).pack(anchor="w")
        tk.Label(self.sidebar_logo, text="Desktop music premium", fg=SPOTIFY_MUTED, bg="#0d0d0d", font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))

        self.sidebar_nav = tk.Frame(self.sidebar, bg="#0d0d0d")
        self.sidebar_nav.pack(fill="x", padx=16, pady=(0, 14))
        self.nav_library = self.create_sidebar_nav_item("Biblioteca")
        self.nav_queue = self.create_sidebar_nav_item("Cola")
        self.nav_favorites = self.create_sidebar_nav_item("Favoritos")
        self.nav_history = self.create_sidebar_nav_item("Videos")
        self.nav_library.bind("<Button-1>", lambda _e: self.set_main_view("library"))
        self.nav_queue.bind("<Button-1>", lambda _e: self.set_main_view("queue"))
        self.nav_favorites.bind("<Button-1>", lambda _e: self.set_main_view("favorites"))
        self.nav_history.bind("<Button-1>", lambda _e: self.set_main_view("history"))

        self.online_search_entry = tk.Entry(self.sidebar, bg=SPOTIFY_PANEL, fg="white", relief="flat", font=("Segoe UI", 11), insertbackground="white", bd=10)
        self.online_search_entry.pack(fill="x", padx=18, pady=(0, 8))
        self.search_button = tk.Button(self.sidebar, text="SEARCH MUSIC", command=self.search_online_music, font=("Segoe UI", 10, "bold"), pady=7)
        self.search_button.pack(fill="x", padx=18, pady=(0, 8))
        self.style_action_button(self.search_button, primary=True)

        self.url_entry = tk.Entry(self.sidebar, bg=SPOTIFY_PANEL, fg="white", relief="flat", font=("Segoe UI", 10), insertbackground="white", bd=10)
        self.url_entry.pack(fill="x", padx=18, pady=(0, 8))
        self.mode_frame = tk.Frame(self.sidebar, bg="#0d0d0d")
        self.mode_frame.pack(fill="x", padx=18, pady=(2, 10))
        self.mode_var = tk.StringVar(value="audio")
        self.audio_mode = tk.Radiobutton(self.mode_frame, text="MP3", variable=self.mode_var, value="audio", bg="#0d0d0d", fg="white", selectcolor=SPOTIFY_PANEL, activebackground="#0d0d0d", activeforeground="white")
        self.audio_mode.pack(side="left")
        self.video_mode = tk.Radiobutton(self.mode_frame, text="MP4", variable=self.mode_var, value="video", bg="#0d0d0d", fg="white", selectcolor=SPOTIFY_PANEL, activebackground="#0d0d0d", activeforeground="white")
        self.video_mode.pack(side="left", padx=(10, 0))
        self.quality_var = tk.StringVar(value="1080p")
        self.quality_menu = ttk.Combobox(self.sidebar, textvariable=self.quality_var, values=["1080p", "720p", "480p", "Best"], state="readonly")
        self.quality_menu.pack(fill="x", padx=18, pady=(0, 10))

        self.download_button = tk.Button(self.sidebar, text="DOWNLOAD TO USB", command=self.start_download, font=("Segoe UI", 10, "bold"), pady=8)
        self.download_button.pack(fill="x", padx=18, pady=(0, 8))
        self.style_action_button(self.download_button, primary=True)
        self.resume_button = tk.Button(self.sidebar, text="RESUME", command=self.resume_last_download, font=("Segoe UI", 10, "bold"), pady=7)
        self.resume_button.pack(fill="x", padx=18, pady=(0, 8))
        self.style_action_button(self.resume_button)
        self.scan_button = tk.Button(self.sidebar, text="SCAN", command=self.start_library_scan, font=("Segoe UI", 10, "bold"), pady=7)
        self.scan_button.pack(fill="x", padx=18, pady=(0, 8))
        self.style_action_button(self.scan_button)
        self.sync_button = tk.Button(self.sidebar, text="SYNC USB", command=self.start_usb_sync, font=("Segoe UI", 10, "bold"), pady=7)
        self.sync_button.pack(fill="x", padx=18, pady=(0, 8))
        self.style_action_button(self.sync_button)
        self.dedupe_button = tk.Button(self.sidebar, text="SHA256 DEDUPE", command=self.start_duplicate_scan, font=("Segoe UI", 10, "bold"), pady=7)
        self.dedupe_button.pack(fill="x", padx=18, pady=(0, 8))
        self.style_action_button(self.dedupe_button)
        self.mini_button = tk.Button(self.sidebar, text="MINI PLAYER", command=lambda: self.mini_player.toggle(), font=("Segoe UI", 10, "bold"), pady=7)
        self.mini_button.pack(fill="x", padx=18, pady=(0, 14))
        self.style_action_button(self.mini_button)

        self.options_frame = tk.Frame(self.sidebar, bg="#0d0d0d")
        self.options_frame.pack(fill="x", padx=18, pady=(0, 8))
        self.auto_delete_duplicates_check = tk.Checkbutton(self.options_frame, text="Auto delete duplicates", variable=self.auto_delete_duplicates_var, bg="#0d0d0d", fg="white", selectcolor=SPOTIFY_PANEL, activebackground="#0d0d0d", activeforeground="white")
        self.auto_delete_duplicates_check.pack(anchor="w")
        self.auto_sync_check = tk.Checkbutton(self.options_frame, text="Auto sync USB", variable=self.auto_sync_var, bg="#0d0d0d", fg="white", selectcolor=SPOTIFY_PANEL, activebackground="#0d0d0d", activeforeground="white")
        self.auto_sync_check.pack(anchor="w")
        self.listen_download_check = tk.Checkbutton(self.options_frame, text="Listen while download", variable=self.listen_while_download_var, bg="#0d0d0d", fg="white", selectcolor=SPOTIFY_PANEL, activebackground="#0d0d0d", activeforeground="white")
        self.listen_download_check.pack(anchor="w")
        self.auto_resume_check = tk.Checkbutton(self.options_frame, text="Auto resume", variable=self.auto_resume_downloads_var, bg="#0d0d0d", fg="white", selectcolor=SPOTIFY_PANEL, activebackground="#0d0d0d", activeforeground="white")
        self.auto_resume_check.pack(anchor="w")
        self.gaming_mode_check = tk.Checkbutton(self.options_frame, text="Gaming mode no notifications", variable=self.gaming_mode_var, bg="#0d0d0d", fg="white", selectcolor=SPOTIFY_PANEL, activebackground="#0d0d0d", activeforeground="white")
        self.gaming_mode_check.pack(anchor="w")

        tk.Label(self.sidebar, text="Playlists", fg=SPOTIFY_MUTED, bg="#0d0d0d", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=20, pady=(10, 6))
        self.sidebar_playlist_listbox = tk.Listbox(self.sidebar, bg="#121212", fg="white", selectbackground="#282828", selectforeground="white", relief="flat", bd=0, highlightthickness=0, font=("Segoe UI", 10), height=8)
        self.sidebar_playlist_listbox.pack(fill="x", padx=18, pady=(0, 12))
        tk.Label(self.sidebar, text="Escuchado recientemente", fg=SPOTIFY_MUTED, bg="#0d0d0d", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=20, pady=(0, 6))
        self.sidebar_recent_listbox = tk.Listbox(self.sidebar, bg="#121212", fg="white", selectbackground=SPOTIFY_GREEN, selectforeground="black", relief="flat", bd=0, highlightthickness=0, font=("Segoe UI", 9), height=7)
        self.sidebar_recent_listbox.pack(fill="both", expand=True, padx=18, pady=(0, 16))

        self.main_content = tk.Frame(self.root, bg=SPOTIFY_BG)
        self.main_content.pack(side="left", fill="both", expand=True)
        self.main_content.pack_propagate(False)
        self.playlist_title = tk.Label(self.main_content, text="Spotify Desktop Style Library", fg="white", bg=SPOTIFY_BG, font=("Segoe UI", 27, "bold"))
        self.playlist_title.pack(anchor="w", padx=28, pady=(22, 10))

        self.top_search_row = tk.Frame(self.main_content, bg=SPOTIFY_BG)
        self.top_search_row.pack(fill="x", padx=28, pady=(0, 12))
        self.library_search_entry = tk.Entry(self.top_search_row, bg="#1a1a1a", fg="white", relief="flat", font=("Segoe UI", 11), insertbackground="white", bd=10)
        self.library_search_entry.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.top_download_button = tk.Button(self.top_search_row, text="SEARCH + DOWNLOAD", command=self.download_from_top_search, font=("Segoe UI", 9, "bold"), padx=14, pady=7)
        self.top_download_button.pack(side="left", padx=(0, 10))
        self.style_action_button(self.top_download_button, primary=True)
        self.top_search_hint = tk.Label(self.top_search_row, text="Search library or download new music", fg=SPOTIFY_MUTED, bg=SPOTIFY_BG, font=("Segoe UI", 10))
        self.top_search_hint.pack(side="left")

        self.header_card = tk.Frame(self.main_content, bg="#1a1a1a", height=240)
        self.header_card.pack(fill="x", padx=28, pady=(0, 14))
        self.header_card.pack_propagate(False)
        self.hero_glow_left = tk.Frame(self.header_card, bg="#1f2d1f", width=18)
        self.hero_glow_left.pack(side="left", fill="y")
        self.hero_glow_right = tk.Frame(self.header_card, bg="#1b1f2d", width=18)
        self.hero_glow_right.pack(side="right", fill="y")
        self.hero_cover_frame = tk.Frame(self.header_card, bg=SPOTIFY_GREEN, width=200, height=200)
        self.hero_cover_frame.pack(side="left", padx=24, pady=20)
        self.hero_cover_frame.pack_propagate(False)
        self.hero_cover_label = tk.Label(self.hero_cover_frame, text="♪", fg="black", bg=SPOTIFY_GREEN, font=("Segoe UI", 44, "bold"))
        self.hero_cover_label.pack(fill="both", expand=True)
        self.hero_text_frame = tk.Frame(self.header_card, bg="#1a1a1a")
        self.hero_text_frame.pack(side="left", fill="both", expand=True, padx=(0, 16), pady=24)
        self.header_subtitle_label = tk.Label(self.hero_text_frame, text="USB LIBRARY", fg=SPOTIFY_MUTED, bg="#1a1a1a", font=("Segoe UI", 10, "bold"))
        self.header_subtitle_label.pack(anchor="w")
        self.header_title_label = tk.Label(self.hero_text_frame, text="Your Music", fg="white", bg="#1a1a1a", font=("Segoe UI", 32, "bold"))
        self.header_title_label.pack(anchor="w", pady=(8, 10))
        self.header_meta_label = tk.Label(self.hero_text_frame, text="USBIFY • 0 canciones • 00:00", fg=SPOTIFY_MUTED, bg="#1a1a1a", font=("Segoe UI", 10))
        self.header_meta_label.pack(anchor="w")
        self.hero_right_panel = tk.Frame(self.header_card, bg="#1a1a1a", width=230)
        self.hero_right_panel.pack(side="right", fill="y", padx=20, pady=20)
        self.hero_right_panel.pack_propagate(False)
        self.hero_right_title = tk.Label(self.hero_right_panel, text="Now Playing", fg="white", bg="#1a1a1a", font=("Segoe UI", 12, "bold"))
        self.hero_right_title.pack(anchor="w")
        self.artist_logo_frame = tk.Frame(self.hero_right_panel, bg=SPOTIFY_GREEN, width=54, height=54)
        self.artist_logo_frame.pack(anchor="w", pady=(12, 6))
        self.artist_logo_frame.pack_propagate(False)
        self.artist_logo_label = tk.Label(self.artist_logo_frame, text="U", fg="black", bg=SPOTIFY_GREEN, font=("Segoe UI", 18, "bold"))
        self.artist_logo_label.pack(fill="both", expand=True)
        self.artist_name_label = tk.Label(self.hero_right_panel, text="USBIFY Artist", fg=SPOTIFY_MUTED, bg="#1a1a1a", font=("Segoe UI", 9, "bold"))
        self.artist_name_label.pack(anchor="w")
        self.library_panel = tk.Frame(self.main_content, bg=SPOTIFY_PANEL)
        self.library_panel.pack(fill="both", expand=True, padx=28, pady=(0, 10))
        self.library_actions = tk.Frame(self.library_panel, bg=SPOTIFY_PANEL)
        # Action buttons moved to the right-click song menu.
        self.library_play_button = tk.Button(self.library_actions, text="PLAY", command=self.play_selected_media, font=("Segoe UI", 9, "bold"), padx=16, pady=8)
        self.library_play_button.pack(side="left", padx=(0, 8))
        self.style_action_button(self.library_play_button, primary=True)
        self.add_queue_button = tk.Button(self.library_actions, text="ADD TO QUEUE", command=self.add_selected_to_queue, font=("Segoe UI", 9, "bold"), padx=16, pady=8)
        self.add_queue_button.pack(side="left", padx=(0, 8))
        self.style_action_button(self.add_queue_button)
        self.favorite_selected_button = tk.Button(self.library_actions, text="HEART", command=self.toggle_selected_favorite, font=("Segoe UI", 9, "bold"), padx=16, pady=8)
        self.favorite_selected_button.pack(side="left", padx=(0, 8))
        self.style_action_button(self.favorite_selected_button)
        self.playlist_target_var = tk.StringVar(value="")
        self.playlist_target_menu = ttk.Combobox(self.library_actions, textvariable=self.playlist_target_var, state="readonly", width=16)
        self.playlist_target_menu.pack(side="left", padx=(0, 8))
        self.add_playlist_button = tk.Button(self.library_actions, text="ADD TO PLAYLIST", command=self.add_selected_to_playlist, font=("Segoe UI", 9, "bold"), padx=16, pady=8)
        self.add_playlist_button.pack(side="left", padx=(0, 8))
        self.style_action_button(self.add_playlist_button)
        self.refresh_button = tk.Button(self.library_actions, text="REFRESH", command=self.refresh_library_view, font=("Segoe UI", 9, "bold"), padx=16, pady=8)
        self.refresh_button.pack(side="left")
        self.style_action_button(self.refresh_button)

        self.song_table_frame = tk.Frame(self.library_panel, bg=SPOTIFY_PANEL)
        self.song_table_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.song_table_scroll = ttk.Scrollbar(self.song_table_frame, orient="vertical")
        self.song_table_scroll.pack(side="right", fill="y")
        self.song_table = ttk.Treeview(self.song_table_frame, style="Spotify.Treeview", columns=("num", "title", "album", "added", "duration"), show="headings", yscrollcommand=self.song_table_scroll.set, selectmode="browse")
        self.song_table.pack(side="left", fill="both", expand=True)
        self.song_table_scroll.config(command=self.song_table.yview)
        self.song_table.heading("num", text="#")
        self.song_table.heading("title", text="Título")
        self.song_table.heading("album", text="Álbum")
        self.song_table.heading("added", text="Fecha añadida")
        self.song_table.heading("duration", text="Duración")
        self.song_table.column("num", width=42, anchor="center", stretch=False)
        self.song_table.column("title", width=500, anchor="w")
        self.song_table.column("album", width=220, anchor="w")
        self.song_table.column("added", width=120, anchor="center", stretch=False)
        self.song_table.column("duration", width=85, anchor="center", stretch=False)
        self.song_context_menu = tk.Menu(self.root, tearoff=0, bg=SPOTIFY_PANEL, fg="white", activebackground=SPOTIFY_GREEN, activeforeground="black")

        self.console = scrolledtext.ScrolledText(self.main_content, bg="#0d0d0d", fg=SPOTIFY_GREEN, relief="flat", font=("Consolas", 9), height=4, bd=0)
        self.console.pack(fill="x", padx=28, pady=(0, 8))
        self.progress_bar = ttk.Progressbar(self.main_content, mode="indeterminate", style="Spotify.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x", padx=28, pady=(0, 8))
        self.stats_label = tk.Label(self.main_content, text="Songs: 0 | GB: 0 | Minutes saved: 0", fg=SPOTIFY_GREEN, bg=SPOTIFY_BG, font=("Segoe UI", 10, "bold"))
        self.stats_label.pack(anchor="w", padx=30, pady=(0, 4))
        self.status_label = tk.Label(self.main_content, text="Ready", fg=SPOTIFY_MUTED, bg=SPOTIFY_BG, font=("Segoe UI", 10))
        self.status_label.pack(anchor="w", padx=30, pady=(0, 10))

        self.bottom_player = tk.Frame(self.root, bg="#090909", height=118)
        self.bottom_player.place(relx=0, rely=1.0, relwidth=1, anchor="sw", height=118)
        self.bottom_player.lift()
        self.bottom_player.pack_propagate(False)
        self.song_info_frame = tk.Frame(self.bottom_player, bg="#090909")
        self.song_info_frame.pack(side="left", padx=18)
        self.cover_frame = tk.Frame(self.song_info_frame, bg=SPOTIFY_GREEN, width=68, height=68)
        self.cover_frame.pack(side="left", pady=18)
        self.cover_frame.pack_propagate(False)
        self.cover_label = tk.Label(self.cover_frame, text="♪", fg="black", bg=SPOTIFY_GREEN, font=("Segoe UI", 20, "bold"))
        self.cover_label.pack(fill="both", expand=True)
        self.cover_image = None
        self.song_text_frame = tk.Frame(self.song_info_frame, bg="#090909")
        self.song_text_frame.pack(side="left", padx=12)
        self.now_playing_label = tk.Label(self.song_text_frame, text="No music playing", fg="white", bg="#090909", font=("Segoe UI", 10, "bold"))
        self.now_playing_label.pack(anchor="w")
        self.artist_label = tk.Label(self.song_text_frame, text="USBIFY Player", fg=SPOTIFY_MUTED, bg="#090909", font=("Segoe UI", 8))
        self.artist_label.pack(anchor="w")
        self.time_label = tk.Label(self.song_text_frame, text="00:00 / 00:00", fg=SPOTIFY_MUTED, bg="#090909", font=("Segoe UI", 8))
        self.time_label.pack(anchor="w")
        self.favorite_button = tk.Button(self.song_info_frame, text="♡", command=self.toggle_current_favorite, font=("Segoe UI", 18, "bold"))
        self.favorite_button.pack(side="left", padx=(6, 0))
        self.style_action_button(self.favorite_button)
        self.favorite_button.config(bg="#090909", fg=SPOTIFY_GREEN, activebackground="#171717", activeforeground=SPOTIFY_GREEN)

        self.center_controls = tk.Frame(self.bottom_player, bg="#090909")
        self.center_controls.pack(side="left", expand=True, fill="both", padx=18)
        self.controls_row = tk.Frame(self.center_controls, bg="#090909")
        self.controls_row.pack(pady=(10, 4))
        self.shuffle_button = tk.Button(self.controls_row, text="SHUFFLE", command=self.play_random_song, font=("Segoe UI", 9, "bold"), padx=14, pady=6)
        self.shuffle_button.grid(row=0, column=0, padx=4)
        self.style_action_button(self.shuffle_button)
        self.prev_button = tk.Button(self.controls_row, text="PREVIOUS", command=self.play_random_song, font=("Segoe UI", 9, "bold"), padx=14, pady=6)
        self.prev_button.grid(row=0, column=1, padx=4)
        self.style_action_button(self.prev_button)
        self.play_button = tk.Button(self.controls_row, text="PLAY", command=self.play_selected_media, font=("Segoe UI", 10, "bold"), padx=18, pady=8)
        self.play_button.grid(row=0, column=2, padx=4)
        self.style_action_button(self.play_button, primary=True)
        self.pause_button = tk.Button(self.controls_row, text="PAUSE", command=self.pause_song, font=("Segoe UI", 9, "bold"), padx=14, pady=6)
        self.pause_button.grid(row=0, column=3, padx=4)
        self.style_action_button(self.pause_button)
        self.next_button = tk.Button(self.controls_row, text="NEXT", command=self.next_song, font=("Segoe UI", 9, "bold"), padx=14, pady=6)
        self.next_button.grid(row=0, column=4, padx=4)
        self.style_action_button(self.next_button)
        self.repeat_button = tk.Button(self.controls_row, text="REPEAT", command=self.play_random_song, font=("Segoe UI", 9, "bold"), padx=14, pady=6)
        self.repeat_button.grid(row=0, column=5, padx=4)
        self.style_action_button(self.repeat_button)
        self.song_progress = ttk.Progressbar(self.center_controls, mode="determinate", maximum=100, value=0, style="Spotify.Horizontal.TProgressbar")
        self.song_progress.pack(fill="x", pady=(6, 8))

        self.volume_frame = tk.Frame(self.bottom_player, bg="#090909")
        self.volume_frame.pack(side="right", padx=20)
        self.volume_label = tk.Label(self.volume_frame, text="VOL", fg="white", bg="#090909", font=("Segoe UI", 10, "bold"))
        self.volume_label.pack(side="left")
        self.volume_slider = tk.Scale(self.volume_frame, from_=0, to=100, orient="horizontal", bg="#090909", fg="white", troughcolor="#333333", highlightthickness=0, length=120, command=self.set_volume)
        self.volume_slider.set(70)
        self.volume_slider.pack(side="left", padx=10)

        if self.audio_ready:
            pygame.mixer.music.set_volume(0.7)

        self.apply_hero_ambience()
        self.update_artist_logo("USBIFY Artist")
        self.update_header_summary()
        self.refresh_recent_sidebar()
        self.start_background_animation()


    def open_video_player(self, video_path):
        if not Path(video_path).exists():
            self.log("[!] Video not found")
            return

        try:
            if self.video_window and self.video_window.winfo_exists():
                self.video_window.destroy()
        except Exception:
            pass

        self.video_window = tk.Toplevel(self.root)
        self.video_window.title("USBIFY VIDEO PLAYER")
        self.video_window.geometry("1000x700")
        self.video_window.configure(bg="black")

        video_frame = tk.Frame(self.video_window, bg="black")
        video_frame.pack(fill="both", expand=True)

        self.video_window.update()

        media = self.video_instance.media_new(str(video_path))
        self.video_player.set_media(media)

        handle = video_frame.winfo_id()

        if os.name == "nt":
            self.video_player.set_hwnd(handle)
        else:
            self.video_player.set_xwindow(handle)

        self.video_player.play()

        controls = tk.Frame(self.video_window, bg="#111111")
        controls.pack(fill="x")

        tk.Button(
            controls,
            text="PLAY",
            command=self.video_player.play,
            bg="#1ed760",
            fg="black",
            relief="flat",
        ).pack(side="left", padx=5, pady=5)

        tk.Button(
            controls,
            text="PAUSE",
            command=self.video_player.pause,
            bg="#333333",
            fg="white",
            relief="flat",
        ).pack(side="left", padx=5, pady=5)

        tk.Button(
            controls,
            text="STOP",
            command=self.video_player.stop,
            bg="#333333",
            fg="white",
            relief="flat",
        ).pack(side="left", padx=5, pady=5)

    def play_selected_media(self):
        item = self.get_selected_library_item()

        if not item:
            return

        path = item["path"]

        if str(path).lower().endswith(".mp4"):
            self.play_video_fullscreen(path)
        else:
            self.play_selected_song()


    def play_video_inside_panel(self, video_path):
        try:
            self.video_panel.update()

            media = self.video_instance.media_new(str(video_path))
            self.video_player.set_media(media)

            handle = self.video_panel.winfo_id()

            if os.name == "nt":
                self.video_player.set_hwnd(handle)
            else:
                self.video_player.set_xwindow(handle)

            self.video_player.play()

            self.log(f"[+] Playing embedded video: {Path(video_path).name}")

        except Exception as exc:
            self.log(f"[!] Video error: {exc}")



    def open_video_fullscreen(self, video_path):
        try:
            fullscreen_window = tk.Toplevel(self.root)
            fullscreen_window.attributes("-fullscreen", True)
            fullscreen_window.configure(bg="black")

            frame = tk.Frame(fullscreen_window, bg="black")
            frame.pack(fill="both", expand=True)

            fullscreen_window.update()

            media = self.video_instance.media_new(str(video_path))
            player = self.video_instance.media_player_new()
            player.set_media(media)

            handle = frame.winfo_id()

            if os.name == "nt":
                player.set_hwnd(handle)
            else:
                player.set_xwindow(handle)

            player.play()

            fullscreen_window.bind("<Escape>", lambda e: fullscreen_window.destroy())

        except Exception as exc:
            self.log(f"[!] Fullscreen error: {exc}")



    def get_video_library_items(self):
        items = []
        for item in getattr(self, "library_items", []):
            path = str(item.get("path", "")).lower()
            if path.endswith(".mp4"):
                items.append(item)
        return items

    
    def show_videos_only(self):
        try:
            self.usb_listbox.delete(*self.usb_listbox.get_children())

            videos = []

            for item in getattr(self, "library_items", []):
                path = str(item.get("path", "")).lower()

                if path.endswith(".mp4"):
                    videos.append(item)

            self.filtered_video_items = videos

            for idx, item in enumerate(videos, start=1):
                title = item.get("title", Path(item.get("path", "")).stem)
                artist = item.get("artist", "Unknown artist")
                duration = item.get("duration", "00:00")

                self.usb_listbox.insert(
                    "",
                    "end",
                    values=(
                        idx,
                        f"■  {title}  ·  {artist}",
                        "MP4 Video",
                        item.get("date_added", ""),
                        duration,
                    ),
                )

            self.title_label.config(text="Videos")

            self.log(f"[+] Loaded {len(videos)} MP4 videos")

        except Exception as exc:
            self.log(f"[!] Video library error: {exc}")


    
    def play_video_fullscreen(self, video_path):
        try:
            fullscreen = tk.Toplevel(self.root)
            fullscreen.attributes("-fullscreen", True)
            fullscreen.configure(bg="black")

            top_bar = tk.Frame(fullscreen, bg="#111111", height=40)
            top_bar.pack(fill="x", side="top")

            info_frame = tk.Frame(top_bar, bg="#111111")
            info_frame.pack(side="top", pady=5)

            time_label = tk.Label(
                info_frame,
                text="00:00/00:00",
                fg="white",
                bg="#111111",
                font=("Segoe UI", 12, "bold")
            )
            time_label.pack(side="left", padx=(0, 25))

            volume_label = tk.Label(
                info_frame,
                text="VOL 100",
                fg="#00ff66",
                bg="#111111",
                font=("Segoe UI", 11, "bold")
            )
            volume_label.pack(side="left")

            frame = tk.Frame(fullscreen, bg="black")
            frame.pack(fill="both", expand=True)

            fullscreen.update()

            instance = vlc.Instance("--avcodec-hw=none", "--vout=directdraw")
            player = instance.media_player_new()

            media = instance.media_new(str(video_path))
            player.set_media(media)

            handle = frame.winfo_id()

            if os.name == "nt":
                player.set_hwnd(handle)
            else:
                player.set_xwindow(handle)

            player.play()

            saved_position = self.video_positions.get(video_path, 0)

            if saved_position > 0:
                player.set_time(saved_position)

            def update_time():
                try:
                    current_ms = max(player.get_time(), 0)
                    total_ms = max(player.get_length(), 0)

                    self.video_positions[video_path] = current_ms

                    current_sec = current_ms // 1000
                    total_sec = total_ms // 1000

                    current_text = f"{current_sec//60:02}:{current_sec%60:02}"
                    total_text = f"{total_sec//60:02}:{total_sec%60:02}"

                    time_label.config(text=f"{current_text}/{total_text}")

                    fullscreen.after(500, update_time)
                except:
                    pass

            update_time()

            def forward_10(event=None):
                player.set_time(player.get_time() + 10000)

            def back_10(event=None):
                player.set_time(max(player.get_time() - 10000, 0))

            fullscreen.bind("<Right>", forward_10)
            fullscreen.bind("<Left>", back_10)

            def toggle_pause(event=None):
                if player.is_playing():
                    player.pause()
                else:
                    player.play()

            fullscreen.bind("<space>", toggle_pause)

            current_volume = 100
            player.audio_set_volume(current_volume)

            def volume_up(event=None):
                nonlocal current_volume
                current_volume = min(current_volume + 5, 200)
                player.audio_set_volume(current_volume)
                volume_label.config(text=f"VOL {current_volume}")

            def volume_down(event=None):
                nonlocal current_volume
                current_volume = max(current_volume - 5, 0)
                player.audio_set_volume(current_volume)
                volume_label.config(text=f"VOL {current_volume}")

            fullscreen.bind("<Up>", volume_up)
            fullscreen.bind("<Down>", volume_down)

            def close_video(event=None):
                try:
                    self.video_positions[video_path] = player.get_time()
                    player.stop()
                except:
                    pass

                fullscreen.destroy()

            fullscreen.bind("<Escape>", close_video)


        except Exception as exc:
            self.log(f"[!] Fullscreen video error: {exc}")

    def bind_events(self):
        if hasattr(self, "song_table"):
            self.song_table.bind("<Double-Button-1>", lambda _: self.play_selected_song())
            self.song_table.bind("<Delete>", lambda _: self.handle_song_table_delete())
            self.song_table.bind("<Button-3>", self.show_song_context_menu)
        if hasattr(self, "sidebar_playlist_listbox"):
            self.sidebar_playlist_listbox.bind("<<ListboxSelect>>", lambda _: self.open_sidebar_playlist())
        if hasattr(self, "sidebar_recent_listbox"):
            self.sidebar_recent_listbox.bind("<Double-Button-1>", lambda e: self.play_selected_recent_sidebar_song())
        if hasattr(self, "library_search_entry"):
            self.library_search_entry.bind("<KeyRelease>", self.schedule_search_refresh)
            self.library_search_entry.bind("<Return>", lambda _e: self.download_from_top_search())

    def download_from_top_search(self):
        query = self.library_search_entry.get().strip()
        if not query:
            self.log("[!] Type a song name in the top search bar")
            return

        self.url_entry.delete(0, tk.END)
        self.url_entry.insert(0, f"ytsearch1:{query}")
        self.online_search_entry.delete(0, tk.END)
        self.online_search_entry.insert(0, query)
        self.mode_var.set("audio")
        self.log(f"[+] Searching and downloading: {query}")
        self.start_download()

    def add_selected_to_playlist_by_id(self, playlist_id, playlist_name):
        item = self.get_selected_library_item()
        if not item:
            return
        self.db.add_song_to_playlist(playlist_id, item["path"])
        self.refresh_selected_playlist_songs()
        self.log(f"[+] Added to playlist {playlist_name}: {self.format_song_display(item)}")

    def show_song_context_menu(self, event):
        row_id = self.song_table.identify_row(event.y)
        if row_id:
            self.song_table.selection_set(row_id)
            self.song_table.focus(row_id)

        item = self.get_selected_library_item()
        if not item:
            return

        menu = self.song_context_menu
        menu.delete(0, tk.END)
        menu.add_command(label="Play", command=self.play_selected_media)
        menu.add_command(label="Add to queue", command=self.add_selected_to_queue)
        menu.add_command(label="Heart / Unheart", command=self.toggle_selected_favorite)

        playlist_menu = tk.Menu(menu, tearoff=0, bg=SPOTIFY_PANEL, fg="white", activebackground=SPOTIFY_GREEN, activeforeground="black")
        playlists = self.db.list_playlists()
        if playlists:
            for playlist in playlists:
                playlist_menu.add_command(
                    label=playlist["name"],
                    command=lambda pid=playlist["id"], name=playlist["name"]: self.add_selected_to_playlist_by_id(pid, name),
                )
        else:
            playlist_menu.add_command(label="No playlists", state="disabled")
        menu.add_cascade(label="Add to playlist", menu=playlist_menu)

        if self.current_view == "queue":
            menu.add_separator()
            menu.add_command(label="Remove from queue", command=self.remove_selected_queue_song)
        menu.add_separator()
        menu.add_command(label="Refresh", command=self.refresh_library_view)

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def open_sidebar_playlist(self):
        selection = self.sidebar_playlist_listbox.curselection()
        if not selection:
            return
        playlist_name = self.sidebar_playlist_listbox.get(selection[0])
        playlist = self.get_playlist_by_name(playlist_name)
        if playlist:
            self.set_main_view("playlist", playlist["id"])

    def handle_song_table_delete(self):
        if self.current_view == "queue":
            self.remove_selected_queue_song()
        elif self.current_view == "favorites":
            item = self.get_selected_library_item()
            if item:
                self.toggle_favorite_for_path(item["path"])

    def refresh_queue_ui(self):
        if getattr(self, "current_view", "library") == "queue":
            self.refresh_library_view()

    def refresh_history_ui(self):
        self.refresh_recent_sidebar()
        if getattr(self, "current_view", "library") == "history":
            self.refresh_library_view()

    def refresh_favorites_ui(self):
        if getattr(self, "current_view", "library") == "favorites":
            self.refresh_library_view()

    def refresh_selected_playlist_songs(self):
        self.open_sidebar_playlist()

    def refresh_recent_sidebar(self):
        if not hasattr(self, "sidebar_recent_listbox"):
            return
        self.sidebar_recent_listbox.delete(0, tk.END)
        self.sidebar_recent_items = []
        for entry in self.db.list_history(limit=8):
            if Path(entry["path"]).exists():
                metadata = self.get_metadata(entry["path"])
                self.sidebar_recent_items.append(metadata)
                self.sidebar_recent_listbox.insert(
                    tk.END,
                    f"{(metadata.get('artist') or 'Artist')[:12]} • {(metadata.get('title') or Path(metadata['path']).stem)[:22]}",
                )

    def play_selected_recent_sidebar_song(self):
        selection = self.sidebar_recent_listbox.curselection()
        if not selection:
            return
        item = self.sidebar_recent_items[selection[0]]
        self.play_song_path(item["path"], source="recent")

    def play_selected_queue_song(self):
        item = self.get_selected_library_item()
        if not item:
            self.log("[!] No queued song selected")
            return
        self.play_song_path(item["path"], source="queue")

    def remove_selected_queue_song(self):
        item = self.get_selected_library_item()
        if not item:
            return
        target_path = item["path"]
        updated_queue = []
        removed = False
        for queued in self.current_queue_paths:
            if not removed and queued == target_path:
                removed = True
                continue
            updated_queue.append(queued)
        self.current_queue_paths = updated_queue
        self.db.save_queue(self.current_queue_paths)
        self.refresh_library_view()

    def play_selected_history_song(self):
        item = self.get_selected_library_item()
        if item:
            self.play_song_path(item["path"], source="history")

    def play_selected_favorite_song(self):
        item = self.get_selected_library_item()
        if item:
            self.play_song_path(item["path"], source="favorites")

    def play_selected_playlist_song(self):
        item = self.get_selected_library_item()
        if item:
            self.play_song_path(item["path"], source="playlist")

    def load_selected_playlist_to_queue(self):
        if not self.current_playlist_id:
            return
        songs = self.db.list_playlist_songs(self.current_playlist_id)
        self.current_queue_paths = [song["path"] for song in songs]
        self.db.save_queue(self.current_queue_paths)
        self.log("[+] Playlist loaded into queue")
        self.set_main_view("queue")

    def refresh_library_view(self):
        query = self.library_search_entry.get().strip()
        active_path = str(Path(self.current_song_path).resolve()) if self.current_song_path else None

        if self.current_view == "queue":
            rows = [self.get_metadata(path) for path in self.current_queue_paths if Path(path).exists()]
            title = "Playback Queue"
            hint = "Editable queue"
        elif self.current_view == "favorites":
            rows = self.db.list_favorites()
            title = "Favorites"
            hint = "Heart collection"
        elif self.current_view == "history":
            history_rows = self.db.list_history()
            rows = []
            for entry in history_rows:
                if Path(entry["path"]).exists():
                    metadata = self.get_metadata(entry["path"])
                    metadata["last_scanned"] = entry["played_at"]
                    rows.append(metadata)
            title = "Videos"
            hint = "Recently played"
        elif self.current_view == "playlist" and self.current_playlist_id:
            rows = self.db.list_playlist_songs(self.current_playlist_id)
            playlist = self.db.get_playlist(self.current_playlist_id)
            title = playlist["name"] if playlist else "Playlist"
            hint = "Playlist view"
        else:
            if query:
                rows = self.db.search_songs(query=query, usb_only=False)
            elif self.current_usb_drive:
                rows = self.db.search_songs(query="", usb_only=True, usb_drive=self.current_usb_drive)
            else:
                rows = self.db.list_recent_songs()
            title = "Your Music"
            hint = "Main library view"

        self.current_library_items = rows
        self.current_table_items = {}
        self.song_table.delete(*self.song_table.get_children())
        self.playlist_title.config(text=title)
        self.top_search_hint.config(text=hint)

        for index, row in enumerate(rows, start=1):
            iid = str(index - 1)
            tags = ("playing",) if active_path and str(Path(row["path"]).resolve()) == active_path else ()
            self.song_table.insert("", "end", iid=iid, values=self.build_song_row_values(row, index), tags=tags)
            self.current_table_items[iid] = row

        self.song_table.tag_configure("playing", foreground=SPOTIFY_GREEN)
        self.update_header_summary(rows[0] if rows else None)

    def get_selected_library_item(self):
        selection = self.song_table.selection()
        if not selection:
            return None
        return self.current_table_items.get(selection[0])

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    USBifyApp().run()
