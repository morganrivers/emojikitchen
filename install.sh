#!/bin/bash
# One-shot installer for emojikitchen.
# Works two ways:
#   1. Remote:  curl -sSL https://github.com/OWNER/emojikitchen/releases/latest/download/install.sh | bash
#   2. Local:   bash install.sh   (from inside the cloned repo)
set -euo pipefail

# ── uninstall ─────────────────────────────────────────────────────────────────

if [ "${1:-}" = "uninstall" ]; then
    INSTALL_DIR="${EMOJIKITCHEN_DIR:-$HOME/.local/share/emojikitchen}"
    BIN="$HOME/.local/bin"
    CACHE="${XDG_CACHE_HOME:-$HOME/.cache}"

    echo "Removing $INSTALL_DIR ..."
    rm -rf "$INSTALL_DIR"

    echo "Removing wrappers from $BIN ..."
    rm -f "$BIN"/emoji-*

    echo ""
    echo "The following ML model caches will also be removed:"
    echo "  $CACHE/fastembed/models--Qdrant--clip-ViT-B-32-text"
    echo "  $CACHE/fastembed/models--sentence-transformers--all-MiniLM-L6-v2"
    echo "  $CACHE/torch/sentence_transformers/clip-ViT-B-32"
    echo "  $CACHE/torch/sentence_transformers/sentence-transformers_all-MiniLM-L6-v2"
    echo ""
    echo "If these are used by other projects, press Ctrl+C to cancel."
    read -r -p "Press Enter to continue..." < /dev/tty

    echo "Removing ML model caches ..."
    rm -rf \
        "$CACHE/fastembed/models--Qdrant--clip-ViT-B-32-text" \
        "$CACHE/fastembed/models--sentence-transformers--all-MiniLM-L6-v2" \
        "$CACHE/torch/sentence_transformers/clip-ViT-B-32" \
        "$CACHE/torch/sentence_transformers/sentence-transformers_all-MiniLM-L6-v2"

    echo "Uninstalled."
    exit 0
fi

REPO_URL="https://github.com/morganrivers/emojikitchen"
RELEASE_URL="$REPO_URL/releases/latest/download"

INSTALL_DIR="${EMOJIKITCHEN_DIR:-$HOME/.local/share/emojikitchen}"
BIN="$HOME/.local/bin"
VENV="$INSTALL_DIR/.venv"

# ── 1. get scripts ────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd)" || true
if [ -f "$SCRIPT_DIR/emoji-picker.py" ]; then
    # Running from inside the repo - use it in place
    INSTALL_DIR="$SCRIPT_DIR"
    VENV="$INSTALL_DIR/.venv"
    echo "Using local repo at $INSTALL_DIR"
else
    echo "Installing emojikitchen to $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
    echo "Downloading scripts..."
    for script in \
        emoji-picker.py \
        emoji-picker-clip.py \
        emoji-picker-semantic.py \
        emoji-picker-combined.py \
        emoji-wallpaper.py \
        emoji-story.py \
        emoji-search-daemon.py \
        emoji-split-daemon.py \
        requirements.txt
    do
        curl -sSL "$RELEASE_URL/$script" -o "$INSTALL_DIR/$script"
    done
fi

# ── 2. get data ───────────────────────────────────────────────────────────────

DATA_DIR="$INSTALL_DIR/data/embeddings"
if [ ! -f "$DATA_DIR/clip-embeddings-pca256.npy" ]; then
    echo "Downloading data (~150 MB compressed)..."
    mkdir -p "$INSTALL_DIR/data"
    curl -L --progress-bar "$RELEASE_URL/data.tar.gz" \
        | tar -xz -C "$INSTALL_DIR"
    echo "Data extracted."
else
    echo "Data already present, skipping download."
fi

# ── 3. python venv ────────────────────────────────────────────────────────────

if [ ! -d "$VENV" ]; then
    echo "Creating virtualenv..."
    python3 -m venv "$VENV"
fi

echo "Installing Python dependencies..."
"$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# ── 4. symlink scripts ────────────────────────────────────────────────────────

mkdir -p "$BIN"

# wrapper so scripts run inside the venv without the user activating it
for script in "$INSTALL_DIR"/emoji-*.py; do
    name="$(basename "$script" .py)"
    wrapper="$BIN/$name"
    cat > "$wrapper" <<EOF
#!/bin/bash
exec "$VENV/bin/python3" "$script" "\$@"
EOF
    chmod +x "$wrapper"
done

echo ""
case ":${PATH}:" in
    *":$BIN:"*)
        echo "Done. Run:"
        echo "  emoji-picker"
        ;;
    *)
        case "$(basename "${SHELL:-}")" in
            zsh)  RC="$HOME/.zshrc" ;;
            *)    RC="$HOME/.bashrc" ;;
        esac
        echo "Done, but $BIN is not on your PATH."
        echo "Add it by running:"
        echo "  echo 'export PATH=\"$BIN:\$PATH\"' >> $RC && source $RC"
        echo "Then run:"
        echo "  emoji-picker"
        ;;
esac
