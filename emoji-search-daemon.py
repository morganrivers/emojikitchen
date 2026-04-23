#!/home/dmrivers/micromamba/envs/4j/bin/python3
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
from sentence_transformers import SentenceTransformer

CACHE_DIR           = Path.home() / ".cache" / "emoji-wallpaper"
SOCK_PATH           = CACHE_DIR / "daemon.sock"
EMBEDDINGS_FILE     = CACHE_DIR / "embeddings.npy"
EMBEDDING_URLS_FILE = CACHE_DIR / "embedding-urls.txt"
EMBEDDING_ALTS_FILE = CACHE_DIR / "embedding-alts.txt"
IDLE_TIMEOUT        = 600  # seconds


def load():
    print("Loading model...", flush=True)
    model = SentenceTransformer("all-MiniLM-L6-v2")
    # Warm up torch JIT so first real query is fast
    model.encode(["warmup"], normalize_embeddings=True)
    print("Loading embeddings...", flush=True)
    embeddings = np.load(EMBEDDINGS_FILE)
    alts = EMBEDDING_ALTS_FILE.read_text().splitlines()
    urls = EMBEDDING_URLS_FILE.read_text().splitlines()
    print(f"Ready — {len(alts):,} combinations.", flush=True)
    return model, embeddings, alts, urls


def handle(conn, model, embeddings, alts, urls):
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
        q_vec = model.encode([query], normalize_embeddings=True)[0]
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

    model, embeddings, alts, urls = load()

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
                args=(conn, model, embeddings, alts, urls),
                daemon=True,
            ).start()
    finally:
        server.close()
        if SOCK_PATH.exists():
            SOCK_PATH.unlink()


if __name__ == "__main__":
    main()
