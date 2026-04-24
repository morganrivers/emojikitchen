#!/bin/bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$REPO/data/embeddings"
CACHE_DIR="$REPO/data/cache"
INDEX="$DATA_DIR/search-index.tsv"
THUMBS="$CACHE_DIR/thumbs"
CLIP_EMBEDDINGS="$DATA_DIR/clip-embeddings.npy"

if ! python3 -c "import numpy, PIL, sentence_transformers" 2>/dev/null; then
    echo "Missing Python dependencies. Install with:"
    echo "  pip install Pillow numpy sentence-transformers torch"
    exit 1
fi

if [ ! -f "$INDEX" ]; then
    echo "Search index not found - building it now (~94 MB download)..."
    python3 "$REPO/emoji-wallpaper.py"
fi

if [ -f "$CLIP_EMBEDDINGS" ] && [ "${1:-}" != "--force" ]; then
    echo "CLIP embeddings already exist: $CLIP_EMBEDDINGS"
    echo "Pass --force to rebuild."
    exit 0
fi

THUMB_COUNT=$(find "$THUMBS" -name "*.png" 2>/dev/null | wc -l || echo 0)
if [ "$THUMB_COUNT" -lt 100 ]; then
    echo "Warning: only $THUMB_COUNT thumbnails cached in $THUMBS"
    echo "Run the keyword picker a few times first to build up more thumbnails"
    echo "(more thumbnails = better CLIP coverage). Continuing anyway..."
    echo ""
fi

echo "Building CLIP image embeddings from $THUMB_COUNT cached thumbnails..."
python3 "$REPO/emoji-picker-clip.py" --build

echo ""
echo "Done. emoji-picker-clip.py is now functional."
