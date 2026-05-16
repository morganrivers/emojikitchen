#!/usr/bin/env python3
"""
Split-query search daemon.
Counts words in the query and dispatches differently for 1 or 2 words:

  1 word  - scores base emojis, returns squared combo (emoji-emoji) first
  2 words - scores base emojis per word, cross-ranks all kitchen combos by
             min(rank_w1[a]+rank_w2[b], rank_w1[b]+rank_w2[a]), prepends results
  3+ words - standard rank-sum combined search only

In all cases, decomposed results are prepended to a full combined-search
fallback so the picker always has a complete ranked list.

Protocol: newline-terminated JSON each direction.
  Request:  {"query": "...", "limit": 5000}
  Response: [{"alt": "...", "url": "...", "rank": 0}, ...]
"""

import os
os.environ["TRANSFORMERS_VERBOSITY"] = "error"

import ast as _ast
import json
import re
import signal
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding

_REPO        = Path(__file__).resolve().parent
if not (_REPO / "data").exists():
    _REPO = Path(sys.executable).resolve().parent
DATA_DIR     = _REPO / "data" / "embeddings"
CACHE_DIR    = _REPO / "data" / "cache"
SOCK_PATH    = CACHE_DIR / "split-daemon.sock"
STATUS_PATH  = CACHE_DIR / "split-daemon-loading.json"
SEARCH_INDEX = DATA_DIR / "search-index.tsv"

_SEM_HF_REPO  = "qdrant/all-MiniLM-L6-v2-onnx"
_CLIP_HF_REPO = "Qdrant/clip-ViT-B-32-text"
_SEM_SIZE_MB  = 88
_CLIP_SIZE_MB = 245

# base emoji files (built by build-base-emoji-embeddings.py)
BASE_CODES = DATA_DIR / "base-emoji-codes.txt"
BASE_NAMES = DATA_DIR / "base-emoji-names.txt"
BASE_SEM   = DATA_DIR / "base-emoji-sem.npy"
BASE_CLIP  = DATA_DIR / "base-emoji-clip.npy"

# full combo files (shared with combined daemon)
SEM_EMBEDDINGS       = DATA_DIR / "embeddings.npy"
SEM_EMBEDDINGS_PCA   = DATA_DIR / "embeddings-pca340.npy"
SEM_PCA_MATRIX       = DATA_DIR / "embeddings-pca340-matrix.npy"
SEM_PCA_MEAN         = DATA_DIR / "embeddings-pca340-mean.npy"
SEM_URLS             = DATA_DIR / "embedding-urls.txt"
CLIP_EMBEDDINGS      = DATA_DIR / "clip-embeddings.npy"
CLIP_EMBEDDINGS_PCA  = DATA_DIR / "clip-embeddings-pca256.npy"
CLIP_PCA_MATRIX      = DATA_DIR / "clip-pca256-matrix.npy"
CLIP_PCA_MEAN        = DATA_DIR / "clip-pca256-mean.npy"
CLIP_URLS            = DATA_DIR / "clip-urls.txt"
CLIP_ALTS            = DATA_DIR / "clip-alts.txt"

IDLE_TIMEOUT = 600


def _fastembed_cache():
    default = Path(tempfile.gettempdir()) / "fastembed_cache"
    return Path(os.getenv("FASTEMBED_CACHE_PATH", str(default)))


def _model_dir(hf_repo_id):
    return _fastembed_cache() / f"models--{hf_repo_id.replace('/', '--')}"


def _model_cached(hf_repo_id):
    blobs = _model_dir(hf_repo_id) / "blobs"
    try:
        return blobs.exists() and any(
            f for f in blobs.iterdir() if f.stat().st_size > 1_000_000
        )
    except OSError:
        return False


def _write_status(step, pct):
    try:
        tmp = STATUS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"step": step, "pct": round(float(pct), 1)}))
        tmp.replace(STATUS_PATH)
    except Exception:
        pass


