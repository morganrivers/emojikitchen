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
import random as _random
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

_REPO        = Path(__file__).resolve().parent
DATA_DIR     = _REPO / "data" / "embeddings"
CACHE_DIR    = _REPO / "data" / "cache"
# Prefer the venv interpreter for spawned subprocesses so the daemon and
# emoji-story script find their pip-installed deps even when the picker
# itself was launched with a different python.
_VENV_PY     = _REPO / ".venv" / "bin" / "python3"
_PYTHON      = str(_VENV_PY) if _VENV_PY.exists() else sys.executable
SEARCH_INDEX = DATA_DIR / "search-index.tsv"
THUMB_DIR    = CACHE_DIR / "thumbs"
WALLPAPER_PATH   = CACHE_DIR / "wallpaper.png"
WALLPAPER_SCRIPT = _REPO / "emoji-wallpaper.py"
SOCK_PATH    = CACHE_DIR / "split-daemon.sock"
DAEMON_PY    = _REPO / "emoji-split-daemon.py"
DAEMON_PID   = CACHE_DIR / "split-daemon.pid"
DAEMON_LOG   = CACHE_DIR / "split-daemon.log"
DOWNLOAD_PID = CACHE_DIR / "data-download.pid"
DOWNLOAD_LOG = CACHE_DIR / "data-download.log"
DATA_TARBALL_URL = "https://github.com/morganrivers/emojikitchen/releases/latest/download/data.tar.gz"

TILE_SIZE = 200
MAX_RESULTS = 5000
# Some combos in the index 404 on Google's CDN (e.g. dress-alarm_clock) so
# the rofi cell renders blank. Default: hide them. Flip to True if you want
# to verify nothing else is silently dropping rows.
SHOW_BROKEN_THUMBS = False
BATCH_SIZE = 100
LOAD_MORE  = "⬇  load more results..."
STORY_PY   = _REPO / "emoji-story.py"
STORY_OUT  = Path("/tmp/emoji-story.png")

# Base emojis from curated image set - boost these in keyword search results.
# Any combo containing at least one of these comes before non-priority combos
# at the same keyword score. Tiebreak within each group is a fixed random order
# seeded by the combo name so it's stable across runs.
PRIORITY_EMOJIS = frozenset({
    "100", "bird", "boom", "bouquet", "brain", "broccoli", "car", "carrot",
    "cat", "city_sunrise", "cloud", "coconut", "coffee", "computer",
    "crystal_ball", "dango", "derelict_house_building", "dragon", "earth_africa",
    "exclamation", "exploding_head", "face_with_raised_eyebrow",
    "face_with_rolling_eyes", "facepunch", "fire", "fish", "frog",
    "glass_of_milk", "goose", "headphones", "hearts", "hole", "hot_pepper",
    "house", "imp", "iphone", "koala", "last_quarter_moon_with_face", "lemon",
    "lightning", "llama", "low_battery", "magic_wand", "milky_way", "mouse",
    "musical_keyboard", "national_park", "neutral_face", "octopus", "ok",
    "parachute", "people_hugging", "pleading_face", "rain_cloud", "rainbow",
    "relieved", "shrug", "skunk", "snail", "sob", "sparkling_heart",
    "sunrise_over_mountains", "sunglasses", "sushi", "taco", "tiger", "tornado",
    "tropical_drink", "tulip", "turtle", "unicorn_face", "upside_down_face",
    "volcano", "whale", "white_check_mark", "wood", "yum",
})


def _keyword_priority(alt):
    """Secondary sort key for keyword search: (not_priority, stable_random).
    Lower is better - priority combos sort before non-priority at the same score."""
    parts = alt.split("-", 1)
    is_priority = any(p in PRIORITY_EMOJIS for p in parts)
    return (0 if is_priority else 1, _random.Random(alt).random())


def copy_image_to_clipboard(path, notify=None):
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        cmd = ["wl-copy", "--type", "image/png"]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard", "-t", "image/png"]
    else:
        subprocess.run(["rofi", "-e", "No clipboard tool found - install xclip (X11) or wl-clipboard (Wayland)"])
        return
    with open(path, "rb") as f:
        subprocess.run(cmd, stdin=f, check=True)
    if notify:
        subprocess.run(["rofi", "-e", notify])


