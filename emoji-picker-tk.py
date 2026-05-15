#!/usr/bin/env python3
"""
Emoji kitchen picker - tkinter UI replacing rofi.
Results appear one-by-one as thumbnails download. Borderless, half-screen, centered.

Bind in i3 config:
  bindsym $mod+shift+e exec --no-startup-id python3 ~/.local/bin/emoji-picker-tk.py
"""

import sys, os, re, json, hashlib, shutil, socket, subprocess
import time, urllib.request, threading, random as _random
from pathlib import Path
import tkinter as tk
from tkinter import ttk

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

_REPO        = Path(__file__).resolve().parent
DATA_DIR     = _REPO / "data" / "embeddings"
CACHE_DIR    = _REPO / "data" / "cache"
_VENV_PY     = _REPO / ".venv" / "bin" / "python3"
_PYTHON      = str(_VENV_PY) if _VENV_PY.exists() else sys.executable
SEARCH_INDEX = DATA_DIR / "search-index.tsv"
THUMB_DIR    = CACHE_DIR / "thumbs"
WALLPAPER_PATH   = CACHE_DIR / "wallpaper.png"
SOCK_PATH    = CACHE_DIR / "split-daemon.sock"
DAEMON_PY    = _REPO / "emoji-split-daemon.py"
DAEMON_PID   = CACHE_DIR / "split-daemon.pid"
DAEMON_LOG   = CACHE_DIR / "split-daemon.log"
DATA_TARBALL_URL = "https://github.com/morganrivers/emojikitchen/releases/latest/download/data.tar.gz"

TILE_SIZE   = 200
MAX_RESULTS = 5000
SHOW_BROKEN_THUMBS = False
BATCH_SIZE  = 100
LOAD_MORE   = "⬇  load more results..."
STORY_PY    = _REPO / "emoji-story.py"
STORY_OUT   = Path("/tmp/emoji-story.png")

PRIORITY_EMOJIS = frozenset({
    "100", "bird", "boom", "bouquet", "brain", "broccoli", "car", "carrot",
    "cat", "city_sunrise", "cloud", "coconut", "coffee", "computer",
    "crystal_ball", "dango", "derelict_house_building", "dragon", "earth_africa",
    "exclamation", "exploding_head", "face_with_raised_eyebrow",
    "face_with_rolling_eyes", "facepunch", "fire", "fish", "frog",
    "glass_of_milk", "goose", "headphones", "hearts", "hole", "hot_pepper",
    "house", "imp", "iphone", "koala", "last_quarter_moon_with_face", "lemon",
    "lightning", "llama", "low_battery", "magic_wand", "milky_way", "mouse",
    "musical_keyboard", "national_park", "neutral_face", "octopus", "ok",
    "parachute", "people_hugging", "pleading_face", "rain_cloud", "rainbow",
    "relieved", "shrug", "skunk", "snail", "sob", "sparkling_heart",
    "sunrise_over_mountains", "sunglasses", "sushi", "taco", "tiger", "tornado",
    "tropical_drink", "tulip", "turtle", "unicorn_face", "upside_down_face",
    "volcano", "whale", "white_check_mark", "wood", "yum",
})


def _keyword_priority(alt):
    parts = alt.split("-", 1)
    is_priority = any(p in PRIORITY_EMOJIS for p in parts)
    return (0 if is_priority else 1, _random.Random(alt).random())


def _notify(msg):
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-t", "3000", "Emoji Kitchen", msg],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        print(msg, file=sys.stderr)


def copy_image_to_clipboard(path):
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        cmd = ["wl-copy", "--type", "image/png"]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard", "-t", "image/png"]
    else:
        _notify("No clipboard tool: install xclip (X11) or wl-clipboard (Wayland)")
        return
    with open(path, "rb") as f:
        subprocess.run(cmd, stdin=f, check=True)