def _with_download_monitor(hf_repo_id, size_mb, label, pct_start, pct_end, fn):
    blobs_dir = _model_dir(hf_repo_id) / "blobs"
    stop = threading.Event()

    def _watch():
        while not stop.wait(0.4):
            try:
                n = sum(f.stat().st_size for f in blobs_dir.iterdir() if f.is_file())
                frac = min(n / (size_mb * 1_000_000), 0.99)
                _write_status(
                    f"{label} ({n/1e6:.0f} / {size_mb} MB)",
                    pct_start + (pct_end - pct_start) * frac,
                )
            except Exception:
                pass

    _write_status(f"{label}...", pct_start)
    threading.Thread(target=_watch, daemon=True).start()
    result = fn()
    stop.set()
    return result


def _np_load_tracked(path, pct_start, pct_end, on_progress, chunk=8 * 1024 * 1024):
    with open(path, "rb") as f:
        f.read(6)                   # magic \x93NUMPY
        major = f.read(1)[0]
        f.read(1)                   # minor version
        hlen = int.from_bytes(f.read(2 if major == 1 else 4), "little")
        header = _ast.literal_eval(f.read(hlen).decode("latin1").strip())
        dtype = np.dtype(header["descr"])
        shape = tuple(header["shape"])
        fortran = header.get("fortran_order", False)
        total = dtype.itemsize * (int(np.prod(shape)) if shape else 1)
        on_progress(pct_start)
        chunks, read = [], 0
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            chunks.append(buf)
            read += len(buf)
            if total > 0:
                on_progress(pct_start + (pct_end - pct_start) * min(read / total, 1.0))
    arr = np.frombuffer(b"".join(chunks), dtype=dtype)
    if shape:
        arr = arr.reshape(shape, order="F" if fortran else "C")
    return arr


def load():
    for f in (BASE_CODES, BASE_NAMES, BASE_SEM, BASE_CLIP):
        if not f.exists():
            print(f"Missing {f.name} - run build-base-emoji-embeddings.py first.", flush=True)
            sys.exit(1)

    # ── semantic model ────────────────────────────────────────────────────────
    print("Loading models...", flush=True)
    if _model_cached(_SEM_HF_REPO):
        _write_status("Loading semantic model...", 5)
        sem_model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
    else:
        sem_model = _with_download_monitor(
            _SEM_HF_REPO, _SEM_SIZE_MB, "Downloading semantic model", 5, 18,
            lambda: TextEmbedding("sentence-transformers/all-MiniLM-L6-v2"),
        )

    # ── CLIP model ────────────────────────────────────────────────────────────
    if _model_cached(_CLIP_HF_REPO):
        _write_status("Loading CLIP model...", 18)
        clip_model = TextEmbedding("Qdrant/clip-ViT-B-32-text")
    else:
        clip_model = _with_download_monitor(
            _CLIP_HF_REPO, _CLIP_SIZE_MB, "Downloading CLIP model", 18, 33,
            lambda: TextEmbedding("Qdrant/clip-ViT-B-32-text"),
        )

    # ── warmup ────────────────────────────────────────────────────────────────
    _write_status("Warming up semantic model...", 33)
    next(sem_model.embed(["warmup"]))
    _write_status("Warming up CLIP model...", 37)
    next(clip_model.embed(["warmup"]))

    # ── base emoji embeddings ─────────────────────────────────────────────────
    _write_status("Loading base emoji data...", 42)
    print("Loading base emoji embeddings...", flush=True)
    base_codes    = BASE_CODES.read_text().splitlines()
    base_sem_emb  = np.load(BASE_SEM).astype(np.float32)
    base_clip_emb = np.load(BASE_CLIP).astype(np.float32)
    code_to_idx   = {c: i for i, c in enumerate(base_codes)}

    # ── search index ──────────────────────────────────────────────────────────
    _write_status("Loading search index...", 48)
    print("Loading combo existence map from search index...", flush=True)
    combo_map = {}
    with open(SEARCH_INDEX) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) < 2:
                continue
            url, alt = parts[0], parts[1]
            m = re.search(r'/([^/]+)/([^/]+)_([^/]+)\.png$', url)
            if not m:
                continue
            c1, c2 = m.group(2), m.group(3)
            combo_map[(c1, c2)] = (url, alt)

    # ── combo embeddings ──────────────────────────────────────────────────────
    print("Loading full combo embeddings...", flush=True)

    if SEM_EMBEDDINGS.exists():
        sem_emb_full   = _np_load_tracked(
            SEM_EMBEDDINGS, 52, 70,
            lambda p: _write_status("Loading semantic embeddings...", p),
        )
        sem_pca_matrix = None
        sem_pca_mean   = None
    else:
        sem_emb_full   = _np_load_tracked(
            SEM_EMBEDDINGS_PCA, 52, 70,
            lambda p: _write_status("Loading semantic embeddings...", p),
        )
        sem_pca_matrix = np.load(SEM_PCA_MATRIX).astype(np.float32)
        sem_pca_mean   = np.load(SEM_PCA_MEAN).astype(np.float32)

    if CLIP_EMBEDDINGS.exists():
        clip_emb        = _np_load_tracked(
            CLIP_EMBEDDINGS, 72, 92,
            lambda p: _write_status("Loading CLIP embeddings...", p),
        )
        clip_pca_matrix = None
        clip_pca_mean   = None
    else:
        clip_emb        = _np_load_tracked(
            CLIP_EMBEDDINGS_PCA, 72, 92,
            lambda p: _write_status("Loading CLIP embeddings...", p),
        )
        clip_pca_matrix = np.load(CLIP_PCA_MATRIX).astype(np.float32)
        clip_pca_mean   = np.load(CLIP_PCA_MEAN).astype(np.float32)

    # ── finalize ──────────────────────────────────────────────────────────────
    _write_status("Building lookup tables...", 94)
    sem_urls_all = SEM_URLS.read_text().splitlines()
    clip_urls    = CLIP_URLS.read_text().splitlines()
    clip_alts    = CLIP_ALTS.read_text().splitlines()

    idx_map = {u: i for i, u in enumerate(sem_urls_all)}
    sem_emb = sem_emb_full[[idx_map[u] for u in clip_urls if u in idx_map]]

    _write_status("Ready", 100)
    print(f"Ready - {len(base_codes)} base emojis, {len(combo_map):,} combos.", flush=True)
    return (sem_model, clip_model,
            base_sem_emb, base_clip_emb, code_to_idx, combo_map,
            sem_emb, sem_pca_matrix, sem_pca_mean,
            clip_emb, clip_pca_matrix, clip_pca_mean,
            clip_alts, clip_urls)


