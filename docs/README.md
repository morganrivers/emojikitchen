# Full Setup Guide

This guide covers everything needed to reproduce all tools from scratch on a new Linux machine.

Only **Step 1** is required to get the keyword picker and wallpaper tools working. Steps 2 and 3 are optional upgrades that add semantic and image-similarity search.

---

## Prerequisites

**System packages** (install via your distro's package manager):
```bash
sudo apt install rofi feh xrandr
# clipboard: pick one based on your session type
sudo apt install xclip          # X11 (most common on Ubuntu)
sudo apt install wl-clipboard   # Wayland (Ubuntu 22.04+ default GNOME session)
```

The pickers auto-detect which clipboard tool is available at runtime, so install whichever matches your session. If unsure, install both — they don't conflict.

**Python environment** *(only needed for optional Steps 2 and 3)*: The ML scripts require a Python environment with heavy ML libraries.

**Option A — pip + venv** (no extra tools needed):
```bash
python3 -m venv ~/.venv/emojikitchen
source ~/.venv/emojikitchen/bin/activate
pip install Pillow sentence-transformers numpy torch torchvision
```

**Option B — conda / micromamba** (micromamba is a drop-in conda replacement):
```bash
# conda:
conda create -n emojikitchen python=3.11
conda activate emojikitchen

# or micromamba:
micromamba create -n emojikitchen python=3.11
micromamba activate emojikitchen

pip install Pillow sentence-transformers numpy torch torchvision
```

The basic scripts (`emoji-wallpaper.py`, `emoji-search.py`) only need `Pillow` and the standard library, so `pip install Pillow` alone works for those.

---

## Install Scripts

Copy all scripts to `~/.local/bin/` and mark them executable, then drop the pre-built embeddings into the cache:

```bash
cp emoji-wallpaper.py emoji-search.py emoji-story.py \
   emoji-picker.py emoji-picker-semantic.py emoji-picker-clip.py \
   emoji-picker-combined.py emoji-search-daemon.py \
   emoji-combined-daemon.py ~/.local/bin/

chmod +x ~/.local/bin/emoji-*.py

mkdir -p ~/.cache/emoji-wallpaper
cp embeddings/*.npy embeddings/*.txt ~/.cache/emoji-wallpaper/
```

If using a venv or conda/micromamba env, update the shebang lines on the ML scripts so they use that env's Python directly (needed when running as a keybind, where `PATH` may not include the activated env):

```bash
# venv (path printed by: echo ~/.venv/emojikitchen/bin/python3):
PYBIN="$HOME/.venv/emojikitchen/bin/python3"

# or conda/micromamba (path printed by: conda run -n emojikitchen which python3):
PYBIN="$HOME/miniconda3/envs/emojikitchen/bin/python3"
# or for micromamba:
PYBIN="$HOME/micromamba/envs/emojikitchen/bin/python3"

sed -i "1s|.*|#!${PYBIN}|" \
  ~/.local/bin/emoji-picker-semantic.py \
  ~/.local/bin/emoji-picker-combined.py \
  ~/.local/bin/emoji-picker-clip.py \
  ~/.local/bin/emoji-search-daemon.py \
  ~/.local/bin/emoji-combined-daemon.py \
  ~/.local/bin/emoji-story.py
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

## Step 2 — Build Semantic (MiniLM) Embeddings *(optional, ~216 MB, ~10 min)*

Unlocks the **semantic search** option in `emoji-picker.py` and makes `emoji-picker-semantic.py` functional. Encodes all 147k emoji descriptions with `all-MiniLM-L6-v2` so you can search by meaning rather than exact keywords.

Run the included script (it also builds the search index if Step 1 hasn't been done yet):

```bash
bash build-semantic-embeddings.sh
```

Pass `--force` to rebuild if the embeddings file already exists.

Output files:

| File | Size | Contents |
|---|---|---|
| `embeddings.npy` | ~216 MB | 147k × 384 float32 vectors |
| `embedding-urls.txt` | ~13 MB | Matching URLs |
| `embedding-alts.txt` | ~3 MB | Matching alt texts |

After this step, `emoji-picker-semantic.py` and the semantic portion of `emoji-picker-combined.py` are functional. The daemon auto-starts on first query and stays resident for 10 minutes of idle time.

**First-query startup time**: ~5–10 s (model + 216 MB file load).

---

## Step 3 — Build CLIP Image Embeddings *(optional, ~65 MB)*

Unlocks `emoji-picker-clip.py` and the combined CLIP+MiniLM mode. CLIP encodes the actual images, so you can find results by visual similarity rather than text. There are two approaches:

### Option A — Quick bootstrap (~8k images already downloaded as thumbnails)

Once you've used the pickers a while and accumulated thumbnails in `~/.cache/emoji-wallpaper/thumbs/`, build CLIP embeddings from those:

```bash
bash build-clip-embeddings.sh
```

This covers only the cached thumbnails (~8–10k images, ~65 MB output). Enough for good results. Pass `--force` to rebuild.

### Option B — Full crawl (all 147k images, ~hours + ~several GB download)

Requires Step 2 to have been run first (needs `embedding-urls.txt`).

```bash
python3 embed-all-emojikitchen-clip.py
```

Downloads all 147k images on demand in batches of 64, encodes each with `clip-ViT-B-32`, checkpoints every ~4k images, and can be interrupted and resumed. Pass `--reset` to start over.

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
# Works after Step 1 only — keyword search, no ML required
bindsym $mod+shift+e exec --no-startup-id python3 ~/.local/bin/emoji-picker.py

# Requires Step 2 — MiniLM semantic search (the picker also exposes this as a menu option)
bindsym $mod+shift+i exec --no-startup-id ~/.local/bin/emoji-picker-semantic.py

# Requires Steps 2 + 3 — best results, combined CLIP + MiniLM
bindsym $mod+shift+c exec --no-startup-id ~/.local/bin/emoji-picker-combined.py
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

| Script | Requires | Optional? | What it does |
|---|---|---|---|
| `emoji-wallpaper.py` | Pillow | no | Daily random wallpaper; builds search index on first run |
| `emoji-search.py` | Pillow | no | CLI search by keyword; `--set N` or `--random` to apply |
| `emoji-story.py` | Pillow | no | Converts a phrase into a PNG emoji strip |
| `emoji-picker.py` | Pillow, rofi | no | rofi picker — keyword always available; semantic option appears after Step 2 |
| `emoji-picker-semantic.py` | sentence-transformers, rofi | yes (Step 2) | rofi MiniLM semantic picker |
| `emoji-picker-clip.py` | sentence-transformers, rofi | yes (Step 3) | rofi CLIP image-similarity picker |
| `emoji-picker-combined.py` | sentence-transformers, rofi | yes (Steps 2+3) | rofi combined rank-sum picker (best results) |
| `emoji-search-daemon.py` | sentence-transformers | yes (Step 2) | MiniLM daemon (auto-started, 10 min idle timeout) |
| `emoji-combined-daemon.py` | sentence-transformers | yes (Steps 2+3) | Combined daemon (auto-started, 10 min idle timeout) |