def download_data_with_progress(progress_cb):
    """
    Download and extract the semantic-models tarball.
    progress_cb(downloaded_bytes, total_bytes) called periodically.
    Returns None on success, error string on failure.
    """
    import tarfile, io
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(DATA_TARBALL_URL,
                                     headers={"User-Agent": "emojikitchen"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            buf = io.BytesIO()
            downloaded = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                buf.write(chunk)
                downloaded += len(chunk)
                progress_cb(downloaded, total)
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:gz") as tf:
            tf.extractall(_REPO)
        return None
    except Exception as e:
        return str(e)


def _daemon_alive():
    if not DAEMON_PID.exists():
        return False
    try:
        pid = int(DAEMON_PID.read_text().strip())
    except (ValueError, OSError):
        DAEMON_PID.unlink(missing_ok=True)
        return False
    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.exists():
        DAEMON_PID.unlink(missing_ok=True)
        return False
    try:
        for line in (proc_dir / "status").read_text().splitlines():
            if line.startswith("State:") and "Z" in line.split(":", 1)[1]:
                DAEMON_PID.unlink(missing_ok=True)
                return False
        if b"emoji-split-daemon" not in (proc_dir / "cmdline").read_bytes():
            DAEMON_PID.unlink(missing_ok=True)
            return False
    except OSError:
        DAEMON_PID.unlink(missing_ok=True)
        return False
    return True


def _spawn_daemon():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(DAEMON_LOG, "wb")
    proc = subprocess.Popen([_PYTHON, str(DAEMON_PY)],
                             stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
    DAEMON_PID.write_text(str(proc.pid))
    return proc


def _wait_for_socket(timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if SOCK_PATH.exists():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(SOCK_PATH))
                s.close()
                return True
            except OSError:
                pass
        time.sleep(0.2)
    return False


def query_daemon(query, limit=MAX_RESULTS):
    if not SOCK_PATH.exists():
        if not _daemon_alive():
            _spawn_daemon()
        if not _wait_for_socket(5):
            return "loading" if _daemon_alive() else None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect(str(SOCK_PATH))
        s.sendall((json.dumps({"query": query, "limit": limit}) + "\n").encode())
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
        if isinstance(results, list):
            return [(r["rank"], r["alt"], r["url"], "") for r in results]
    except Exception:
        if SOCK_PATH.exists():
            SOCK_PATH.unlink()
        return "loading" if _daemon_alive() else None
    return None


def load_index():
    entries = []
    with open(SEARCH_INDEX) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) == 3:
                entries.append((parts[0], parts[1], parts[2]))
    return entries


def _score_entry(alt, words, is_single):
    alt_lower  = alt.lower()
    alt_tokens = set(re.split(r'[-_]', alt_lower))
    name_score = sum(1 for w in words if w in alt_tokens)
    square_score = 0
    if is_single:
        parts = alt_lower.split('-', 1)
        if len(parts) == 2 and parts[0] == parts[1]:
            if words[0] in set(parts[0].split('_')):
                square_score = 1
    return square_score, name_score


def search(entries, query, limit=MAX_RESULTS):
    words    = query.lower().split()
    patterns = [re.compile(r'\b' + re.escape(w) + r'\b') for w in words]
    is_single = len(words) == 1
    scored = []
    for url, alt, text in entries:
        text_score = sum(1 for p in patterns if p.search(text.lower()))
        if text_score > 0:
            sq, ns = _score_entry(alt, words, is_single)
            scored.append((sq, ns, text_score, alt, url, text))
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2], _keyword_priority(x[3])))
    if scored:
        return [(ts, alt, url, text) for _, _, ts, alt, url, text in scored[:limit]]
    for url, alt, text in entries:
        text_score = sum(1 for w in words if w in text.lower())
        if text_score > 0:
            sq, ns = _score_entry(alt, words, is_single)
            scored.append((sq, ns, text_score, alt, url, text))
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2], _keyword_priority(x[3])))
    return [(ts, alt, url, text) for _, _, ts, alt, url, text in scored[:limit]]


def url_to_base_emojis(url):
    m = re.search(r'/u([0-9a-f]+)(?:-u[0-9a-f]+)*_u([0-9a-f]+)(?:-u[0-9a-f]+)*\.png$',
                  url, re.IGNORECASE)
    if m:
        try:
            return chr(int(m.group(1), 16)) + chr(int(m.group(2), 16))
        except (ValueError, OverflowError):
            pass
    return ""


def format_label(alt, url, text):
    base = url_to_base_emojis(url)
    base_str = f"  {base}" if base else ""
    if not text:
        return f"{alt}{base_str}"
    return f"{alt}{base_str}  ({text})"


def build_base_emoji_index(entries):
    seen = {}
    for url, alt, _text in entries:
        m = re.search(r'/u([0-9a-f]+)(?:-u[0-9a-f]+)*_u([0-9a-f]+)(?:-u[0-9a-f]+)*\.png$',
                      url, re.IGNORECASE)
        if not m:
            continue
        hex1, hex2 = m.group(1).lower(), m.group(2).lower()
        parts = alt.split('-', 1)
        name1, name2 = (parts[0], parts[1]) if len(parts) == 2 else (alt, alt)
        for hex_code, name in [(hex1, name1), (hex2, name2)]:
            if hex_code not in seen:
                try:
                    seen[hex_code] = (chr(int(hex_code, 16)), name)
                except (ValueError, OverflowError):
                    pass
    return sorted(seen.items(), key=lambda x: x[1][1])


_THUMB_LIMIT = 200 * 1024 * 1024


def _trim_thumb_cache():
    entries, total = [], 0
    for p in THUMB_DIR.glob("*.png"):
        st = p.stat()
        entries.append((st.st_mtime, st.st_size, p))
        total += st.st_size
    if total <= _THUMB_LIMIT:
        return
    entries.sort()
    for _, size, p in entries:
        if total <= _THUMB_LIMIT:
            break
        p.unlink(missing_ok=True)
        total -= size