def rank_base(query_word, sem_model, clip_model, base_sem_emb, base_clip_emb):
    sq = next(sem_model.embed([query_word])).astype(np.float32)
    cq = next(clip_model.embed([query_word])).astype(np.float32)
    sem_r  = (base_sem_emb @ sq).argsort()[::-1].argsort()
    clip_r = (base_clip_emb @ cq).argsort()[::-1].argsort()
    return sem_r + clip_r


def decompose_one(word, sem_model, clip_model,
                  base_sem_emb, base_clip_emb, combo_map):
    ranks = rank_base(word, sem_model, clip_model, base_sem_emb, base_clip_emb)
    best  = int(ranks.argmin())
    # find best code from combo_map keys that correspond to index `best`
    # (we need the actual code string - recover via sorted order matching build script)
    # We stored code_to_idx during load; use a reverse lookup passed in via closure
    return ranks, best


def search_one(word, sem_model, clip_model,
               base_sem_emb, base_clip_emb, code_to_idx, combo_map):
    ranks = rank_base(word, sem_model, clip_model, base_sem_emb, base_clip_emb)
    idx_to_code = {v: k for k, v in code_to_idx.items()}
    best_code = idx_to_code.get(int(ranks.argmin()))
    if best_code and (best_code, best_code) in combo_map:
        url, alt = combo_map[(best_code, best_code)]
        return [(0, alt, url)]
    return []


def search_two(word1, word2, sem_model, clip_model,
               base_sem_emb, base_clip_emb, code_to_idx, combo_map):
    ranks_w1 = rank_base(word1, sem_model, clip_model, base_sem_emb, base_clip_emb)
    ranks_w2 = rank_base(word2, sem_model, clip_model, base_sem_emb, base_clip_emb)

    scored = []
    for (c1, c2), (url, alt) in combo_map.items():
        i = code_to_idx.get(c1)
        j = code_to_idx.get(c2)
        if i is None or j is None:
            continue
        score = min(
            int(ranks_w1[i]) + int(ranks_w2[j]),
            int(ranks_w1[j]) + int(ranks_w2[i]),
        )
        scored.append((score, alt, url))
    scored.sort(key=lambda x: x[0])
    return scored


