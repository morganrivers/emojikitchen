#!/usr/bin/env python3
"""
Build full CLIP image embeddings for all 147k emoji kitchen images.
Downloads each image on demand (batch of 64), encodes, then deletes the temp file.
Images already cached in THUMB_DIR are reused without re-downloading.

Usage:
  crawl_emoji_kitchen.py           build / resume
  crawl_emoji_kitchen.py --reset   delete existing clip files and rebuild from scratch
"""

import sys
import os
import hashlib
import tempfile
import urllib.request
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
    from sentence_transformers import SentenceTransformer
except ImportError as e:
    print(f"Missing dependency: {e}", file=__import__("sys").stderr)
    print("Run: pip install Pillow numpy sentence-transformers torch", file=__import__("sys").stderr)
    raise SystemExit(1)

CACHE_DIR       = Path.home() / ".cache" / "emoji-wallpaper"
THUMB_DIR       = CACHE_DIR / "thumbs"
SEM_URLS        = CACHE_DIR / "embedding-urls.txt"
SEM_ALTS        = CACHE_DIR / "embedding-alts.txt"
CLIP_EMBEDDINGS = CACHE_DIR / "clip-embeddings.npy"
CLIP_URLS       = CACHE_DIR / "clip-urls.txt"
CLIP_ALTS       = CACHE_DIR / "clip-alts.txt"

MODEL_NAME = "clip-ViT-B-32"
BATCH      = 64


def thumb_path(url):
    return THUMB_DIR / (hashlib.md5(url.encode()).hexdigest() + ".png")


def fetch_image(url):
    """Return (PIL Image, is_temp). Caller must delete temp file if is_temp."""
    cached = thumb_path(url)
    if cached.exists():
        return Image.open(cached).convert("RGB"), None
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    try:
        urllib.request.urlretrieve(url, tmp.name)
        return Image.open(tmp.name).convert("RGB"), tmp.name
    except Exception:
        os.unlink(tmp.name)
        return None, None


def load_existing():
    if not CLIP_EMBEDDINGS.exists():
        return set(), [], [], []
    done_urls = set(CLIP_URLS.read_text().splitlines())
    embs = np.load(CLIP_EMBEDDINGS)
    urls = CLIP_URLS.read_text().splitlines()
    alts = CLIP_ALTS.read_text().splitlines()
    return done_urls, embs, urls, alts


def save(all_embs, all_urls, all_alts):
    np.save(CLIP_EMBEDDINGS, np.vstack(all_embs).astype(np.float16))
    CLIP_URLS.write_text("\n".join(all_urls))
    CLIP_ALTS.write_text("\n".join(all_alts))


def main():
    if "--reset" in sys.argv:
        for f in (CLIP_EMBEDDINGS, CLIP_URLS, CLIP_ALTS):
            f.unlink(missing_ok=True)
        print("Reset done.")
    elif CLIP_EMBEDDINGS.exists() and not (CLIP_EMBEDDINGS.with_name("clip-embeddings_old.npy")).exists():
        import shutil as _shutil
        backup = CLIP_EMBEDDINGS.with_name("clip-embeddings_old.npy")
        _shutil.copy2(CLIP_EMBEDDINGS, backup)
        print(f"Backed up existing embeddings to {backup.name}")

    all_urls_ordered = SEM_URLS.read_text().splitlines()
    all_alts_ordered = SEM_ALTS.read_text().splitlines()
    total = len(all_urls_ordered)

    done_urls, existing_embs, done_url_list, done_alt_list = load_existing()
    todo = [(u, a) for u, a in zip(all_urls_ordered, all_alts_ordered) if u not in done_urls]

    print(f"Total: {total:,}  Already done: {len(done_urls):,}  Remaining: {len(todo):,}")
    if not todo:
        print("Nothing to do.")
        return

    model = SentenceTransformer(MODEL_NAME)

    # Accumulate in memory; checkpoint every 1000 batches (~64k images)
    all_embs    = [existing_embs] if len(done_urls) else []
    all_url_buf = list(done_url_list)
    all_alt_buf = list(done_alt_list)

    processed = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        images, chunk_urls, chunk_alts, tmps = [], [], [], []
        for url, alt in chunk:
            img, tmp = fetch_image(url)
            if img is not None:
                images.append(img)
                chunk_urls.append(url)
                chunk_alts.append(alt)
                if tmp:
                    tmps.append(tmp)

        if images:
            embs = model.encode(images, normalize_embeddings=True, batch_size=BATCH)
            all_embs.append(embs)
            all_url_buf.extend(chunk_urls)
            all_alt_buf.extend(chunk_alts)

        for tmp in tmps:
            try:
                os.unlink(tmp)
            except OSError:
                pass

        processed += len(chunk)
        done_total = len(done_urls) + processed
        pct = done_total / total * 100
        print(f"  {done_total:>7,}/{total:,}  ({pct:.1f}%)", end="\r", flush=True)

        # Checkpoint every ~4k images
        if processed % 4096 < BATCH:
            print(f"\n  checkpoint at {done_total:,}...", flush=True)
            save(all_embs, all_url_buf, all_alt_buf)

    print(flush=True)
    save(all_embs, all_url_buf, all_alt_buf)
    final = np.load(CLIP_EMBEDDINGS)
    print(f"Done. {final.shape[0]:,} embeddings saved ({CLIP_EMBEDDINGS.stat().st_size // 1_000_000}MB)")


if __name__ == "__main__":
    main()
