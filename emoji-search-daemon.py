#!/usr/bin/env python3
"""
Persistent semantic search daemon.
Loads model + embeddings once, serves queries via Unix socket.
Auto-started by emoji-picker-semantic.py on first use.
Exits after 10 minutes of inactivity.

Protocol: newline-terminated JSON each direction.
  Request:  {"query": "...", "limit": 20}
  Response: [{"alt": "...", "url": "...", "score": 0.5}, ...]
"""

import json
import socket
import signal
import threading
import sys
import numpy as np
from pathlib import Path
from fastembed import TextEmbedding

_REPO               = Path(__file__).resolve().parent
DATA_DIR            = _REPO / "data" / "embeddings"
CACHE_DIR           = _REPO / "data" / "cache"
SOCK_PATH           = CACHE_DIR / "daemon.sock"
EMBEDDINGS_FILE     = DATA_DIR / "embeddings.npy"
EMBEDDINGS_PCA      = DATA_DIR / "embeddings-pca340.npy"
EMBEDDINGS_PCA_MAT  = DATA_DIR / "embeddings-pca340-matrix.npy"
EMBEDDINGS_PCA_MEAN = DATA_DIR / "embeddings-pca340-mean.npy"
EMBEDDING_URLS_FILE = DATA_DIR / "embedding-urls.txt"
EMBEDDING_ALTS_FILE = DATA_DIR / "embedding-alts.txt"
IDLE_TIMEOUT        = 600  # seconds


def load():
    print("Loading model...", flush=True)
    model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
    next(model.embed(["warmup"]))  # warm up ONNX session
    print("Loading embeddings...", flush=True)
    if EMBEDDINGS_FILE.exists():
        embeddings = np.load(EMBEDDINGS_FILE).astype(np.float32)
        pca_matrix, pca_mean = None, None
        print(f"  using full embeddings ({embeddings.shape[1]} dims)", flush=True)
    elif EMBEDDINGS_PCA.exists():
        embeddings = np.load(EMBEDDINGS_PCA).astype(np.float32)
        pca_matrix = np.load(EMBEDDINGS_PCA_MAT).astype(np.float32)
        pca_mean   = np.load(EMBEDDINGS_PCA_MEAN).astype(np.float32)
        print(f"  using PCA embeddings ({embeddings.shape[1]} dims)", flush=True)
    else:
        print("No embeddings found - run build-semantic-embeddings.sh first.", flush=True)
        sys.exit(1)
    alts = EMBEDDING_ALTS_FILE.read_text().splitlines()
    urls = EMBEDDING_URLS_FILE.read_text().splitlines()
    print(f"Ready - {len(alts):,} combinations.", flush=True)
    return model, embeddings, pca_matrix, pca_mean, alts, urls


def handle(conn, model, embeddings, pca_matrix, pca_mean, alts, urls):
    try:
        data = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            if data.endswith(b"\n"):
                break
        req = json.loads(data.decode())
        query = req["query"]
        limit = req.get("limit", 20)
        q_vec = next(model.embed([query])).astype(np.float32)
        if pca_matrix is not None:
            q_vec = (q_vec - pca_mean) @ pca_matrix
            q_vec /= max(np.linalg.norm(q_vec), 1e-8)
        scores = embeddings @ q_vec
        top_idx = scores.argsort()[::-1][:limit]
        results = [{"alt": alts[i], "url": urls[i], "score": float(scores[i])} for i in top_idx]
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

    model, embeddings, pca_matrix, pca_mean, alts, urls = load()

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
                args=(conn, model, embeddings, pca_matrix, pca_mean, alts, urls),
                daemon=True,
            ).start()
    finally:
        server.close()
        if SOCK_PATH.exists():
            SOCK_PATH.unlink()


if __name__ == "__main__":
    main()