def search_combined(query, sem_model, clip_model,
                    sem_emb, sem_pca_matrix, sem_pca_mean,
                    clip_emb, clip_pca_matrix, clip_pca_mean,
                    clip_alts, clip_urls):
    sq = next(sem_model.embed([query])).astype(np.float32)
    if sem_pca_matrix is not None:
        sq = (sq - sem_pca_mean) @ sem_pca_matrix
        sq /= max(np.linalg.norm(sq), 1e-8)

    cq = next(clip_model.embed([query])).astype(np.float32)
    if clip_pca_matrix is not None:
        cq = (cq - clip_pca_mean) @ clip_pca_matrix
        cq /= max(np.linalg.norm(cq), 1e-8)

    k = 60
    sr = (sem_emb  @ sq).argsort()[::-1].argsort()
    cr = (clip_emb @ cq).argsort()[::-1].argsort()
    combined = 1.0 / (k + sr) + 1.0 / (k + cr)
    top_idx  = combined.argsort()[::-1]
    return [(float(combined[i]), clip_alts[i], clip_urls[i]) for i in top_idx]


def handle(conn, sem_model, clip_model,
           base_sem_emb, base_clip_emb, code_to_idx, combo_map,
           sem_emb, sem_pca_matrix, sem_pca_mean,
           clip_emb, clip_pca_matrix, clip_pca_mean,
           clip_alts, clip_urls):
    try:
        data = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            if data.endswith(b"\n"):
                break
        req   = json.loads(data.decode())
        query = req["query"]
        limit = req.get("limit", 5000)
        words = query.strip().split()

        decomposed = []
        if len(words) == 1:
            decomposed = search_one(
                words[0], sem_model, clip_model,
                base_sem_emb, base_clip_emb, code_to_idx, combo_map,
            )
        elif len(words) == 2:
            decomposed = search_two(
                words[0], words[1], sem_model, clip_model,
                base_sem_emb, base_clip_emb, code_to_idx, combo_map,
            )

        fallback = search_combined(
            query, sem_model, clip_model,
            sem_emb, sem_pca_matrix, sem_pca_mean,
            clip_emb, clip_pca_matrix, clip_pca_mean,
            clip_alts, clip_urls,
        )

        seen = {url for _, _, url in decomposed}
        merged = decomposed + [(r, a, u) for r, a, u in fallback if u not in seen]

        results = [{"alt": a, "url": u, "rank": r} for r, a, u in merged[:limit]]
        conn.sendall((json.dumps(results) + "\n").encode())
    except Exception as e:
        try:
            conn.sendall((json.dumps({"error": str(e)}) + "\n").encode())
        except Exception:
            pass
    finally:
        conn.close()


def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if SOCK_PATH.exists():
        SOCK_PATH.unlink()
    if STATUS_PATH.exists():
        STATUS_PATH.unlink()

    (sem_model, clip_model,
     base_sem_emb, base_clip_emb, code_to_idx, combo_map,
     sem_emb, sem_pca_matrix, sem_pca_mean,
     clip_emb, clip_pca_matrix, clip_pca_mean,
     clip_alts, clip_urls) = load()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCK_PATH))
    server.listen(8)
    server.settimeout(IDLE_TIMEOUT)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"Listening on {SOCK_PATH}", flush=True)

    try:
        while True:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                print("Idle timeout - exiting.", flush=True)
                break
            threading.Thread(
                target=handle,
                args=(conn, sem_model, clip_model,
                      base_sem_emb, base_clip_emb, code_to_idx, combo_map,
                      sem_emb, sem_pca_matrix, sem_pca_mean,
                      clip_emb, clip_pca_matrix, clip_pca_mean,
                      clip_alts, clip_urls),
                daemon=True,
            ).start()
    finally:
        server.close()
        if SOCK_PATH.exists():
            SOCK_PATH.unlink()
        if STATUS_PATH.exists():
            STATUS_PATH.unlink()


if __name__ == "__main__":
    main()
