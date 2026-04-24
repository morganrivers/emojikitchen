#!/usr/bin/env python3
"""
Emoji kitchen picker combining semantic (text→keywords) and CLIP (text→image) search.
Ranks each result in both systems independently, returns lowest rank-sum.
Only covers the ~8k images that have been cached locally (have CLIP embeddings).

Bind in i3 config:
  bindsym $mod+shift+c exec --no-startup-id ~/.local/bin/emoji-picker-combined.py
"""

import sys
import os
import re
import json
import hashlib
import shutil
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

_REPO          = Path(__file__).resolve().parent
CACHE_DIR      = _REPO / "data" / "cache"
THUMB_DIR      = CACHE_DIR / "thumbs"
WALLPAPER_PATH = CACHE_DIR / "wallpaper.png"
SOCK_PATH      = CACHE_DIR / "combined-daemon.sock"
DAEMON_PY      = _REPO / "emoji-combined-daemon.py"

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
    subprocess.Popen([sys.executable, str(DAEMON_PY)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(75):  # up to 15s — two models to load
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


# ── rofi UI ──────────────────────────────────────────────────────────────────

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


_THUMB_LIMIT = 200 * 1024 * 1024  # 200 MB

def _trim_thumb_cache():
    entries, total = [], 0
    for p in THUMB_DIR.glob("*.png"):
        st = p.stat()
        entries.append((st.st_mtime, st.st_size, p))
        total += st.st_size
    if total <= _THUMB_LIMIT:
        return
    entries.sort()
    for _, size, p in entries:
        if total <= _THUMB_LIMIT:
            break
        p.unlink(missing_ok=True)
        total -= size


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


def set_wallpaper(url, alt):
    if not HAS_PIL:
        subprocess.run(["rofi", "-e", "Pillow not installed — run: pip install Pillow"])
        return
    cached = get_thumb(url)
    if not cached:
        subprocess.run(["rofi", "-e", f"Could not get image for: {alt}"])
        return
    emoji_img = Image.open(cached).convert("RGBA")

    width, height = 1920, 1080
    try:
        out = subprocess.check_output(["xrandr", "--current"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if " connected" in line:
                m = re.search(r"(\d+)x(\d+)\+", line)
                if m:
                    width, height = int(m.group(1)), int(m.group(2))
                    break
    except Exception:
        pass

    tile_size = int(os.environ.get("EMOJI_TILE_SIZE", TILE_SIZE))
    emoji_img = emoji_img.resize((tile_size, tile_size), Image.LANCZOS)
    wallpaper = Image.new("RGBA", (width, height), "white")
    for y in range(0, height, tile_size):
        for x in range(0, width, tile_size):
            wallpaper.paste(emoji_img, (x, y), emoji_img)
    wallpaper.convert("RGB").save(WALLPAPER_PATH)

    nitrogen_cfg = Path.home() / ".config" / "nitrogen" / "bg-saved.cfg"
    if nitrogen_cfg.exists() or shutil.which("nitrogen"):
        try:
            nitrogen_cfg.parent.mkdir(parents=True, exist_ok=True)
            nitrogen_cfg.write_text(f"[xin_-1]\nfile={WALLPAPER_PATH}\nmode=5\nbgcolor=#000000\n")
            subprocess.run(["nitrogen", "--restore"], check=True, capture_output=True)
            return
        except Exception:
            pass
    try:
        subprocess.run(["feh", "--bg-fill", str(WALLPAPER_PATH)], check=True, capture_output=True)
    except Exception:
        subprocess.run(["rofi", "-e", f"Wallpaper saved but couldn't set it: {WALLPAPER_PATH}"])


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    query = rofi("emoji search (combined):")
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

    _trim_thumb_cache()


if __name__ == "__main__":
    main()
