#!/usr/bin/env python3
"""
Turn text into a phrase-per-row PNG with one emoji kitchen image per phrase.

Usage:
  emoji-story.py "the sky shines down. We love it."
  emoji-story.py --output out.png "your text here"
  echo "some text" | emoji-story.py
"""

import sys
import re
import json
import hashlib
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_REPO      = Path(__file__).resolve().parent
CACHE_DIR  = _REPO / "data" / "cache"
THUMB_DIR  = CACHE_DIR / "thumbs"
SOCK_PATH  = CACHE_DIR / "combined-daemon.sock"
DAEMON_PY  = _REPO / "emoji-combined-daemon.py"

# Layout
CANVAS_W    = 820
PADDING     = 44       # outer margin and row padding
IMG_SIZE    = 160      # emoji image square
TEXT_MAX_W  = 420      # max width of text column
IMG_X       = CANVAS_W - PADDING - IMG_SIZE   # right-align image column
FONT_SIZE   = 48
LINE_GAP    = 8        # gap between wrapped lines
BG          = (255, 255, 255)
BORDER      = (40, 40, 40)
DIVIDER     = (210, 210, 210)


def find_font(size):
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def wrap_text(text, font, max_width):
    """Return list of lines that each fit within max_width."""
    words = text.split()
    lines, current = [], ""
    for word in words:
        candidate = (current + " " + word).strip()
        w = font.getbbox(candidate)[2]
        if w <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _start_daemon():
    subprocess.Popen([str(DAEMON_PY)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(75):
        time.sleep(0.2)
        if SOCK_PATH.exists():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(SOCK_PATH))
                s.close()
                return True
            except OSError:
                pass
    return False


def query_daemon(phrase):
    for attempt in range(2):
        if not SOCK_PATH.exists():
            if attempt > 0 or not _start_daemon():
                return None
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(30)
            s.connect(str(SOCK_PATH))
            s.sendall((json.dumps({"query": phrase, "limit": 1}) + "\n").encode())
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
                if data.endswith(b"\n"):
                    break
            s.close()
            results = json.loads(data.decode())
            if isinstance(results, list) and results:
                return results[0]["url"]
        except Exception:
            if SOCK_PATH.exists():
                SOCK_PATH.unlink()
    return None


def split_phrases(text, max_words=5):
    """Split on punctuation boundaries, then cap long chunks at max_words."""
    raw = re.split(r'(?<=[.!?,;:])\s+', text.strip())
    result = []
    for chunk in raw:
        chunk = chunk.strip()
        if not chunk:
            continue
        words = chunk.split()
        if len(words) <= max_words:
            result.append(chunk)
        else:
            for i in range(0, len(words), max_words):
                result.append(" ".join(words[i:i + max_words]))
    return [p for p in result if p]


def get_image(url):
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    name = hashlib.md5(url.encode()).hexdigest() + ".png"
    path = THUMB_DIR / name
    if not path.exists():
        urllib.request.urlretrieve(url, path)
    return Image.open(path).convert("RGBA")


def build_png(pairs, output_path, font):
    # Pre-compute row heights (variable if text wraps)
    row_data = []
    for phrase, url in pairs:
        lines = wrap_text(phrase, font, TEXT_MAX_W)
        line_h = font.getbbox("Ag")[3]
        text_block_h = len(lines) * line_h + (len(lines) - 1) * LINE_GAP
        row_h = max(IMG_SIZE, text_block_h) + PADDING * 2
        row_data.append((lines, url, row_h))

    total_h = sum(r[2] for r in row_data) + PADDING
    canvas = Image.new("RGB", (CANVAS_W, total_h), BG)
    draw = ImageDraw.Draw(canvas)

    y = PADDING
    for i, (lines, url, row_h) in enumerate(row_data):
        mid_y = y + row_h // 2

        # Text: vertically centered block
        line_h = font.getbbox("Ag")[3]
        text_block_h = len(lines) * line_h + (len(lines) - 1) * LINE_GAP
        text_y = mid_y - text_block_h // 2
        for line in lines:
            draw.text(
                (PADDING, text_y), line, font=font,
                fill=(255, 255, 255), stroke_width=3, stroke_fill=(30, 30, 30),
            )
            text_y += line_h + LINE_GAP

        # Emoji image: vertically centered
        img_y = mid_y - IMG_SIZE // 2
        draw.rectangle(
            [IMG_X - 3, img_y - 3, IMG_X + IMG_SIZE + 3, img_y + IMG_SIZE + 3],
            outline=BORDER, width=2,
        )
        if url:
            try:
                emoji = get_image(url).resize((IMG_SIZE, IMG_SIZE), Image.LANCZOS)
                canvas.paste(emoji, (IMG_X, img_y), emoji)
            except Exception:
                pass

        # Divider between rows (skip last)
        if i < len(row_data) - 1:
            div_y = y + row_h
            draw.line([(PADDING, div_y), (CANVAS_W - PADDING, div_y)], fill=DIVIDER, width=1)

        y += row_h

    canvas.save(output_path)
    print(f"Saved: {output_path}")


def main():
    args = sys.argv[1:]
    output = Path("emoji-story.png")

    if "--output" in args:
        idx = args.index("--output")
        output = Path(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    if args:
        text = " ".join(args)
    elif not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    else:
        print(__doc__)
        sys.exit(1)

    phrases = split_phrases(text)
    if not phrases:
        print("No phrases found.")
        sys.exit(1)

    font = find_font(FONT_SIZE)

    print(f"{len(phrases)} phrases. Searching...", flush=True)
    pairs = []
    for phrase in phrases:
        url = query_daemon(phrase)
        label = url.split("/")[-1].replace(".png", "") if url else "none"
        print(f"  {phrase!r:45s} -> {label}")
        pairs.append((phrase, url))

    build_png(pairs, output, font)


if __name__ == "__main__":
    main()
