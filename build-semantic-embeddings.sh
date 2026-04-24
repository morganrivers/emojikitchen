#!/bin/bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$REPO/data/embeddings"
CACHE_DIR="$REPO/data/cache"
INDEX="$DATA_DIR/search-index.tsv"
EMBEDDINGS="$DATA_DIR/embeddings.npy"

if ! python3 -c "import numpy, sentence_transformers" 2>/dev/null; then
    echo "Missing Python dependencies. Install with:"
    echo "  pip install numpy sentence-transformers torch"
    exit 1
fi

if [ ! -f "$INDEX" ]; then
    echo "Search index not found - building it now (~94 MB download)..."
    python3 "$REPO/emoji-wallpaper.py"
fi

if [ -f "$EMBEDDINGS" ] && [ "${1:-}" != "--force" ]; then
    echo "Semantic embeddings already exist: $EMBEDDINGS"
    echo "Pass --force to rebuild."
    exit 0
fi

if [ -f "$EMBEDDINGS" ]; then
    BACKUP="$DATA_DIR/embeddings_old.npy"
    if [ -f "$BACKUP" ]; then
        echo "Backup already exists at $BACKUP - not overwriting"
    else
        cp "$EMBEDDINGS" "$BACKUP"
        echo "Backed up existing embeddings to embeddings_old.npy"
    fi
fi

mkdir -p "$DATA_DIR"
echo "Building MiniLM text embeddings for 147k emoji combinations (~10 min)..."
python3 - "$DATA_DIR" << 'PYEOF'
import sys
from pathlib import Path
import numpy as np
from sentence_transformers import SentenceTransformer

DATA = Path(sys.argv[1])
rows  = [line.rstrip("\n").split("\t", 2) for line in open(DATA / "search-index.tsv")]
urls  = [r[0] for r in rows if len(r) == 3]
alts  = [r[1] for r in rows if len(r) == 3]
texts = [r[2] for r in rows if len(r) == 3]

print(f"Encoding {len(texts):,} entries with all-MiniLM-L6-v2...")
model = SentenceTransformer("all-MiniLM-L6-v2")
embeddings = model.encode(texts, normalize_embeddings=True,
                          batch_size=256, show_progress_bar=True)

np.save(DATA / "embeddings.npy", embeddings.astype(np.float16))
(DATA / "embedding-urls.txt").write_text("\n".join(urls))
(DATA / "embedding-alts.txt").write_text("\n".join(alts))
mb = (DATA / "embeddings.npy").stat().st_size / 1_000_000
print(f"Saved to {DATA}/embeddings.npy ({mb:.0f} MB)")
PYEOF

echo ""
echo "Done. emoji-picker-semantic.py is now functional."