def rofi(prompt, entries_with_icons=None, text_entries=None, lines=0):
    """
    Run rofi dmenu.
      entries_with_icons: list of (label, icon_path_or_None) - shows icon grid.
      text_entries: list of plain strings - shows filterable text list.
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


def _download_in_progress():
    if not DOWNLOAD_PID.exists():
        return False
    try:
        pid = int(DOWNLOAD_PID.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        DOWNLOAD_PID.unlink(missing_ok=True)
        return False


def _start_data_download():
    """Spawn a background curl|tar to fetch the semantic models tarball.
    Writes the bash PID to DOWNLOAD_PID so subsequent invocations can detect
    progress; the wrapper removes the pid file when extraction finishes."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_LOG.unlink(missing_ok=True)
    script = (
        f"(curl -L --fail --silent --show-error '{DATA_TARBALL_URL}' "
        f"| tar -xz -C '{_REPO}') > '{DOWNLOAD_LOG}' 2>&1; "
        f"rm -f '{DOWNLOAD_PID}'"
    )
    proc = subprocess.Popen(
        ["bash", "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    DOWNLOAD_PID.write_text(str(proc.pid))


def _daemon_alive():
    """True only if the recorded PID is a running, non-zombie emoji-split daemon.
    os.kill(pid, 0) alone returns True for zombies (dead-but-unreaped children),
    so we also read /proc/<pid>/status and cmdline."""
    if not DAEMON_PID.exists():
        return False
    try:
        pid = int(DAEMON_PID.read_text().strip())
    except (ValueError, OSError):
        DAEMON_PID.unlink(missing_ok=True)
        return False
    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.exists():
        DAEMON_PID.unlink(missing_ok=True)
        return False
    try:
        for line in (proc_dir / "status").read_text().splitlines():
            if line.startswith("State:") and "Z" in line.split(":", 1)[1]:
                DAEMON_PID.unlink(missing_ok=True)
                return False
        cmdline = (proc_dir / "cmdline").read_bytes()
        if b"emoji-split-daemon" not in cmdline:
            # PID was recycled into some unrelated process
            DAEMON_PID.unlink(missing_ok=True)
            return False
    except OSError:
        DAEMON_PID.unlink(missing_ok=True)
        return False
    return True


def _spawn_daemon():
    """Launch the daemon in the background. Returns the Popen object.
    On first run, fastembed downloads ~230 MB of models, so the daemon may
    take 30-90s before its socket appears. Stderr/stdout are tee'd to a log
    so failures are diagnosable instead of silent."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(DAEMON_LOG, "wb")
    proc = subprocess.Popen(
        [_PYTHON, str(DAEMON_PY)],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    DAEMON_PID.write_text(str(proc.pid))
    return proc


def _wait_for_socket(timeout):
    """Poll SOCK_PATH until it accepts a connection, up to `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if SOCK_PATH.exists():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(SOCK_PATH))
                s.close()
                return True
            except OSError:
                pass
        time.sleep(0.2)
    return False


def query_daemon(query, limit=MAX_RESULTS):
    """Returns a list of results, "loading" if the daemon is still warming up,
    or None if it failed to start at all."""
    if not SOCK_PATH.exists():
        if not _daemon_alive():
            _spawn_daemon()
        # Brief wait for the warm case (models cached, ~2s startup).
        # On first run this will time out and we tell the user to retry.
        if not _wait_for_socket(5):
            return "loading" if _daemon_alive() else None

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
        return "loading" if _daemon_alive() else None
    return None


def load_index():
    entries = []
    with open(SEARCH_INDEX) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) == 3:
                entries.append((parts[0], parts[1], parts[2]))
    return entries


def _score_entry(alt, words, is_single):
    """Return (square_score, name_score) for a combo alt and query words.

    square_score: 1 if this is a X-X combo and the single query word matches X.
    name_score:   count of query words that appear as tokens in the alt name.
    For 2-word queries, name_score==2 means each word matched a different
    component - the keyword equivalent of the daemon's cross-rank."""
    alt_lower = alt.lower()
    alt_tokens = set(re.split(r'[-_]', alt_lower))
    name_score = sum(1 for w in words if w in alt_tokens)
    square_score = 0
    if is_single:
        parts = alt_lower.split('-', 1)
        if len(parts) == 2 and parts[0] == parts[1]:
            if words[0] in set(parts[0].split('_')):
                square_score = 1
    return square_score, name_score


def search(entries, query, limit=MAX_RESULTS):
    words = query.lower().split()
    patterns = [re.compile(r'\b' + re.escape(w) + r'\b') for w in words]
    is_single = len(words) == 1
    scored = []
    for url, alt, text in entries:
        text_score = sum(1 for p in patterns if p.search(text.lower()))
        if text_score > 0:
            sq, ns = _score_entry(alt, words, is_single)
            scored.append((sq, ns, text_score, alt, url, text))
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2], _keyword_priority(x[3])))
    if scored:
        return [(ts, alt, url, text) for _, _, ts, alt, url, text in scored[:limit]]
    # fallback: substring match
    for url, alt, text in entries:
        text_score = sum(1 for w in words if w in text.lower())
        if text_score > 0:
            sq, ns = _score_entry(alt, words, is_single)
            scored.append((sq, ns, text_score, alt, url, text))
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2], _keyword_priority(x[3])))
    return [(ts, alt, url, text) for _, _, ts, alt, url, text in scored[:limit]]


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
    # user typed something not in the list - use it as a raw search term
    return ("", selected)


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
    """Download `url` to the thumb cache and return the path, or None on
    permanent failure. Uses a tmp file + atomic rename so a network blip
    can't leave a truncated PNG that would render as an empty grid cell on
    later runs. Retries once with a short backoff for transient failures."""
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    name = hashlib.md5(url.encode()).hexdigest() + ".png"
    path = THUMB_DIR / name
    if path.exists() and path.stat().st_size > 0:
        return str(path)
    if path.exists():
        path.unlink(missing_ok=True)
    tmp = path.with_suffix(".png.tmp")
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "emojikitchen-picker"})
            with urllib.request.urlopen(req, timeout=10) as resp, open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
            if tmp.stat().st_size > 0:
                tmp.replace(path)
                return str(path)
        except Exception:
            pass
        tmp.unlink(missing_ok=True)
        if attempt == 0:
            time.sleep(0.3)
    return None


def set_wallpaper(url, alt):
    if not HAS_PIL:
        subprocess.run(["rofi", "-e", "Pillow not installed - run: pip install Pillow"])
        return
    nitrogen_cfg = Path.home() / ".config" / "nitrogen" / "bg-saved.cfg"
    if not shutil.which("feh") and not shutil.which("nitrogen") and not nitrogen_cfg.exists():
        subprocess.run(["rofi", "-e", "Install feh or nitrogen to set wallpapers"])
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
        # Mode selector
        _has_semantic = (
            (DATA_DIR / "base-emoji-sem.npy").exists() and
            ((DATA_DIR / "embeddings.npy").exists() or
             (DATA_DIR / "embeddings-pca340.npy").exists())
        )
        _downloading = (not _has_semantic) and _download_in_progress()
        if _has_semantic:
            sem_suffix = ""
        elif _downloading:
            sem_suffix = "  [downloading...]"
        else:
            sem_suffix = "  [models not downloaded]"
        sem_label   = "semantic search (better, slow)" + sem_suffix
        story_label = "emoji story"
        modes = ["keyword search", "combo", sem_label, story_label]

        mode = rofi("emoji:", text_entries=modes)
        if not mode:
            sys.exit(0)

        if mode == story_label:
            text = rofi("story text:")
            if not text:
                continue
            subprocess.run([_PYTHON, str(STORY_PY), "--output", str(STORY_OUT), text], check=True)
            copy_image_to_clipboard(str(STORY_OUT), notify="Story copied to clipboard")
            break

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
                rofi(f"No results for '{term1} {term2}' - press Esc", lines=0)
                continue  # back to start

            exact_alts = {f"{term1}-{term2}".lower(), f"{term2}-{term1}".lower()}
            exact = [r for r in results if r[1].lower() in exact_alts]
            rest  = [r for r in results if r[1].lower() not in exact_alts]
            results = exact + rest

            patterns = [re.compile(re.escape(term1)), re.compile(re.escape(term2))]
            query_label = f"'{term1}+{term2}'"
        elif mode == sem_label:
            if not _has_semantic:
                if not _downloading:
                    _start_data_download()
                subprocess.run(["rofi", "-e",
                    "Downloading semantic models (~150 MB) in background.\n"
                    "Try search again in a minute - it'll work as soon as\n"
                    "the download finishes."])
                continue
            query = rofi("emoji search (semantic):")
            if not query:
                continue  # back to start

            results = query_daemon(query)
            if results == "loading":
                subprocess.run(["rofi", "-e",
                    "Search daemon is still loading models (first run\n"
                    "downloads ~230 MB).\n"
                    "Try the search again in a minute."])
                continue
            if not results:
                subprocess.run(["rofi", "-e",
                    f"Search daemon failed to start.\nSee {DAEMON_LOG} for details."])
                continue  # back to start

            patterns = []
            query_label = f"'{query}' (semantic)"
        else:
            # If the user typed something not in the mode list, use it directly
            if mode == "keyword search":
                query = rofi("emoji search:")
                if not query:
                    continue  # back to start
            else:
                query = mode

            results = search(entries, query)
            if not results:
                rofi(f"No results for '{query}' - press Esc", lines=0)
                continue  # back to start

            patterns = [re.compile(r'\b' + re.escape(w) + r'\b') for w in query.lower().split()]
            query_label = f"'{query}'"

        # Show results in batches - Escape goes back to start
        selected = None
        offset = 0
        while True:
            batch = results[offset:offset + BATCH_SIZE]
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
                thumbs = list(ex.map(get_thumb, [url for _, _, url, _ in batch]))

            icon_entries = [(format_label(alt, url, text, patterns), thumb)
                            for (_, alt, url, text), thumb in zip(batch, thumbs)
                            if thumb is not None or SHOW_BROKEN_THUMBS]
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
                    copy_image_to_clipboard(thumb, notify="Copied to clipboard")
                break
        _trim_thumb_cache()
        break  # done


if __name__ == "__main__":
    main()
