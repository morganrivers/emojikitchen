#!/usr/bin/env python3
"""
Search emoji kitchen combinations by text and optionally set as wallpaper.

Usage:
  emoji-search.py <query>              show top matches
  emoji-search.py <query> --set 3     set match #3 as wallpaper
  emoji-search.py <query> --random    set a random match as wallpaper
  emoji-search.py <query> --limit 50  show more results (default: 20)
"""

import sys
import os
import random
import shutil
import subprocess
import urllib.request
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "emoji-wallpaper"
SEARCH_INDEX = CACHE_DIR / "search-index.tsv"
WALLPAPER_PATH = CACHE_DIR / "wallpaper.png"
WALLPAPER_SCRIPT = Path.home() / ".local" / "bin" / "emoji-wallpaper.py"

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def ensure_index():
    if not SEARCH_INDEX.exists():
        print("Search index not found — rebuilding (downloads ~94MB, once only)...", flush=True)
        subprocess.run([sys.executable, str(WALLPAPER_SCRIPT)], check=True)
        if not SEARCH_INDEX.exists():
            print("Error: could not build search index.")
            sys.exit(1)


def load_index():
    entries = []
    with open(SEARCH_INDEX) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) == 3:
                entries.append((parts[0], parts[1], parts[2]))
    return entries


def search(entries, query, limit=20):
    words = query.lower().split()
    scored = []
    for url, alt, text in entries:
        haystack = text.lower()
        score = sum(1 for w in words if w in haystack)
        if score > 0:
            scored.append((score, alt, url))
    scored.sort(key=lambda x: -x[0])
    return scored[:limit]


def set_wallpaper(url, alt):
    if not HAS_PIL:
        print("Pillow not installed — cannot build wallpaper image.")
        sys.exit(1)

    tmp = CACHE_DIR / "current_emoji.png"
    urllib.request.urlretrieve(url, tmp)
    emoji_img = Image.open(tmp).convert("RGBA")

    # Get screen size via xrandr
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

    tile_size = int(os.environ.get("EMOJI_TILE_SIZE", 200))
    emoji_img = emoji_img.resize((tile_size, tile_size), Image.LANCZOS)
    wallpaper = Image.new("RGBA", (width, height), "white")
    for y in range(0, height, tile_size):
        for x in range(0, width, tile_size):
            wallpaper.paste(emoji_img, (x, y), emoji_img)
    wallpaper.convert("RGB").save(WALLPAPER_PATH)

    # Set via nitrogen
    nitrogen_cfg = Path.home() / ".config" / "nitrogen" / "bg-saved.cfg"
    if nitrogen_cfg.exists() or shutil.which("nitrogen"):
        try:
            nitrogen_cfg.parent.mkdir(parents=True, exist_ok=True)
            nitrogen_cfg.write_text(f"[xin_-1]\nfile={WALLPAPER_PATH}\nmode=5\nbgcolor=#000000\n")
            subprocess.run(["nitrogen", "--restore"], check=True, capture_output=True)
            print(f"Wallpaper set: {alt}")
            return
        except Exception:
            pass

    # feh fallback
    try:
        subprocess.run(["feh", "--bg-fill", str(WALLPAPER_PATH)], check=True, capture_output=True)
        print(f"Wallpaper set: {alt}")
        return
    except Exception:
        pass

    print(f"Wallpaper saved to {WALLPAPER_PATH} — set it manually.")


def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print(__doc__)
        sys.exit(0)

    query = args[0]
    limit = 20
    set_index = None
    use_random = False

    i = 1
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif args[i] == "--set" and i + 1 < len(args):
            set_index = int(args[i + 1])
            i += 2
        elif args[i] == "--random":
            use_random = True
            i += 1
        else:
            i += 1

    ensure_index()
    entries = load_index()
    results = search(entries, query, limit=limit if not (set_index or use_random) else 10000)

    if not results:
        print(f"No matches for '{query}'.")
        sys.exit(0)

    if use_random:
        score, alt, url = random.choice(results)
        set_wallpaper(url, alt)
        return

    if set_index is not None:
        if set_index < 1 or set_index > len(results):
            print(f"Pick a number between 1 and {len(results)}.")
            sys.exit(1)
        score, alt, url = results[set_index - 1]
        set_wallpaper(url, alt)
        return

    # Display results
    print(f"Found {len(results)} matches for '{query}':\n")
    for i, (score, alt, url) in enumerate(results, 1):
        print(f"  {i:>2}. {alt}")
    print(f"\nTo set one: emoji-search.py \"{query}\" --set <number>")
    print(f"To set random: emoji-search.py \"{query}\" --random")


if __name__ == "__main__":
    main()
