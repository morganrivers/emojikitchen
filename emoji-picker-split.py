#!/usr/bin/env python3
"""
Visual emoji kitchen picker — split-query search.

1-word query  → squared combo (emoji-emoji) surfaced first
2-word query  → best base-emoji cross-match surfaced first
3+ word query → standard combined search

Bind in i3 config:
  bindsym $mod+shift+s exec --no-startup-id ~/.local/bin/emoji-picker-split.py
"""

import sys
import os
import json
import shutil
import hashlib
import socket
import subprocess
import time
import urllib.request
import concurrent.futures
from pathlib import Path

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

CACHE_DIR      = Path.home() / ".cache" / "emoji-wallpaper"
THUMB_DIR      = CACHE_DIR / "thumbs"
WALLPAPER_PATH = CACHE_DIR / "wallpaper.png"
SOCK_PATH      = CACHE_DIR / "split-daemon.sock"
DAEMON_PY      = Path.home() / ".local" / "bin" / "emoji-split-daemon.py"

TILE_SIZE   = 200
MAX_RESULTS = 5000
BATCH_SIZE  = 100
LOAD_MORE   = "⬇  load more results..."


def copy_image_to_clipboard(path):
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        cmd = ["wl-copy", "--type", "image/png"]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard", "-t", "image/png"]
    else:
        subprocess.run(["rofi", "-e", "No clipboard tool found — install xclip (X11) or wl-clipboard (Wayland)"])
        return
    with open(path, "rb") as f:
        subprocess.run(cmd, stdin=f, check=True)


def _start_daemon():
    daemon = DAEMON_PY if DAEMON_PY.exists() else Path(__file__).parent / "emoji-split-daemon.py"
    subprocess.Popen([str(daemon)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):  # up to 20s — two models + index to load
        time.sleep(0.2)
        if SOCK_PATH.exists():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(SOCK_PATH))
                s.close()
                return True
            except OSError:
                pass
    return False


def query_daemon(query, limit=MAX_RESULTS):
    for attempt in range(2):
        if not SOCK_PATH.exists():
            if attempt > 0 or not _start_daemon():
                return None
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(30)
            s.connect(str(SOCK_PATH))
            s.sendall((json.dumps({"query": query, "limit": limit}) + "\n").encode())
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
                if data.endswith(b"\n"):
                    break
            s.close()
            results = json.loads(data.decode())
            if isinstance(results, list):
                return [(r["rank"], r["alt"], r["url"]) for r in results]
        except Exception:
            if SOCK_PATH.exists():
                SOCK_PATH.unlink()
    return None


def rofi(prompt, entries_with_icons=None, lines=0):
    cmd = ["rofi", "-dmenu", "-p", prompt]
    if entries_with_icons is not None:
        cmd += [
            "-show-icons",
            "-theme-str",
            "element-icon { size: 100px; } window { location: north; anchor: north; y-offset: 0; } listview { lines: 8; }",
        ]
        stdin = ""
        for label, icon in entries_with_icons:
            if icon:
                stdin += f"{label}\0icon\x1f{icon}\n"
            else:
                stdin += f"{label}\n"
    else:
        cmd += ["-lines", str(lines)]
        stdin = ""
    result = subprocess.run(cmd, input=stdin, text=True, capture_output=True)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def get_thumb(url):
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    name = hashlib.md5(url.encode()).hexdigest() + ".png"
    path = THUMB_DIR / name
    if not path.exists():
        try:
            urllib.request.urlretrieve(url, path)
        except Exception:
            return None
    return str(path)


def main():
    query = rofi("emoji search (split):")
    if not query:
        sys.exit(0)

    results = query_daemon(query)
    if not results:
        rofi("Search daemon unavailable — press Esc", lines=0)
        sys.exit(1)

    offset = 0
    while True:
        batch = results[offset:offset + BATCH_SIZE]
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            thumbs = list(ex.map(get_thumb, [url for _, _, url in batch]))

        icon_entries = [(alt, thumb) for (_, alt, _), thumb in zip(batch, thumbs)]
        has_more = offset + BATCH_SIZE < len(results)
        if has_more:
            icon_entries.append((LOAD_MORE, None))

        selected = rofi(
            f"'{query}' ({offset+1}–{offset+len(batch)} of {len(results)}):",
            entries_with_icons=icon_entries,
        )
        if not selected:
            sys.exit(0)
        if selected == LOAD_MORE:
            offset += BATCH_SIZE
            continue
        break

    for _, alt, url in results:
        if alt == selected:
            thumb = get_thumb(url)
            if thumb:
                copy_image_to_clipboard(thumb)
            break


if __name__ == "__main__":
    main()