def get_thumb(url):
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    name = hashlib.md5(url.encode()).hexdigest() + ".png"
    path = THUMB_DIR / name
    if path.exists() and path.stat().st_size > 0:
        return str(path)
    if path.exists():
        path.unlink(missing_ok=True)
    tmp = path.with_suffix(".png.tmp")
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "emojikitchen-picker"})
            with urllib.request.urlopen(req, timeout=10) as resp, open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
            if tmp.stat().st_size > 0:
                tmp.replace(path)
                return str(path)
        except Exception:
            pass
        tmp.unlink(missing_ok=True)
        if attempt == 0:
            time.sleep(0.3)
    return None


def set_wallpaper(url, alt):
    if not HAS_PIL:
        _notify("Pillow not installed - run: pip install Pillow")
        return
    nitrogen_cfg = Path.home() / ".config" / "nitrogen" / "bg-saved.cfg"
    if not shutil.which("feh") and not shutil.which("nitrogen") and not nitrogen_cfg.exists():
        _notify("Install feh or nitrogen to set wallpapers")
        return
    cached = get_thumb(url)
    if not cached:
        _notify(f"Could not get image for: {alt}")
        return
    emoji_img = Image.open(cached).convert("RGBA")
    width, height = 1920, 1080
    try:
        out = subprocess.check_output(["xrandr", "--current"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if " connected" in line:
                m = re.search(r"(\d+)x(\d+)\+", line)
                if m:
                    width, height = int(m.group(1)), int(m.group(2))
                    break
    except Exception:
        pass
    tile_size = int(os.environ.get("EMOJI_TILE_SIZE", TILE_SIZE))
    emoji_img = emoji_img.resize((tile_size, tile_size), Image.LANCZOS)
    wallpaper = Image.new("RGBA", (width, height), "white")
    for y in range(0, height, tile_size):
        for x in range(0, width, tile_size):
            wallpaper.paste(emoji_img, (x, y), emoji_img)
    wallpaper.convert("RGB").save(WALLPAPER_PATH)
    if nitrogen_cfg.exists() or shutil.which("nitrogen"):
        try:
            nitrogen_cfg.parent.mkdir(parents=True, exist_ok=True)
            nitrogen_cfg.write_text(f"[xin_-1]\nfile={WALLPAPER_PATH}\nmode=5\nbgcolor=#000000\n")
            subprocess.run(["nitrogen", "--restore"], check=True, capture_output=True)
            return
        except Exception:
            pass
    try:
        subprocess.run(["feh", "--bg-fill", str(WALLPAPER_PATH)], check=True, capture_output=True)
    except Exception:
        _notify(f"Wallpaper saved but couldn't set it: {WALLPAPER_PATH}")


_PIL_EMOJI_FONT  = None   # PIL ImageFont, loaded once
_PIL_EMOJI_CACHE = {}     # char -> PIL Image (or None on failure)


def _find_emoji_ttf():
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/noto/NotoColorEmoji.ttf",
        "/System/Library/Fonts/Apple Color Emoji.ttc",
        "C:/Windows/Fonts/seguiemj.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    try:
        import subprocess
        out = subprocess.check_output(
            ["fc-list", "Noto Color Emoji", "--format=%{file}\n"],
            text=True, timeout=3)
        path = out.strip().splitlines()[0]
        if path and Path(path).exists():
            return path
    except Exception:
        pass
    return None


def _get_pil_emoji_font():
    global _PIL_EMOJI_FONT
    if _PIL_EMOJI_FONT is not None:
        return _PIL_EMOJI_FONT
    if not HAS_PIL:
        return None
    from PIL import ImageFont
    ttf = _find_emoji_ttf()
    if not ttf:
        return None
    try:
        _PIL_EMOJI_FONT = ImageFont.truetype(ttf, 109)
    except Exception:
        return None
    return _PIL_EMOJI_FONT


def render_emoji_pil(char, size=20):
    """Render an emoji char to a PIL Image using the system color emoji font."""
    if char in _PIL_EMOJI_CACHE:
        return _PIL_EMOJI_CACHE[char]
    font = _get_pil_emoji_font()
    if not font or not HAS_PIL:
        _PIL_EMOJI_CACHE[char] = None
        return None
    try:
        from PIL import Image, ImageDraw
        canvas = Image.new("RGBA", (150, 150), (0, 0, 0, 0))
        ImageDraw.Draw(canvas).text((10, 10), char, font=font, embedded_color=True)
        bbox = canvas.getbbox()
        if not bbox:
            _PIL_EMOJI_CACHE[char] = None
            return None
        img = canvas.crop(bbox).resize((size, size), Image.LANCZOS)
        _PIL_EMOJI_CACHE[char] = img
        return img
    except Exception:
        _PIL_EMOJI_CACHE[char] = None
        return None


# ── TkPicker ──────────────────────────────────────────────────────────────────

class TkPicker:
    BG       = "#ffffff"
    FG       = "#222222"
    FG_DIM   = "#999999"
    ENTRY_BG = "#f5f5f5"
    ACCENT   = "#6633cc"
    THUMB    = 64

    # Pastel rainbow stripes for rows (normal / selected)
    RAINBOW_ROW = ["#fff0f0", "#fff8ee", "#fdfff0", "#f0fff5", "#f0f8ff", "#faf0ff"]
    RAINBOW_SEL = ["#ffbbbb", "#ffd4a8", "#f5ffaa", "#aaffcc", "#b8ddff", "#dbb8ff"]

    def __init__(self):
        root = tk.Tk()
        root.configure(bg=self.BG)
        root.title("Emoji Kitchen")
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        side = min(sw, sh) // 2
        root.geometry(f"{side}x{side}+{(sw - side)//2}+{(sh - side)//2}")
        self._floating = self._setup_floating(root)
        self.root      = root
        self._result   = None
        self._mode     = "input"
        self._rows     = []
        self._sel      = -1
        self._img_refs = []
        self._options  = []
        self._trace_id = None
        self._build()

    def _build(self):
        root = self.root

        # ── top bar ───────────────────────────────────────────────────────
        top = tk.Frame(root, bg=self.BG)
        top.pack(fill="x", padx=10, pady=(10, 4))

        self._prompt_frame = tk.Frame(top, bg=self.BG)
        self._prompt_frame.pack(fill="x")

        self._entry_var = tk.StringVar()
        self._entry = tk.Entry(top, textvariable=self._entry_var,
                               bg=self.ENTRY_BG, fg=self.FG,
                               insertbackground=self.FG,
                               font=("Helvetica", 13),
                               relief="flat", bd=6,
                               highlightthickness=2,
                               highlightbackground="#dddddd",
                               highlightcolor=self.ACCENT)
        self._entry.pack(fill="x", pady=(4, 0))

        # Entry must handle its own nav keys — root bindings don't fire when
        # the Entry has focus because Entry consumes Up/Down/Escape/Return.
        self._entry.bind("<Escape>", self._cancel)
        self._entry.bind("<Return>", self._on_return)
        self._entry.bind("<Up>",     lambda e: (self._up(),   "break")[1])
        self._entry.bind("<Down>",   lambda e: (self._down(), "break")[1])

        # ── progress bar (hidden until explicitly shown) ──────────────────
        self._prog_frame = tk.Frame(root, bg=self.BG)
        self._prog_var     = tk.DoubleVar(value=0)
        self._prog_lbl_var = tk.StringVar()
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("lgt.Horizontal.TProgressbar",
                        troughcolor="#e8e8e8", background=self.ACCENT,
                        bordercolor=self.BG, lightcolor=self.ACCENT,
                        darkcolor=self.ACCENT)
        self._progbar = ttk.Progressbar(self._prog_frame,
                                        variable=self._prog_var,
                                        style="lgt.Horizontal.TProgressbar",
                                        mode="determinate")
        self._progbar.pack(fill="x", padx=10, pady=(4, 0))
        tk.Label(self._prog_frame, textvariable=self._prog_lbl_var,
                 bg=self.BG, fg=self.FG_DIM,
                 font=("Helvetica", 9), anchor="e", padx=10
                 ).pack(fill="x")

        # ── scrollable list ───────────────────────────────────────────────
        list_outer = tk.Frame(root, bg=self.BG)
        list_outer.pack(fill="both", expand=True, padx=2)

        style.configure("lgt.Vertical.TScrollbar",
                        background="#dddddd", troughcolor=self.BG,
                        bordercolor=self.BG, arrowcolor=self.FG_DIM)

        self._canvas = tk.Canvas(list_outer, bg=self.BG, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(list_outer, orient="vertical",
                           command=self._canvas.yview,
                           style="lgt.Vertical.TScrollbar")
        self._canvas.configure(yscrollcommand=sb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._inner = tk.Frame(self._canvas, bg=self.BG)
        self._win_id = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfig(self._win_id, width=e.width))

        for w in (self._canvas, self._inner):
            w.bind("<MouseWheel>", self._on_scroll)
            w.bind("<Button-4>",   self._on_scroll)
            w.bind("<Button-5>",   self._on_scroll)

        root.bind("<Escape>", self._cancel)
        root.bind("<Up>",     self._up)
        root.bind("<Down>",   self._down)
        root.bind("<Return>", self._on_return)

    @staticmethod
    def _setup_floating(root):
        """
        Make root borderless and floating.
        On Linux/X11: use -type splash (WM hint, no overrideredirect — keyboard
        focus stays under normal WM rules).
        Fallback (macOS, Windows, or if splash unsupported): overrideredirect +
        topmost; focus/grab are deferred via after() so the window is mapped first.
        Returns True if floating was applied.
        """
        if sys.platform.startswith("linux"):
            try:
                root.wm_attributes("-type", "splash")
                return True
            except tk.TclError:
                pass
        # Fallback: full bypass — needs deferred grab+focus (done in _run)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        return True

    def _run(self):
        # Defer focus/grab so the window is fully mapped before we grab input.
        # With -type splash the WM handles focus; with overrideredirect we need
        # focus_force + grab_set to capture keyboard events.
        def _activate():
            self.root.focus_force()
            self._entry.focus_force()
            try:
                self.root.grab_set()
            except tk.TclError:
                pass
        self.root.after(1, _activate)
        self.root.mainloop()
        try:
            self.root.grab_release()
        except tk.TclError:
            pass
        return self._result

    def _set_prompt(self, text):
        for w in self._prompt_frame.winfo_children():
            w.destroy()
        widgets = self._pack_rich_label(
            self._prompt_frame, text, self.BG,
            font=("Helvetica", 11, "bold"), pady=2)
        for w in widgets:
            w.configure(fg=self.ACCENT)

    # ── scrolling ─────────────────────────────────────────────────────────────

    def _on_scroll(self, e):
        # Treat scroll wheel as Up/Down so the selection moves (rofi-style).
        if e.num == 5 or (e.num == 0 and (e.delta or 0) < 0):
            self._down()
        else:
            self._up()
        return "break"

    # ── keyboard ──────────────────────────────────────────────────────────────

    def _cancel(self, e=None):
        self._result = None
        self.root.quit()

    def _on_return(self, e=None):
        if self._mode == "input":
            val = self._entry_var.get().strip()
            self._result = val or None
            self.root.quit()
        elif self._mode in ("list", "imagelist"):
            if self._sel >= 0 and self._rows:
                self._result = self._rows[self._sel]["label"]
            else:
                val = self._entry_var.get().strip()
                self._result = val or None
            self.root.quit()
        elif self._mode == "showimage":
            self._result = "copy"
            self.root.quit()

    def _up(self, e=None):
        if self._rows and self._sel > 0:
            self._select(self._sel - 1)

    def _down(self, e=None):
        if self._rows and self._sel < len(self._rows) - 1:
            self._select(self._sel + 1)

    # ── selection ─────────────────────────────────────────────────────────────

    def _select(self, idx, scroll=True):
        if 0 <= self._sel < len(self._rows):
            self._color_row(self._sel, selected=False)
        self._sel = idx
        if 0 <= idx < len(self._rows):
            self._color_row(idx, selected=True)
            if scroll:
                self._scroll_into_view(idx)

    def _scroll_into_view(self, idx):
        self.root.update_idletasks()
        row = self._rows[idx]["frame"]
        row_y = row.winfo_y()
        row_h = row.winfo_reqheight()
        canvas_h = self._canvas.winfo_height()
        total_h  = self._inner.winfo_reqheight()
        if total_h <= 0:
            return
        view_top = self._canvas.yview()[0] * total_h
        view_bot = self._canvas.yview()[1] * total_h
        if row_y < view_top:
            self._canvas.yview_moveto(row_y / total_h)
        elif row_y + row_h > view_bot:
            self._canvas.yview_moveto((row_y + row_h - canvas_h) / total_h)

    def _color_row(self, idx, selected):
        rd = self._rows[idx]
        color = rd["sel_bg"] if selected else rd["row_bg"]
        row = rd["frame"]
        try:
            row.configure(bg=color)
            for w in row.winfo_children():
                try: w.configure(bg=color)
                except tk.TclError: pass
        except tk.TclError:
            pass

    def _click_row(self, idx):
        self._select(idx, scroll=False)
        self._result = self._rows[idx]["label"]
        self.root.quit()

    # ── state management ──────────────────────────────────────────────────────

    def _reset(self):
        if self._trace_id:
            try: self._entry_var.trace_remove("write", self._trace_id)
            except Exception: pass
            self._trace_id = None
        for w in self._inner.winfo_children():
            w.destroy()
        self._rows     = []
        self._img_refs = []
        self._sel      = -1
        self._entry_var.set("")
        self._prog_frame.pack_forget()
        self._progbar.configure(mode="determinate")
        if not self._entry.winfo_ismapped():
            self._entry.pack(fill="x", pady=(4, 0))

    # ── public API ────────────────────────────────────────────────────────────

    def ask(self, prompt):
        """Plain text input. Returns typed text or None."""
        self._reset()
        self._mode = "input"
        self._set_prompt(prompt)
        return self._run()

    def message(self, text):
        """Display a message. Escape dismisses."""
        self._reset()
        self._mode = "message"
        self._set_prompt(text)
        tk.Label(self._inner, text="Press Esc to dismiss",
                 bg=self.BG, fg=self.FG_DIM,
                 font=("Helvetica", 11), anchor="w", padx=10, pady=20
                 ).pack(fill="x")
        self._entry.pack_forget()
        return self._run()

    def pick(self, prompt, options):
        """Filterable text list. Returns selection or free-typed text."""
        self._reset()
        self._mode    = "list"
        self._options = list(options)
        self._set_prompt(prompt)
        self._build_text_rows(self._options)
        if self._rows:
            self._select(0)
        self._trace_id = self._entry_var.trace_add("write", self._filter_cb)
        return self._run()

    def _filter_cb(self, *_):
        q = self._entry_var.get().lower()
        filtered = [o for o in self._options if q in o.lower()] if q else self._options
        prev = self._sel
        self._build_text_rows(filtered)
        if self._rows:
            self._select(min(prev, len(self._rows) - 1) if prev >= 0 else 0)

    @staticmethod
    def _is_emoji_char(ch):
        cp = ord(ch)
        return (0x2300 <= cp <= 0x27BF or   # Misc Technical, Symbols, Dingbats
                0x2B00 <= cp <= 0x2BFF or   # Misc Symbols and Arrows
                0x1F000 <= cp <= 0x1FFFF)   # Main emoji block

    def _pack_rich_label(self, parent, text, bg, font=("Helvetica", 12), pady=5):
        """
        Pack a series of Label widgets into parent for text that may contain
        emoji characters. Emoji are rendered via PIL; plain text uses font.
        Returns a list of all created widgets (for click-binding).
        """
        # Segment the string into alternating text/emoji runs
        segments, buf, in_emoji = [], "", False
        for ch in text:
            is_e = self._is_emoji_char(ch)
            if is_e != in_emoji:
                if buf:
                    segments.append(("e" if in_emoji else "t", buf))
                buf, in_emoji = ch, is_e
            else:
                buf += ch
        if buf:
            segments.append(("e" if in_emoji else "t", buf))

        em_size = font[1] + 4
        widgets = []
        for idx, (kind, content) in enumerate(segments):
            is_last = (idx == len(segments) - 1)
            if kind == "e":
                for ch in content:
                    pil_img = render_emoji_pil(ch, size=em_size)
                    if pil_img and HAS_PIL:
                        from PIL import ImageTk
                        photo = ImageTk.PhotoImage(pil_img)
                        self._img_refs.append(photo)
                        w = tk.Label(parent, image=photo, bg=bg,
                                     width=em_size + 4, height=em_size + 4)
                        w.pack(side="left", pady=pady)
                    else:
                        w = tk.Label(parent, text=ch, bg=bg, fg=self.FG, font=font)
                        w.pack(side="left", pady=pady)
                    widgets.append(w)
            else:
                w = tk.Label(parent, text=content, bg=bg, fg=self.FG,
                             font=font, anchor="w")
                kw = dict(side="left", pady=pady)
                if is_last:
                    kw.update(fill="x", expand=True)
                w.pack(**kw)
                widgets.append(w)
        return widgets

    def _build_text_rows(self, opts):
        for w in self._inner.winfo_children():
            w.destroy()
        self._rows = []
        self._sel  = -1
        for i, label in enumerate(opts):
            rbg  = self.RAINBOW_ROW[i % len(self.RAINBOW_ROW)]
            rsel = self.RAINBOW_SEL[i % len(self.RAINBOW_SEL)]
            row = tk.Frame(self._inner, bg=rbg, cursor="hand2")
            row.pack(fill="x", padx=2, pady=1)
            inner_widgets = self._pack_rich_label(row, label, rbg,
                                                  font=("Helvetica", 12), pady=5)
            self._rows.append({"frame": row, "label": label,
                                "row_bg": rbg, "sel_bg": rsel})
            for w in [row] + inner_widgets:
                w.bind("<Button-1>",   lambda e, i=i: self._click_row(i))
                w.bind("<MouseWheel>", self._on_scroll)
                w.bind("<Button-4>",   self._on_scroll)
                w.bind("<Button-5>",   self._on_scroll)

    def pick_with_images(self, prompt, entries, on_url):
        """
        Show results progressively: list starts empty; each row appears as its
        thumbnail finishes downloading.

        entries : list of (label, url_or_None)
        on_url  : callable(url) -> local_path_or_None
        """
        self._reset()
        self._mode = "imagelist"
        self._set_prompt(prompt)

        entries   = list(entries)
        next_rank = [0]
        pending   = {}

        def _append_row(label, photo):
            i = len(self._rows)
            rbg  = self.RAINBOW_ROW[i % len(self.RAINBOW_ROW)]
            rsel = self.RAINBOW_SEL[i % len(self.RAINBOW_SEL)]
            row = tk.Frame(self._inner, bg=rbg, cursor="hand2")
            row.pack(fill="x", padx=2, pady=1)
            img_lbl = tk.Label(row, image=photo, bg=rbg,
                               width=self.THUMB, height=self.THUMB)
            img_lbl.pack(side="left", padx=(6, 10), pady=4)
            self._img_refs.append(photo)
            txt_widgets = self._pack_rich_label(row, label, rbg,
                                                font=("Helvetica", 11), pady=6)
            self._rows.append({"frame": row, "label": label,
                                "row_bg": rbg, "sel_bg": rsel})
            for w in [row, img_lbl] + txt_widgets:
                w.bind("<Button-1>",   lambda e, i=i: self._click_row(i))
                w.bind("<MouseWheel>", self._on_scroll)
                w.bind("<Button-4>",   self._on_scroll)
                w.bind("<Button-5>",   self._on_scroll)
            if len(self._rows) == 1:
                self._select(0)

        def _on_image_ready(rank, label, path):
            photo = None
            if path and HAS_PIL:
                try:
                    img   = Image.open(path).convert("RGBA")
                    img   = img.resize((self.THUMB, self.THUMB), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                except Exception:
                    pass
            if photo is None:
                # skip broken images — don't hold up the rank queue
                pending[rank] = None
            else:
                pending[rank] = (label, photo)

            # flush contiguous completed ranks in order
            while next_rank[0] in pending:
                item = pending.pop(next_rank[0])
                next_rank[0] += 1
                if item is not None:
                    _append_row(*item)

        for rank, (label, url) in enumerate(entries):
            if url is None:
                # LOAD_MORE or similar text-only entry — invisible 1×1 image
                if HAS_PIL:
                    photo = ImageTk.PhotoImage(
                        Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
                else:
                    photo = tk.PhotoImage(width=1, height=1)
                self._img_refs.append(photo)
                pending[rank] = (label, photo)
                while next_rank[0] in pending:
                    item = pending.pop(next_rank[0])
                    next_rank[0] += 1
                    if item is not None:
                        _append_row(*item)
                continue

            def _worker(rank=rank, label=label, url=url):
                path = on_url(url)
                self.root.after(0, lambda: _on_image_ready(rank, label, path))

            threading.Thread(target=_worker, daemon=True).start()

        return self._run()

    def show_download_progress(self, title, download_fn):
        """
        Run download_fn(progress_cb) in a thread and show a determinate
        progress bar. progress_cb(downloaded_bytes, total_bytes).
        Returns None on success, or an error string.
        """
        self._reset()
        self._mode = "download"
        self._set_prompt(title)
        self._entry.pack_forget()

        self._prog_var.set(0)
        self._progbar.configure(maximum=100, mode="determinate")
        self._prog_lbl_var.set("Starting…")
        self._prog_frame.pack(fill="x")

        result = [None]

        def _progress_cb(downloaded, total):
            def _update():
                if total:
                    pct = downloaded / total * 100
                    self._prog_var.set(pct)
                    self._prog_lbl_var.set(
                        f"{downloaded/1e6:.1f} MB / {total/1e6:.1f} MB")
                else:
                    self._prog_lbl_var.set(f"{downloaded/1e6:.1f} MB")
            self.root.after(0, _update)

        def _worker():
            err = download_fn(_progress_cb)
            result[0] = err
            def _finish():
                if err:
                    self._set_prompt(f"Download failed: {err[:80]}")
                else:
                    self._prog_var.set(100)
                    self._prog_lbl_var.set("Complete")
                self.root.after(600, self.root.quit)
            self.root.after(0, _finish)

        threading.Thread(target=_worker, daemon=True).start()
        self.root.mainloop()
        return result[0]

    def run_blocking(self, title, fn):
        """
        Run fn() in a background thread, show an indeterminate spinner.
        Returns fn's return value, or raises its exception.
        """
        self._reset()
        self._mode = "spinner"
        self._set_prompt(title)
        self._entry.pack_forget()

        self._progbar.configure(mode="indeterminate")
        self._prog_frame.pack(fill="x")
        self._progbar.start(12)

        result = [None]
        error  = [None]

        def _worker():
            try:
                result[0] = fn()
            except Exception as exc:
                error[0] = exc
            self.root.after(0, self.root.quit)

        threading.Thread(target=_worker, daemon=True).start()
        self.root.mainloop()
        self._progbar.stop()

        if error[0]:
            raise error[0]
        return result[0]

    def show_image(self, prompt, image_path):
        """
        Display an image full-size in the content area.
        Enter → returns 'copy'.  Esc → returns None.
        """
        self._reset()
        self._mode = "showimage"
        self._set_prompt(prompt)
        self._entry.pack_forget()

        if HAS_PIL:
            self.root.update_idletasks()
            avail_w = max(self.root.winfo_width() - 20, 100)
            avail_h = max(int(self.root.winfo_height() * 0.80), 100)
            img = Image.open(image_path)
            img.thumbnail((avail_w, avail_h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._img_refs.append(photo)
            tk.Label(self._inner, image=photo, bg=self.BG).pack(pady=(10, 6))
        else:
            tk.Label(self._inner, text=f"[image: {image_path}]",
                     bg=self.BG, fg=self.FG,
                     font=("Helvetica", 11)).pack(pady=20)

        tk.Label(self._inner,
                 text="Enter to copy  ·  Esc to cancel",
                 bg=self.BG, fg=self.FG_DIM,
                 font=("Helvetica", 10), anchor="center"
                 ).pack()

        return self._run()

    def destroy(self):
        try: self.root.destroy()
        except Exception: pass


# ── helpers that need picker ───────────────────────────────────────────────────

def pick_base_emoji(base_index, prompt, picker):
    labels = [f"{emoji} {name}" for _, (emoji, name) in base_index]
    selected = picker.pick(prompt, labels)
    if not selected:
        return None
    for _hex, (emoji, name) in base_index:
        if selected == f"{emoji} {name}":
            return (emoji, name)
    return ("", selected)


def _has_semantic_models():
    return (
        (DATA_DIR / "base-emoji-sem.npy").exists() and
        ((DATA_DIR / "embeddings.npy").exists() or
         (DATA_DIR / "embeddings-pca340.npy").exists())
    )


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if not SEARCH_INDEX.exists():
        _notify("No search index - run emoji-wallpaper.py first.")
        sys.exit(1)

    entries = load_index()
    picker  = TkPicker()

    try:
        while True:
            has_sem     = _has_semantic_models()
            sem_suffix  = ("" if has_sem else "  [models not downloaded]")
            sem_label   = "semantic search (better, slow)" + sem_suffix
            story_label = "emoji story"
            modes = ["keyword search", "combo", sem_label, story_label]

            mode = picker.pick("emoji:", modes)
            if not mode:
                sys.exit(0)

            # ── story ────────────────────────────────────────────────────
            if mode == story_label:
                text = picker.ask("story text:")
                if not text:
                    continue
                try:
                    picker.run_blocking(
                        "Generating emoji story…",
                        lambda: subprocess.run(
                            [_PYTHON, str(STORY_PY), "--output", str(STORY_OUT), text],
                            check=True))
                except Exception:
                    picker.message("Story generation failed.")
                    continue
                action = picker.show_image(
                    "Emoji story  (Enter to copy  ·  Esc to cancel)",
                    str(STORY_OUT))
                if action == "copy":
                    copy_image_to_clipboard(str(STORY_OUT))
                    _notify("Story copied to clipboard")
                break

            # ── combo ────────────────────────────────────────────────────
            if mode == "combo":
                base_index = build_base_emoji_index(entries)
                first = pick_base_emoji(base_index, "first emoji:", picker)
                if not first:
                    continue
                emoji1, term1 = first

                second = pick_base_emoji(
                    base_index,
                    f"second emoji (+ {emoji1}{' ' + term1 if emoji1 else term1}):",
                    picker)
                if not second:
                    continue
                emoji2, term2 = second

                results = search(entries, f"{term1} {term2}")
                if not results:
                    picker.message(f"No results for '{term1} {term2}'")
                    continue

                exact_alts = {f"{term1}-{term2}".lower(), f"{term2}-{term1}".lower()}
                exact   = [r for r in results if r[1].lower() in exact_alts]
                rest    = [r for r in results if r[1].lower() not in exact_alts]
                results = exact + rest
                patterns    = [re.compile(re.escape(term1)), re.compile(re.escape(term2))]
                query_label = f"'{term1}+{term2}'"

            # ── semantic ─────────────────────────────────────────────────
            elif mode == sem_label:
                if not has_sem:
                    err = picker.show_download_progress(
                        "Downloading semantic models (~150 MB)…",
                        download_data_with_progress)
                    if err:
                        picker.message(f"Download failed:\n{err}")
                        continue
                    # re-check now that download finished
                    if not _has_semantic_models():
                        picker.message("Download finished but models not found.")
                        continue
                query = picker.ask("emoji search (semantic):")
                if not query:
                    continue
                results = query_daemon(query)
                if results == "loading":
                    picker.message(
                        "Search daemon is still loading models.\n"
                        "Try the search again in a moment.")
                    continue
                if not results:
                    picker.message(
                        f"Search daemon failed to start.\nSee {DAEMON_LOG} for details.")
                    continue
                patterns    = []
                query_label = f"'{query}' (semantic)"

            # ── keyword ──────────────────────────────────────────────────
            else:
                if mode == "keyword search":
                    query = picker.ask("emoji search:")
                    if not query:
                        continue
                else:
                    query = mode
                results = search(entries, query)
                if not results:
                    picker.message(f"No results for '{query}'")
                    continue
                patterns    = [re.compile(r'\b' + re.escape(w) + r'\b')
                                for w in query.lower().split()]
                query_label = f"'{query}'"

            # ── results ──────────────────────────────────────────────────
            selected = None
            offset   = 0
            while True:
                batch = results[offset:offset + BATCH_SIZE]
                count = f"({offset+1}–{offset+len(batch)} of {len(results)})"
                icon_entries = [(format_label(alt, url, text), url)
                                for _, alt, url, text in batch]
                if offset + BATCH_SIZE < len(results):
                    icon_entries.append((LOAD_MORE, None))

                selected = picker.pick_with_images(
                    f"{query_label} {count}:", icon_entries, get_thumb)

                if not selected:
                    break
                if selected == LOAD_MORE:
                    offset += BATCH_SIZE
                    continue
                break

            if not selected or selected == LOAD_MORE:
                continue

            m = re.match(r'^\S+', selected)
            selected_alt = m.group(0) if m else selected

            for _, alt, url, _ in results:
                if alt == selected_alt:
                    thumb = get_thumb(url)
                    if thumb:
                        copy_image_to_clipboard(thumb)
                        _notify("Copied to clipboard")
                    break
            _trim_thumb_cache()
            break

    finally:
        picker.destroy()


if __name__ == "__main__":
    main()
