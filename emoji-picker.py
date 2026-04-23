#!/usr/bin/env python3
"""
Visual emoji kitchen picker via rofi.
1. Rofi text prompt for search query.
2. Rofi icon grid showing matching emoji thumbnails.
3. Selection sets it as the tiled wallpaper.

Bind in i3 config:
  bindsym $mod+shift+e exec --no-startup-id python3 ~/.local/bin/emoji-picker.py
"""

import sys
import os
import re
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

CACHE_DIR = Path.home() / ".cache" / "emoji-wallpaper"
SEARCH_INDEX = CACHE_DIR / "search-index.tsv"
THUMB_DIR = CACHE_DIR / "thumbs"
WALLPAPER_PATH = CACHE_DIR / "wallpaper.png"
WALLPAPER_SCRIPT = Path.home() / ".local" / "bin" / "emoji-wallpaper.py"
SOCK_PATH = CACHE_DIR / "combined-daemon.sock"
DAEMON_PY = Path.home() / ".local" / "bin" / "emoji-combined-daemon.py"

TILE_SIZE = 200
MAX_RESULTS = 5000
BATCH_SIZE = 100
LOAD_MORE = "⬇  load more results..."


def rofi(prompt, entries_with_icons=None, text_entries=None, lines=0):
    """
    Run rofi dmenu.
      entries_with_icons: list of (label, icon_path_or_None) — shows icon grid.
      text_entries: list of plain strings — shows filterable text list.
    Returns selected label, or None if cancelled.
    """
    cmd = ["rofi", "-dmenu", "-p", prompt]
    if entries_with_icons is not None:
        cmd += [
            "-show-icons",
            "-markup-rows",
            "-theme-str", "element-icon { size: 100px; } window { location: north; anchor: north; y-offset: 0; } listview { lines: 8; }",
        ]
        stdin = ""
        for label, icon in entries_with_icons:
            if icon:
                stdin += f"{label}\0icon\x1f{icon}\n"
            else:
                stdin += f"{label}\n"
    elif text_entries is not None:
        cmd += [
            "-theme-str", "window { location: north; anchor: north; y-offset: 0; } listview { lines: 8; }",
        ]
        stdin = "\n".join(text_entries) + "\n"
    else:
        cmd += ["-lines", str(lines)]
        stdin = ""

    result = subprocess.run(cmd, input=stdin, text=True, capture_output=True)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _start_daemon():
    subprocess.Popen([str(DAEMON_PY)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                return [(r["rank"], r["alt"], r["url"], "") for r in results]
        except Exception:
            if SOCK_PATH.exists():
                SOCK_PATH.unlink()
    return None


def load_index():
    entries = []
    with open(SEARCH_INDEX) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) == 3:
                entries.append((parts[0], parts[1], parts[2]))
    return entries


def search(entries, query, limit=MAX_RESULTS):
    words = query.lower().split()
    patterns = [re.compile(r'\b' + re.escape(w) + r'\b') for w in words]
    scored = []
    for url, alt, text in entries:
        haystack = text.lower()
        score = sum(1 for p in patterns if p.search(haystack))
        if score > 0:
            scored.append((score, alt, url, text))
    scored.sort(key=lambda x: -x[0])
    if scored:
        return scored[:limit]
    # fallback: substring match
    for url, alt, text in entries:
        haystack = text.lower()
        score = sum(1 for w in words if w in haystack)
        if score > 0:
            scored.append((score, alt, url, text))
    scored.sort(key=lambda x: -x[0])
    return scored[:limit]


def _xml_escape(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def url_to_base_emojis(url):
    m = re.search(r'/u([0-9a-f]+)_u([0-9a-f]+)\.png$', url, re.IGNORECASE)
    if m:
        try:
            return chr(int(m.group(1), 16)) + chr(int(m.group(2), 16))
        except (ValueError, OverflowError):
            pass
    return ""


def format_label(alt, url, text, patterns):
    base = url_to_base_emojis(url)
    base_str = f'  {base}' if base else ''
    if not text:
        return f'{alt}{base_str}'
    parts = []
    for word in text.split():
        escaped = _xml_escape(word)
        if any(p.search(word.lower()) for p in patterns):
            parts.append(f'<b>{escaped}</b>')
        else:
            parts.append(escaped)
    return f'{alt}{base_str}  ({" ".join(parts)})'


def build_base_emoji_index(entries):
    """Return sorted list of (hex, emoji_char, name) for all unique base emojis."""
    seen = {}
    for url, alt, _text in entries:
        m = re.search(r'/u([0-9a-f]+)_u([0-9a-f]+)\.png$', url, re.IGNORECASE)
        if not m:
            continue
        hex1, hex2 = m.group(1).lower(), m.group(2).lower()
        parts = alt.split('-', 1)
        name1, name2 = (parts[0], parts[1]) if len(parts) == 2 else (alt, alt)
        for hex_code, name in [(hex1, name1), (hex2, name2)]:
            if hex_code not in seen:
                try:
                    seen[hex_code] = (chr(int(hex_code, 16)), name)
                except (ValueError, OverflowError):
                    pass
    return sorted(seen.items(), key=lambda x: x[1][1])


def pick_base_emoji(base_index, prompt):
    """Show a searchable rofi list of base emojis.
    Returns (emoji_char, search_term) or None if cancelled.
    Exact match returns the emoji char; free-form text returns empty string."""
    labels = [f"{emoji} {name}" for _, (emoji, name) in base_index]
    selected = rofi(prompt, text_entries=labels)
    if not selected:
        return None
    for _hex, (emoji, name) in base_index:
        if selected == f"{emoji} {name}":
            return (emoji, name)
    # user typed something not in the list — use it as a raw search term
    return ("", selected)


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
        import re
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


def main():
    if not SEARCH_INDEX.exists():
        subprocess.run(["rofi", "-e", "Building emoji index... run emoji-wallpaper.py first."])
        sys.exit(1)

    entries = load_index()

    while True:
        # Mode selector — Escape here exits
        mode = rofi("emoji:", text_entries=["keyword search", "combo", "semantic search (better, slow)"])
        if not mode:
            sys.exit(0)

        if mode == "combo":
            base_index = build_base_emoji_index(entries)

            first = pick_base_emoji(base_index, "first emoji:")
            if not first:
                continue  # back to start
            emoji1, term1 = first

            second = pick_base_emoji(base_index, f"second emoji (+ {emoji1}{' ' + term1 if emoji1 else term1}):")
            if not second:
                continue  # back to start
            emoji2, term2 = second

            results = search(entries, f"{term1} {term2}")
            if not results:
                rofi(f"No results for '{term1} {term2}' — press Esc", lines=0)
                continue  # back to start

            patterns = [re.compile(re.escape(term1)), re.compile(re.escape(term2))]
            query_label = f"'{term1}+{term2}'"
        elif mode == "semantic search (better, slow)":
            query = rofi("emoji search (semantic):")
            if not query:
                continue  # back to start

            results = query_daemon(query)
            if not results:
                rofi("Search daemon unavailable — press Esc", lines=0)
                continue  # back to start

            patterns = []
            query_label = f"'{query}' (semantic)"
        else:
            query = rofi("emoji search:")
            if not query:
                continue  # back to start

            results = search(entries, query)
            if not results:
                rofi(f"No results for '{query}' — press Esc", lines=0)
                continue  # back to start

            patterns = [re.compile(r'\b' + re.escape(w) + r'\b') for w in query.lower().split()]
            query_label = f"'{query}'"

        # Show results in batches — Escape goes back to start
        selected = None
        offset = 0
        while True:
            batch = results[offset:offset + BATCH_SIZE]
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
                thumbs = list(ex.map(get_thumb, [url for _, _, url, _ in batch]))

            icon_entries = [(format_label(alt, url, text, patterns), thumb)
                            for (_, alt, url, text), thumb in zip(batch, thumbs)]
            if offset + BATCH_SIZE < len(results):
                icon_entries.append((LOAD_MORE, None))

            selected = rofi(f"{query_label} ({offset+1}–{offset+len(batch)} of {len(results)}):", entries_with_icons=icon_entries)
            if not selected:
                break  # back to start (outer loop continues)
            if selected == LOAD_MORE:
                offset += BATCH_SIZE
                continue
            break

        if not selected or selected == LOAD_MORE:
            continue  # back to start

        # alt is the leading word-chars+hyphens in the label, before the base emoji pair
        m = re.match(r'^[\w-]+', selected)
        selected_alt = m.group(0) if m else selected

        # Copy selected image to clipboard
        for _, alt, url, _ in results:
            if alt == selected_alt:
                thumb = get_thumb(url)
                if thumb:
                    with open(thumb, "rb") as f:
                        subprocess.run(
                            ["xclip", "-selection", "clipboard", "-t", "image/png"],
                            stdin=f, check=True,
                        )
                break
        break  # done


if __name__ == "__main__":
    main()
