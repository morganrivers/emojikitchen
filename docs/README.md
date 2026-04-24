# Full Setup Guide

---

## Install

**System packages:**
```bash
sudo apt install rofi feh xrandr
# clipboard: pick one based on your session type
sudo apt install xclip          # X11
sudo apt install wl-clipboard   # Wayland
```

**Clone and install:**
```bash
git clone https://github.com/morganrivers/emojikitchen
cd emojikitchen
bash install.sh
```

`install.sh` creates a `.venv`, installs `requirements.txt` (`Pillow`, `numpy`, `fastembed`), and symlinks all scripts into `~/.local/bin`. All embedding data is included in the repo - no separate download step needed.

---

## Bind Keys

For i3 or sway, use the full path to the venv's Python so the keybinding works outside an activated shell:

```
bindsym $mod+shift+e exec --no-startup-id /path/to/repo/.venv/bin/python3 /path/to/repo/emoji-picker.py
```

All pickers work immediately after install. `emoji-picker.py` offers keyword search and semantic search from the same menu.

---

## Optional - Rebuild Embeddings

The repo ships with pre-built embeddings. You only need these if you want to regenerate them from scratch.

**Semantic (MiniLM) embeddings** - ~10 min, requires `sentence-transformers`:
```bash
pip install sentence-transformers
bash build-semantic-embeddings.sh
```

**CLIP image embeddings** - builds from cached thumbnails:
```bash
pip install sentence-transformers
bash build-clip-embeddings.sh
```

For a full crawl of all 147k images (hours, several GB download):
```bash
python3 embed-all-emojikitchen-clip.py
```

---

## Daily Wallpaper (Optional)

In your i3 config:
```
exec --no-startup-id /path/to/repo/.venv/bin/python3 /path/to/repo/emoji-wallpaper.py
```

Or via crontab:
```bash
@reboot sleep 10 && /path/to/repo/.venv/bin/python3 /path/to/repo/emoji-wallpaper.py
```

---

## Data Size

| Path | Size | Notes |
|---|---|---|
| `data/embeddings/search-index.tsv` | 42 MB | Keyword search index |
| `data/embeddings/embeddings-pca340.npy` | ~95 MB | MiniLM text embeddings (PCA-compressed) |
| `data/embeddings/clip-embeddings-pca256.npy` | 72 MB | CLIP image embeddings (PCA-compressed) |
| `data/cache/thumbs/` | grows with use | Downloaded thumbnails |

Full-size embeddings (`embeddings.npy`, `clip-embeddings.npy`) are gitignored - only the PCA-compressed versions ship with the repo.

**Model downloads** (one-time, stored in `~/.cache/`):

| Model | Size |
|---|---|
| `fastembed` default model | ~90 MB |
| `clip-ViT-B-32` | ~600 MB |

---

## Tool Reference

| Script | What it does |
|---|---|
| `emoji-wallpaper.py` | Daily random wallpaper |
| `emoji-search.py` | CLI search; `--set N` or `--random` to apply wallpaper |
| `emoji-story.py` | Converts a phrase into a PNG emoji strip |
| `emoji-picker.py` | rofi picker - keyword + semantic search |
| `emoji-picker-semantic.py` | rofi semantic-only picker |
| `emoji-picker-clip.py` | rofi CLIP image-similarity picker |
| `emoji-picker-combined.py` | rofi combined CLIP + MiniLM picker (best results) |
| `emoji-search-daemon.py` | MiniLM daemon (auto-started, 10 min idle timeout) |
