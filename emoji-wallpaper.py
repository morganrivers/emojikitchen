#!/usr/bin/env python3
"""
Daily emoji kitchen wallpaper setter.
Downloads a random Emoji Kitchen mashup image each day and tiles it on a white background.
"""

import os
import sys
import json
import hashlib
import datetime
import random
import shutil
import urllib.request
import subprocess
import re
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Error: Pillow is required. Install with: pip install Pillow")
    sys.exit(1)

_REPO        = Path(__file__).resolve().parent
DATA_DIR     = _REPO / "data" / "embeddings"
CACHE_DIR    = _REPO / "data" / "cache"
URL_CACHE    = DATA_DIR / "urls.txt"
SEARCH_INDEX = DATA_DIR / "search-index.tsv"
WALLPAPER_PATH = CACHE_DIR / "wallpaper.png"
THUMB_DIR    = CACHE_DIR / "thumbs"
METADATA_URL = "https://raw.githubusercontent.com/xsalazar/emoji-kitchen-backend/main/app/metadata.json"
URL_CACHE_MAX_DAYS = 365
TILE_SIZE = 200


def get_screen_size():
    try:
        out = subprocess.check_output(["xrandr", "--current"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if " connected" in line and "primary" in line:
                m = re.search(r"(\d+)x(\d+)\+", line)
                if m:
                    return int(m.group(1)), int(m.group(2))
        for line in out.splitlines():
            if " connected" in line:
                m = re.search(r"(\d+)x(\d+)\+", line)
                if m:
                    return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 1920, 1080


def build_url_cache():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print("Downloading emoji metadata (~94MB, first run only)...", flush=True)

    req = urllib.request.Request(METADATA_URL, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()

    # Handle gzip if needed
    if raw[:2] == b'\x1f\x8b':
        import gzip
        raw = gzip.decompress(raw)

    print("Parsing metadata...", flush=True)
    data = json.loads(raw)
    del raw

    emoji_data = data.get("data", {})

    # Build keyword lookup: codepoint -> list of keywords
    kw_lookup = {code: entry.get("keywords", []) for code, entry in emoji_data.items()}

    seen_urls = set()
    index_rows = []  # (url, alt, searchable_text)

    for code_a, entry_a in emoji_data.items():
        for code_b, combos in entry_a.get("combinations", {}).items():
            entry_b = emoji_data.get(code_b, {})
            kw_a = kw_lookup.get(code_a, [])
            kw_b = kw_lookup.get(code_b, [])
            cat_a = entry_a.get("category", "")
            cat_b = entry_b.get("category", "")
            for combo in combos:
                if combo.get("isLatest", False):
                    url = combo.get("gStaticUrl", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        alt = combo.get("alt", "")
                        text = " ".join([alt] + kw_a + kw_b + [cat_a, cat_b])
                        index_rows.append((url, alt, text))

    urls = [r[0] for r in index_rows]
    print(f"Found {len(urls):,} emoji combinations.", flush=True)

    with open(URL_CACHE, "w") as f:
        f.write("\n".join(urls))

    with open(SEARCH_INDEX, "w") as f:
        for url, alt, text in index_rows:
            f.write(f"{url}\t{alt}\t{text}\n")

    return urls


def load_urls():
    if URL_CACHE.exists():
        age = (datetime.date.today() - datetime.date.fromtimestamp(URL_CACHE.stat().st_mtime)).days
        if age < URL_CACHE_MAX_DAYS:
            urls = URL_CACHE.read_text().splitlines()
            if urls:
                return urls
    return build_url_cache()


def pick_daily_url(urls, force_random=False):
    if force_random:
        return random.choice(urls)
    seed = int(hashlib.sha256(datetime.date.today().isoformat().encode()).hexdigest(), 16)
    rng = random.Random(seed)
    return rng.choice(urls)


def download_emoji(url):
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    cached = THUMB_DIR / (hashlib.md5(url.encode()).hexdigest() + ".png")
    if not cached.exists():
        urllib.request.urlretrieve(url, cached)
    return Image.open(cached).convert("RGBA")


def build_wallpaper(emoji_img, width, height, tile_size=TILE_SIZE):
    # Resize emoji to tile_size x tile_size preserving aspect ratio
    emoji_img = emoji_img.resize((tile_size, tile_size), Image.LANCZOS)

    wallpaper = Image.new("RGBA", (width, height), "white")
    for y in range(0, height, tile_size):
        for x in range(0, width, tile_size):
            wallpaper.paste(emoji_img, (x, y), emoji_img)

    return wallpaper.convert("RGB")


def set_wallpaper(path):
    # nitrogen (i3, openbox, etc.) - write config directly then restore
    nitrogen_cfg = Path.home() / ".config" / "nitrogen" / "bg-saved.cfg"
    if nitrogen_cfg.exists() or shutil.which("nitrogen"):
        try:
            nitrogen_cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg_content = f"[xin_-1]\nfile={path}\nmode=5\nbgcolor=#000000\n"
            nitrogen_cfg.write_text(cfg_content)
            subprocess.run(["nitrogen", "--restore"], check=True, capture_output=True)
            return "nitrogen"
        except Exception:
            pass

    # feh fallback
    try:
        subprocess.run(["feh", "--bg-fill", str(path)], check=True, capture_output=True)
        return "feh"
    except Exception:
        pass

    # GNOME
    try:
        uri = f"file://{path}"
        env = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": _find_dbus()}
        for key in ("picture-uri", "picture-uri-dark"):
            subprocess.run(
                ["gsettings", "set", "org.gnome.desktop.background", key, uri],
                env=env, check=True, capture_output=True,
            )
        subprocess.run(
            ["gsettings", "set", "org.gnome.desktop.background", "picture-options", "stretched"],
            env=env, check=True, capture_output=True,
        )
        return "gsettings (GNOME)"
    except Exception:
        pass

    # XFCE
    try:
        monitors = subprocess.check_output(
            ["xfconf-query", "-c", "xfce4-desktop", "-l"], text=True
        ).splitlines()
        image_props = [p for p in monitors if p.endswith("/last-image")]
        if image_props:
            for prop in image_props:
                subprocess.run(
                    ["xfconf-query", "-c", "xfce4-desktop", "-p", prop, "-s", str(path)],
                    check=True, capture_output=True,
                )
            return "xfconf-query (XFCE)"
    except Exception:
        pass

    return None


def _find_dbus():
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if addr:
        return addr
    # Try to find it from a running session
    try:
        pid_file = Path(f"/run/user/{os.getuid()}/bus")
        if pid_file.exists():
            return f"unix:path={pid_file}"
    except Exception:
        pass
    return addr


def pick_from_cache():
    cached = list(THUMB_DIR.glob("*.png"))
    if not cached:
        return None
    return random.choice(cached)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    force_random = "--random" in sys.argv or "-r" in sys.argv
    urls = load_urls()
    url = pick_daily_url(urls, force_random=force_random)
    print(f"Today's emoji: {url}", flush=True)

    try:
        emoji_img = download_emoji(url)
    except Exception as e:
        print(f"Download failed ({e}), falling back to local cache.", flush=True)
        cached_file = pick_from_cache()
        if not cached_file:
            print("No cached emoji available offline.")
            sys.exit(1)
        print(f"Using cached: {cached_file.name}", flush=True)
        emoji_img = Image.open(cached_file).convert("RGBA")
    width, height = get_screen_size()
    print(f"Building {width}x{height} wallpaper...", flush=True)

    tile_size = int(os.environ.get("EMOJI_TILE_SIZE", TILE_SIZE))
    wallpaper = build_wallpaper(emoji_img, width, height, tile_size)
    wallpaper.save(WALLPAPER_PATH)
    print(f"Saved wallpaper to {WALLPAPER_PATH}", flush=True)

    method = set_wallpaper(WALLPAPER_PATH)
    if method:
        print(f"Wallpaper set via {method}.")
    else:
        print(f"Could not set wallpaper automatically.")
        print(f"Set it manually: {WALLPAPER_PATH}")


if __name__ == "__main__":
    main()
