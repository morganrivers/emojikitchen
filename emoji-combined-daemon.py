#!/home/dmrivers/micromamba/envs/4j/bin/python3
"""
Persistent combined-search daemon.
Loads all-MiniLM-L6-v2 + clip-ViT-B-32 once, serves rank-sum queries.
Auto-started by emoji-picker-combined.py and emoji-story.py.
Exits after 10 minutes of inactivity.

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
from sentence_transformers import SentenceTransformer

CACHE_DIR       = Path.home() / ".cache" / "emoji-wallpaper"
SOCK_PATH       = CACHE_DIR / "combined-daemon.sock"
SEM_EMBEDDINGS  = CACHE_DIR / "embeddings.npy"
SEM_URLS        = CACHE_DIR / "embedding-urls.txt"
CLIP_EMBEDDINGS = CACHE_DIR / "clip-embeddings.npy"
CLIP_URLS       = CACHE_DIR / "clip-urls.txt"
CLIP_ALTS       = CACHE_DIR / "clip-alts.txt"
IDLE_TIMEOUT    = 600


def load():
    print("Loading models...", flush=True)
    sem_model  = SentenceTransformer("all-MiniLM-L6-v2")
    clip_model = SentenceTransformer("clip-ViT-B-32")

    print("Loading embeddings...", flush=True)
    sem_emb_full = np.load(SEM_EMBEDDINGS)
    sem_urls_all = SEM_URLS.read_text().splitlines()
    clip_emb  = np.load(CLIP_EMBEDDINGS)
    clip_urls = CLIP_URLS.read_text().splitlines()
    clip_alts = CLIP_ALTS.read_text().splitlines()

    idx_map = {u: i for i, u in enumerate(sem_urls_all)}
    sem_emb = sem_emb_full[[idx_map[u] for u in clip_urls if u in idx_map]]

    print(f"Ready — {len(clip_urls):,} images.", flush=True)
    return sem_model, clip_model, sem_emb, clip_emb, clip_alts, clip_urls


def handle(conn, sem_model, clip_model, sem_emb, clip_emb, clip_alts, clip_urls):
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

        sq = sem_model.encode([query],  normalize_embeddings=True)[0]
        cq = clip_model.encode([query], normalize_embeddings=True)[0]
        sr = (sem_emb  @ sq).argsort()[::-1].argsort()
        cr = (clip_emb @ cq).argsort()[::-1].argsort()
        combined = sr + cr
        top_idx  = combined.argsort()[:limit]

        results = [{"alt": clip_alts[i], "url": clip_urls[i], "rank": int(combined[i])}
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

    sem_model, clip_model, sem_emb, clip_emb, clip_alts, clip_urls = load()

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
                args=(conn, sem_model, clip_model, sem_emb, clip_emb, clip_alts, clip_urls),
                daemon=True,
            ).start()
    finally:
        server.close()
        if SOCK_PATH.exists():
            SOCK_PATH.unlink()


if __name__ == "__main__":
    main()
