#!/usr/bin/env python3
"""
Compress CLIP and text (MiniLM) embeddings via PCA for repo hosting.

CLIP:  512 → 256 dims  (~72 MB → fits GitHub)
Text:  384 → 340 dims  (108 MB → ~95 MB, fits GitHub)

Outputs (CLIP):
  clip-embeddings-pca256.npy    - compressed embeddings (float16)
  clip-pca256-matrix.npy        - 512×256 projection matrix (float32)
  clip-pca256-mean.npy          - mean vector (float32)

Outputs (text):
  embeddings-pca340.npy         - compressed embeddings (float16)
  embeddings-pca340-matrix.npy  - 384×340 projection matrix (float32)
  embeddings-pca340-mean.npy    - mean vector (float32)

Usage:
  python3 compress-clip-embeddings.py            # both CLIP + text
  python3 compress-clip-embeddings.py --clip-only
  python3 compress-clip-embeddings.py --text-only
  python3 compress-clip-embeddings.py --dims 128          # override CLIP dims
  python3 compress-clip-embeddings.py --text-dims 320     # override text dims
"""

import sys
import numpy as np
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "data" / "embeddings"

clip_dims = 256
text_dims = 340
if "--dims" in sys.argv:
    clip_dims = int(sys.argv[sys.argv.index("--dims") + 1])
if "--text-dims" in sys.argv:
    text_dims = int(sys.argv[sys.argv.index("--text-dims") + 1])

do_clip = "--text-only" not in sys.argv
do_text = "--clip-only" not in sys.argv


def compress(embeddings_path, dims, prefix):
    if not embeddings_path.exists():
        print(f"{embeddings_path.name} not found - skipping.")
        return
    print(f"Loading {embeddings_path.name} ...", flush=True)
    embeddings = np.load(embeddings_path).astype(np.float32)
    n, d = embeddings.shape
    print(f"  shape: {embeddings.shape}  ({embeddings.nbytes // 1_000_000} MB float32)")

    print(f"Fitting PCA to {dims} dims ...", flush=True)
    mean = embeddings.mean(axis=0)
    centered = embeddings - mean
    _, _, Vt = np.linalg.svd(centered, full_matrices=False)
    components = Vt[:dims].T  # d × dims

    projected = centered @ components
    norms = np.linalg.norm(projected, axis=1, keepdims=True)
    projected = projected / np.maximum(norms, 1e-8)

    out_emb  = CACHE / f"{prefix}-pca{dims}.npy"
    out_mat  = CACHE / f"{prefix}-pca{dims}-matrix.npy"
    out_mean = CACHE / f"{prefix}-pca{dims}-mean.npy"

    np.save(out_emb,  projected.astype(np.float16))
    np.save(out_mat,  components.astype(np.float32))
    np.save(out_mean, mean.astype(np.float32))

    mb = out_emb.stat().st_size / 1_000_000
    print(f"Saved {out_emb.name}  ({mb:.1f} MB)")
    print(f"Saved {out_mat.name}  ({out_mat.stat().st_size // 1000} KB)")
    print(f"Saved {out_mean.name}")
    print()


if do_clip:
    compress(CACHE / "clip-embeddings.npy", clip_dims, "clip-embeddings")

if do_text:
    compress(CACHE / "embeddings.npy", text_dims, "embeddings")
