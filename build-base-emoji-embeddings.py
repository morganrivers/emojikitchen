#!/usr/bin/env python3
"""
Build MiniLM text + CLIP image embeddings for the ~619 base emojis
that participate in emoji kitchen combinations.

Downloads 512px Noto emoji PNGs from fonts.gstatic.com, then encodes
each with sentence-transformers/all-MiniLM-L6-v2 (name as text) and
clip-ViT-B-32 (image).

Outputs to ~/.cache/emoji-wallpaper/:
  base-emoji-codes.txt     one code per line, e.g. u1faa9
  base-emoji-names.txt     one name per line, e.g. mirror_ball
  base-emoji-sem.npy       MiniLM embeddings  (N, 384) float16
  base-emoji-clip.npy      CLIP image embeddings (N, 512) float16
  base-emoji-thumbs/       downloaded 512px Noto PNGs

Usage:
  python3 build-base-emoji-embeddings.py
  python3 build-base-emoji-embeddings.py --force   # re-download + rebuild
"""

import re
import sys
import urllib.request
import concurrent.futures
from pathlib import Path

import numpy as np

_REPO        = Path(__file__).resolve().parent
DATA_DIR     = _REPO / "data" / "embeddings"
CACHE_DIR    = _REPO / "data" / "cache"
SEARCH_INDEX = DATA_DIR / "search-index.tsv"
THUMB_DIR    = CACHE_DIR / "base-emoji-thumbs"
CODES_FILE   = DATA_DIR / "base-emoji-codes.txt"
NAMES_FILE   = DATA_DIR / "base-emoji-names.txt"
SEM_FILE     = DATA_DIR / "base-emoji-sem.npy"
CLIP_FILE    = DATA_DIR / "base-emoji-clip.npy"

NOTO_BASE = "https://fonts.gstatic.com/s/e/notoemoji/latest/{code}/512.png"


def code_to_noto(code):
    """u26f8-ufe0f -> 26f8_fe0f,  u1faa9 -> 1faa9"""
    parts = code.split("-")
    return "_".join(p.lstrip("u") for p in parts)


def extract_base_emojis():
    code_to_name = {}
    with open(SEARCH_INDEX) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) < 2:
                continue
            url, alt = parts[0], parts[1]
            m = re.search(r'/([^/]+)/([^/]+)_([^/]+)\.png$', url)
            if not m:
                continue
            code1, code2 = m.group(2), m.group(3)
            if "-" not in alt:
                continue
            name1, name2 = alt.split("-", 1)
            if code1 not in code_to_name:
                code_to_name[code1] = name1
            if code2 not in code_to_name:
                code_to_name[code2] = name2
    return code_to_name


def download_thumb(args):
    code, force = args
    path = THUMB_DIR / f"{code}.png"
    if path.exists() and not force:
        return code, path
    noto_code = code_to_noto(code)
    url = NOTO_BASE.format(code=noto_code)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = resp.read()
        path.write_bytes(data)
        return code, path
    except Exception:
        return code, None


def main():
    force = "--force" in sys.argv

    if not SEARCH_INDEX.exists():
        print("search-index.tsv not found — run emoji-wallpaper.py first.")
        sys.exit(1)

    if SEM_FILE.exists() and CLIP_FILE.exists() and not force:
        print("Base emoji embeddings already exist. Pass --force to rebuild.")
        sys.exit(0)

    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    print("Extracting base emojis from search index...", flush=True)
    code_to_name = extract_base_emojis()
    codes = sorted(code_to_name.keys())
    names = [code_to_name[c] for c in codes]
    print(f"Found {len(codes)} base emojis.", flush=True)

    print("Downloading Noto emoji images (512px)...", flush=True)
    args = [(c, force) for c in codes]
    paths = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        for i, (code, path) in enumerate(ex.map(download_thumb, args), 1):
            paths[code] = path
            print(f"  {i}/{len(codes)}", end="\r", flush=True)
    print(flush=True)
    ok = sum(1 for p in paths.values() if p is not None)
    print(f"Downloaded {ok}/{len(codes)} images.", flush=True)

    print("Building MiniLM text embeddings from names...", flush=True)
    from fastembed import TextEmbedding
    sem_model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
    sem_vecs = np.array(list(sem_model.embed(names)), dtype=np.float32)
    norms = np.linalg.norm(sem_vecs, axis=1, keepdims=True)
    sem_vecs /= np.maximum(norms, 1e-8)

    print("Building CLIP image embeddings...", flush=True)
    from PIL import Image
    from sentence_transformers import SentenceTransformer
    clip_model = SentenceTransformer("clip-ViT-B-32")

    images, valid_idx = [], []
    for i, code in enumerate(codes):
        p = paths.get(code)
        if p and p.exists():
            try:
                images.append(Image.open(p).convert("RGB"))
                valid_idx.append(i)
            except Exception:
                pass

    print(f"  Encoding {len(images)} images...", flush=True)
    clip_valid = clip_model.encode(
        images, normalize_embeddings=True, batch_size=64, show_progress_bar=True
    )
    clip_vecs = np.zeros((len(codes), clip_valid.shape[1]), dtype=np.float32)
    for idx, vec in zip(valid_idx, clip_valid):
        clip_vecs[idx] = vec

    CODES_FILE.write_text("\n".join(codes))
    NAMES_FILE.write_text("\n".join(names))
    np.save(SEM_FILE,  sem_vecs.astype(np.float16))
    np.save(CLIP_FILE, clip_vecs.astype(np.float16))
    print(f"Saved embeddings for {len(codes)} base emojis to {CACHE_DIR}", flush=True)


if __name__ == "__main__":
    main()
