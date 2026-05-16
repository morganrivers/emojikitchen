#!/usr/bin/env python3
"""
Test thumbnail loading for a variety of searches.
Prints pass/fail counts per query and logs every failure reason.
"""
import sys, re, hashlib, shutil, time, urllib.request, threading
from pathlib import Path

_REPO      = Path(__file__).resolve().parent
CACHE_DIR  = _REPO / "data" / "cache"
THUMB_DIR  = CACHE_DIR / "thumbs"
INDEX_PATH = _REPO / "data" / "embeddings" / "search-index.tsv"

QUERIES = [
    "cat", "fire", "heart", "moon", "coffee",     # common
    "dragon", "octopus", "volcano", "tornado", "rainbow",
    "ghost", "unicorn", "broccoli", "snail", "crystal ball",
    "disco", "alien", "waterfall", "sad rain", "exploding head",  # more unusual
]
SAMPLE = 100   # match real BATCH_SIZE
THUMB_SIZE = 80
TIMEOUT = 10

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("WARNING: PIL not installed — skipping image-open checks")

def load_index():
    entries = []
    with open(INDEX_PATH) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) >= 2:
                entries.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    return entries

def search(entries, query):
    words = query.lower().split()
    scored = []
    for url, alt, text in entries:
        blob = (alt + " " + text).lower()
        if all(w in blob for w in words):
            scored.append((url, alt))
    return scored[:200]

def get_thumb_test(url):
    """Returns (path_or_None, error_or_None, was_cached)."""
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    name = hashlib.md5(url.encode()).hexdigest() + ".png"
    path = THUMB_DIR / name
    if path.exists() and path.stat().st_size > 0:
        return str(path), None, True
    tmp = path.with_suffix(".png.tmp")
    err = "unknown"
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "emojikitchen-picker"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp, open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
            if tmp.stat().st_size > 0:
                tmp.replace(path)
                return str(path), None, False
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        tmp.unlink(missing_ok=True)
        if attempt == 0:
            time.sleep(0.3)
    return None, err, False

def pil_load_test(path):
    """Returns error string or None if PIL can open+resize the image."""
    if not HAS_PIL:
        return None
    try:
        img = Image.open(path).convert("RGBA")
        img.resize((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"

def test_query(query, entries):
    results = search(entries, query)
    if not results:
        print(f"  [{query}] no results in index")
        return

    # prefer uncached to stress-test network
    uncached = [(u, a) for u, a in results
                if not (THUMB_DIR / (hashlib.md5(u.encode()).hexdigest() + ".png")).exists()]
    sample = (uncached[:SAMPLE] if len(uncached) >= SAMPLE
              else results[:SAMPLE])

    lock = threading.Lock()
    stats = {"ok": 0, "fail": 0, "cached": 0}
    errors = []

    def worker(url, alt):
        path, dl_err, cached = get_thumb_test(url)
        pil_err = pil_load_test(path) if path else None
        with lock:
            if cached and not pil_err:
                stats["cached"] += 1
            elif dl_err:
                stats["fail"] += 1
                errors.append(f"    DL FAIL  {alt}: {dl_err}")
            elif pil_err:
                stats["fail"] += 1
                errors.append(f"    PIL FAIL {alt}: {pil_err}")
            else:
                stats["ok"] += 1

    threads = [threading.Thread(target=worker, args=(u, a), daemon=True)
               for u, a in sample]
    for t in threads: t.start()
    for t in threads: t.join()

    total = stats["ok"] + stats["fail"] + stats["cached"]
    print(f"  [{query}] {len(results)} results | sampled {total} | "
          f"ok={stats['ok']} cached={stats['cached']} fail={stats['fail']}")
    for e in errors:
        print(e)

def main():
    print(f"Loading index...")
    entries = load_index()
    print(f"  {len(entries)} entries\n")
    for q in QUERIES:
        test_query(q, entries)
        print()

if __name__ == "__main__":
    main()
