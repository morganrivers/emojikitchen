#!/usr/bin/env python3
"""
Visual emoji kitchen picker via rofi — CLIP image search.
Encodes cached thumbnail images with CLIP; queries with text.
On first run, builds embeddings from whatever thumbs are cached (~8k).

Usage:
  emoji-picker-clip.py            normal picker
  emoji-picker-clip.py --build    force-rebuild embeddings
"""

import sys
import os
import re
import hashlib
import shutil
import subprocess
import urllib.request
import concurrent.futures
from pathlib import Path

try:
    import numpy as np
    from fastembed import TextEmbedding
    HAS_ML = True
except ImportError:
    HAS_ML = False

try:
    from PIL import Image
    from sentence_transformers import SentenceTransformer
    HAS_BUILD = True
except ImportError:
    HAS_BUILD = False

_REPO        = Path(__file__).resolve().parent
DATA_DIR     = _REPO / "data" / "embeddings"
CACHE_DIR    = _REPO / "data" / "cache"
SEARCH_INDEX = DATA_DIR / "search-index.tsv"
THUMB_DIR    = CACHE_DIR / "thumbs"
WALLPAPER_PATH = CACHE_DIR / "wallpaper.png"

CLIP_EMBEDDINGS     = DATA_DIR / "clip-embeddings.npy"
CLIP_URLS           = DATA_DIR / "clip-urls.txt"
CLIP_ALTS           = DATA_DIR / "clip-alts.txt"
CLIP_PCA_EMBEDDINGS = DATA_DIR / "clip-embeddings-pca256.npy"
CLIP_PCA_MATRIX     = DATA_DIR / "clip-pca256-matrix.npy"
CLIP_PCA_MEAN       = DATA_DIR / "clip-pca256-mean.npy"

MODEL_NAME  = "clip-ViT-B-32"
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


# ── embedding builder ────────────────────────────────────────────────────────

def build_embeddings():
    if CLIP_EMBEDDINGS.exists():
        backup = CLIP_EMBEDDINGS.with_name("clip-embeddings_old.npy")
        import shutil as _shutil
        _shutil.copy2(CLIP_EMBEDDINGS, backup)
        print(f"Backed up existing embeddings to {backup.name}")
    print("Building hash→url map from search index...", flush=True)
    url_map = {}
    with open(SEARCH_INDEX) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) >= 2:
                url, alt = parts[0], parts[1]
                url_map[hashlib.md5(url.encode()).hexdigest()] = (url, alt)

    thumb_files = sorted(THUMB_DIR.glob("*.png"))
    matched = [(url_map[p.stem], p) for p in thumb_files if p.stem in url_map]
    print(f"Matched {len(matched)} cached thumbs.", flush=True)

    model = SentenceTransformer(MODEL_NAME)
    print(f"Encoding {len(matched)} images with {MODEL_NAME}...", flush=True)

    CHUNK = 64
    all_embeddings, urls, alts = [], [], []
    for i in range(0, len(matched), CHUNK):
        chunk = matched[i:i + CHUNK]
        images, chunk_urls, chunk_alts = [], [], []
        for (url, alt), path in chunk:
            try:
                images.append(Image.open(path).convert("RGB"))
                chunk_urls.append(url)
                chunk_alts.append(alt)
            except Exception:
                pass
        if images:
            embs = model.encode(images, normalize_embeddings=True, batch_size=CHUNK)
            all_embeddings.append(embs)
            urls.extend(chunk_urls)
            alts.extend(chunk_alts)
        print(f"  {min(i + CHUNK, len(matched))}/{len(matched)}", end="\r", flush=True)

    print(flush=True)
    embeddings = np.vstack(all_embeddings)
    np.save(CLIP_EMBEDDINGS, embeddings.astype(np.float16))
    CLIP_URLS.write_text("\n".join(urls))
    CLIP_ALTS.write_text("\n".join(alts))
    print(f"Saved {len(urls)} CLIP embeddings to {CACHE_DIR}", flush=True)
    return model, embeddings, alts, urls


# ── search ───────────────────────────────────────────────────────────────────

def load_or_build(use_pca=False):
    model = TextEmbedding("Qdrant/clip-ViT-B-32-text")
    if use_pca:
        if not CLIP_PCA_EMBEDDINGS.exists():
            subprocess.run(["rofi", "-e", "PCA embeddings not found — run compress-clip-embeddings.py first."])
            sys.exit(1)
        embeddings = np.load(CLIP_PCA_EMBEDDINGS)
        pca_matrix = np.load(CLIP_PCA_MATRIX)
        pca_mean   = np.load(CLIP_PCA_MEAN)
        alts = CLIP_ALTS.read_text().splitlines()
        urls = CLIP_URLS.read_text().splitlines()
        return model, embeddings, alts, urls, pca_matrix, pca_mean
    if not CLIP_EMBEDDINGS.exists():
        if not HAS_BUILD:
            subprocess.run(["rofi", "-e", "Build deps missing — run: pip install Pillow sentence-transformers torch"])
            sys.exit(1)
        embs = build_embeddings()
        return embs + (None, None)
    embeddings = np.load(CLIP_EMBEDDINGS)
    alts = CLIP_ALTS.read_text().splitlines()
    urls = CLIP_URLS.read_text().splitlines()
    return model, embeddings, alts, urls, None, None


def clip_search(model, embeddings, alts, urls, query, limit=MAX_RESULTS,
                pca_matrix=None, pca_mean=None):
    q_vec = next(model.embed([query])).astype(np.float32)
    if pca_matrix is not None:
        q_vec = q_vec - pca_mean
        q_vec = q_vec @ pca_matrix
        q_vec = q_vec / max(np.linalg.norm(q_vec), 1e-8)
    scores = embeddings @ q_vec
    top_idx = scores.argsort()[::-1][:limit]
    return [(float(scores[i]), alts[i], urls[i]) for i in top_idx]


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
    if path.exists() and path.stat().st_size == 0:
        path.unlink()
    if not path.exists():
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = resp.read()
            path.write_bytes(data)
        except Exception:
            path.unlink(missing_ok=True)
            return None
    return str(path)


def set_wallpaper(url, alt):
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
    if not HAS_ML:
        subprocess.run(["rofi", "-e", "ML dependencies not installed.\nRun: pip install Pillow numpy sentence-transformers torch"])
        sys.exit(1)

    use_pca = "--no-pca" not in sys.argv

    if "--build" in sys.argv:
        build_embeddings()
        return

    if not SEARCH_INDEX.exists():
        subprocess.run(["rofi", "-e", "Search index missing — run emoji-wallpaper.py first."])
        sys.exit(1)

    model, embeddings, alts, urls, pca_matrix, pca_mean = load_or_build(use_pca)

    query = rofi("emoji search (CLIP):")
    if not query:
        sys.exit(0)

    results = clip_search(model, embeddings, alts, urls, query,
                         pca_matrix=pca_matrix, pca_mean=pca_mean)

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
