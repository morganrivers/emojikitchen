# Full Setup Guide

This guide covers everything needed to reproduce all tools from scratch on a new Linux machine.

---

## Prerequisites

**System packages** (install via your distro's package manager):
```
rofi
feh           # or nitrogen — for setting wallpaper
xclip         # for clipboard copy in emoji-picker-clip.py
xrandr        # usually pre-installed with X11
```

**Python environment**: The ML scripts (`emoji-picker-semantic.py`, `emoji-picker-combined.py`, `emoji-picker-clip.py`, and both daemons) require a Python environment with heavy ML libraries. A `micromamba`/`conda` env is recommended:

```bash
micromamba create -n emojikitchen python=3.11
micromamba activate emojikitchen
pip install Pillow sentence-transformers numpy torch torchvision
```

The basic scripts (`emoji-wallpaper.py`, `emoji-search.py`) only need `Pillow` and the standard library, so `pip install Pillow` alone works for those.

---

## Install Scripts

Copy all scripts to `~/.local/bin/` and mark them executable:

```bash
cp emoji-wallpaper.py emoji-search.py emoji-story.py \
   emoji-picker.py emoji-picker-semantic.py emoji-picker-clip.py \
   emoji-picker-combined.py emoji-search-daemon.py \
   emoji-combined-daemon.py ~/.local/bin/

chmod +x ~/.local/bin/emoji-*.py
```

If using a conda/micromamba env, update the shebang lines on the ML scripts to point to your env's Python:

```bash
# e.g. if your env is at ~/micromamba/envs/emojikitchen:
sed -i '1s|.*|#!/home/$USER/micromamba/envs/emojikitchen/bin/python3|' \
  ~/.local/bin/emoji-picker-semantic.py \
  ~/.local/bin/emoji-picker-combined.py \
  ~/.local/bin/emoji-picker-clip.py \
  ~/.local/bin/emoji-search-daemon.py \
  ~/.local/bin/emoji-combined-daemon.py
```

---

## Step 1 — Build the Search Index (~94 MB download, once only)

This fetches the full emoji kitchen metadata from GitHub and builds the keyword search index.

```bash
python3 ~/.local/bin/emoji-wallpaper.py
```

This creates (in `~/.cache/emoji-wallpaper/`):

| File | Size | Contents |
|---|---|---|
| `search-index.tsv` | ~42 MB | 147k rows: `url\talt\tkeywords` |
| `urls.txt` | ~13 MB | All 147k image URLs |

After this step, `emoji-wallpaper.py` and `emoji-search.py` are fully functional.

---

## Step 2 — Build Semantic (MiniLM) Embeddings (~216 MB, ~10 min)

The semantic search daemon (`emoji-search-daemon.py`) needs text embeddings for all 147k combinations. These are built by encoding the alt text + keywords from the search index with `all-MiniLM-L6-v2`.

There is no dedicated script in the repo for this — run it with a short Python snippet:

```python
# build_semantic_embeddings.py
from pathlib import Path
import numpy as np
from sentence_transformers import SentenceTransformer

CACHE = Path.home() / ".cache" / "emoji-wallpaper"
rows = [line.rstrip("\n").split("\t", 2) for line in open(CACHE / "search-index.tsv")]
urls  = [r[0] for r in rows if len(r) == 3]
alts  = [r[1] for r in rows if len(r) == 3]
texts = [r[2] for r in rows if len(r) == 3]

print(f"Encoding {len(texts):,} entries with all-MiniLM-L6-v2 ...")
model = SentenceTransformer("all-MiniLM-L6-v2")
embeddings = model.encode(texts, normalize_embeddings=True,
                          batch_size=256, show_progress_bar=True)

np.save(CACHE / "embeddings.npy", embeddings)
(CACHE / "embedding-urls.txt").write_text("\n".join(urls))
(CACHE / "embedding-alts.txt").write_text("\n".join(alts))
print("Done.")
```

```bash
python3 build_semantic_embeddings.py
```

Output files:

| File | Size | Contents |
|---|---|---|
| `embeddings.npy` | ~216 MB | 147k × 384 float32 vectors |
| `embedding-urls.txt` | ~13 MB | Matching URLs |
| `embedding-alts.txt` | ~3 MB | Matching alt texts |

After this step, `emoji-picker-semantic.py` and the semantic portion of `emoji-picker-combined.py` are functional. The daemon auto-starts on first query and stays resident for 10 minutes of idle time.

**First-query startup time**: ~5–10 s (model + 216 MB file load).

---

## Step 3 — Build CLIP Image Embeddings (~65 MB, ~hours for full set)

CLIP embeddings encode the actual images, enabling image-similarity search. There are two approaches:

### Option A — Quick bootstrap (~8k images already downloaded as thumbnails)

Once you've used the pickers a while and accumulated thumbnails in `~/.cache/emoji-wallpaper/thumbs/`, build CLIP embeddings from those:

```bash
emoji-picker-clip.py --build
```

This covers only the cached thumbnails (~8–10k images, ~65 MB output). Enough for good results.

### Option B — Full crawl (all 147k images, ~hours + ~several GB download)

```bash
crawl_emoji_kitchen.py
```

`crawl_emoji_kitchen.py` is not in this repo — it lives in `~/.local/bin/`. Copy it from an existing install or write one using the pattern in `emoji-picker-clip.py`'s `build_embeddings()`. It reads `embedding-urls.txt` as its input list, downloads each image in batches of 64, encodes with `clip-ViT-B-32`, checkpoints every ~4k images, and can be interrupted and resumed.

Output files:

| File | Size (option A) | Contents |
|---|---|---|
| `clip-embeddings.npy` | ~65 MB | N × 512 float32 vectors |
| `clip-urls.txt` | ~3 MB | Matching URLs |
| `clip-alts.txt` | ~660 KB | Matching alt texts |

After this step, `emoji-picker-clip.py` and the CLIP portion of `emoji-picker-combined.py` are functional.

---

## Cache Size Summary

| Path | Size | Notes |
|---|---|---|
| `~/.cache/emoji-wallpaper/` total | ~570 MB | After full setup |
| `search-index.tsv` | 42 MB | Metadata, re-downloads after 1 year |
| `embeddings.npy` | 216 MB | MiniLM text embeddings, 147k entries |
| `clip-embeddings.npy` | 65 MB | CLIP image embeddings, ~8–10k entries |
| `thumbs/` | ~160 MB+ | Downloaded thumbnails, grows with use |

**Model downloads** (one-time, stored in `~/.cache/torch/` or `~/.cache/huggingface/`):

| Model | Size |
|---|---|
| `all-MiniLM-L6-v2` | ~90 MB |
| `clip-ViT-B-32` | ~600 MB |

---

## Step 4 — Bind Keys

Add to your i3 or sway config:

```
# Best overall: combined CLIP + MiniLM semantic search
bindsym $mod+shift+e exec --no-startup-id ~/.local/bin/emoji-picker-combined.py

# Keyword-only (fast, no ML, works offline after index built)
bindsym $mod+shift+w exec --no-startup-id python3 ~/.local/bin/emoji-picker.py

# MiniLM semantic only
bindsym $mod+shift+i exec --no-startup-id ~/.local/bin/emoji-picker-semantic.py
```

Reload config (`$mod+shift+r`) and the pickers are ready.

---

## Daily Wallpaper (Optional)

Add to your session autostart or crontab:

```bash
# crontab -e
@reboot sleep 10 && python3 ~/.local/bin/emoji-wallpaper.py
```

Or in i3 config:
```
exec --no-startup-id python3 ~/.local/bin/emoji-wallpaper.py
```

---

## Tool Reference

| Script | Requires | What it does |
|---|---|---|
| `emoji-wallpaper.py` | Pillow | Daily random wallpaper; also builds search index on first run |
| `emoji-search.py` | Pillow | CLI search by keyword; `--set N` or `--random` to apply |
| `emoji-story.py` | Pillow | Converts a phrase into a PNG emoji strip |
| `emoji-picker.py` | Pillow, rofi | rofi keyword picker → set wallpaper |
| `emoji-picker-semantic.py` | sentence-transformers, rofi | rofi MiniLM semantic picker |
| `emoji-picker-clip.py` | sentence-transformers, rofi | rofi CLIP image-similarity picker |
| `emoji-picker-combined.py` | sentence-transformers, rofi | rofi combined rank-sum picker (best results) |
| `emoji-search-daemon.py` | sentence-transformers | MiniLM daemon (auto-started, 10 min idle timeout) |
| `emoji-combined-daemon.py` | sentence-transformers | Combined daemon (auto-started, 10 min idle timeout) |
