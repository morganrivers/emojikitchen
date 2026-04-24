#!/usr/bin/env python3
"""
Persistent combined-search daemon.
Loads all-MiniLM-L6-v2 + clip-ViT-B-32 once, serves RRF queries.
Auto-started by emoji-picker-combined.py and emoji-story.py.
Exits after 10 minutes of inactivity.

Prefers full embeddings when present; falls back to PCA-compressed
versions (embeddings-pca340.npy / clip-embeddings-pca256.npy) so the
daemon works from the repo-hosted files without torch.

Protocol: newline-terminated JSON each direction.
  Request:  {"query": "...", "limit": 20}
  Response: [{"alt": "...", "url": "...", "rank": 42}, ...]
"""

import os
os.environ["TRANSFORMERS_VERBOSITY"] = "error"

import json
import signal
import socket
import sys
import threading

import numpy as np
from pathlib import Path
from fastembed import TextEmbedding

CACHE_DIR            = Path.home() / ".cache" / "emoji-wallpaper"
SOCK_PATH            = CACHE_DIR / "combined-daemon.sock"
SEM_EMBEDDINGS       = CACHE_DIR / "embeddings.npy"
SEM_EMBEDDINGS_PCA   = CACHE_DIR / "embeddings-pca340.npy"
SEM_PCA_MATRIX       = CACHE_DIR / "embeddings-pca340-matrix.npy"
SEM_PCA_MEAN         = CACHE_DIR / "embeddings-pca340-mean.npy"
SEM_URLS             = CACHE_DIR / "embedding-urls.txt"
CLIP_EMBEDDINGS      = CACHE_DIR / "clip-embeddings.npy"
CLIP_EMBEDDINGS_PCA  = CACHE_DIR / "clip-embeddings-pca256.npy"
CLIP_PCA_MATRIX      = CACHE_DIR / "clip-pca256-matrix.npy"
CLIP_PCA_MEAN        = CACHE_DIR / "clip-pca256-mean.npy"
CLIP_URLS            = CACHE_DIR / "clip-urls.txt"
CLIP_ALTS            = CACHE_DIR / "clip-alts.txt"
IDLE_TIMEOUT         = 600


def load():
    print("Loading models...", flush=True)
    sem_model  = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
    clip_model = TextEmbedding("Qdrant/clip-ViT-B-32-text")
    next(sem_model.embed(["warmup"]))
    next(clip_model.embed(["warmup"]))

    print("Loading embeddings...", flush=True)

    if SEM_EMBEDDINGS.exists():
        sem_emb_full   = np.load(SEM_EMBEDDINGS)
        sem_pca_matrix = None
        sem_pca_mean   = None
        print(f"  sem: full ({sem_emb_full.shape[1]} dims)", flush=True)
    else:
        sem_emb_full   = np.load(SEM_EMBEDDINGS_PCA)
        sem_pca_matrix = np.load(SEM_PCA_MATRIX).astype(np.float32)
        sem_pca_mean   = np.load(SEM_PCA_MEAN).astype(np.float32)
        print(f"  sem: PCA ({sem_emb_full.shape[1]} dims)", flush=True)

    if CLIP_EMBEDDINGS.exists():
        clip_emb        = np.load(CLIP_EMBEDDINGS)
        clip_pca_matrix = None
        clip_pca_mean   = None
        print(f"  clip: full ({clip_emb.shape[1]} dims)", flush=True)
    else:
        clip_emb        = np.load(CLIP_EMBEDDINGS_PCA)
        clip_pca_matrix = np.load(CLIP_PCA_MATRIX).astype(np.float32)
        clip_pca_mean   = np.load(CLIP_PCA_MEAN).astype(np.float32)
        print(f"  clip: PCA ({clip_emb.shape[1]} dims)", flush=True)

    sem_urls_all = SEM_URLS.read_text().splitlines()
    clip_urls    = CLIP_URLS.read_text().splitlines()
    clip_alts    = CLIP_ALTS.read_text().splitlines()

    idx_map = {u: i for i, u in enumerate(sem_urls_all)}
    sem_emb = sem_emb_full[[idx_map[u] for u in clip_urls if u in idx_map]]

    print(f"Ready — {len(clip_urls):,} images.", flush=True)
    return (sem_model, clip_model,
            sem_emb, sem_pca_matrix, sem_pca_mean,
            clip_emb, clip_pca_matrix, clip_pca_mean,
            clip_alts, clip_urls)


def handle(conn, sem_model, clip_model,
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
        limit = req.get("limit", 20)

        sq = next(sem_model.embed([query])).astype(np.float32)
        if sem_pca_matrix is not None:
            sq = (sq - sem_pca_mean) @ sem_pca_matrix
            sq /= max(np.linalg.norm(sq), 1e-8)

        cq = next(clip_model.embed([query])).astype(np.float32)
        if clip_pca_matrix is not None:
            cq = (cq - clip_pca_mean) @ clip_pca_matrix
            cq /= max(np.linalg.norm(cq), 1e-8)

        sr = (sem_emb  @ sq).argsort()[::-1].argsort()
        cr = (clip_emb @ cq).argsort()[::-1].argsort()
        k = 60
        combined = 1.0 / (k + sr) + 1.0 / (k + cr)
        top_idx  = combined.argsort()[::-1][:limit]

        results = [{"alt": clip_alts[i], "url": clip_urls[i], "rank": round(float(combined[i]), 6)}
                   for i in top_idx]
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

    (sem_model, clip_model,
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
                print("Idle timeout — exiting.", flush=True)
                break
            threading.Thread(
                target=handle,
                args=(conn, sem_model, clip_model,
                      sem_emb, sem_pca_matrix, sem_pca_mean,
                      clip_emb, clip_pca_matrix, clip_pca_mean,
                      clip_alts, clip_urls),
                daemon=True,
            ).start()
    finally:
        server.close()
        if SOCK_PATH.exists():
            SOCK_PATH.unlink()


if __name__ == "__main__":
    main()
