#!/usr/bin/env python3
"""
Emoji kitchen picker - tkinter UI replacing rofi.
Results appear one-by-one as thumbnails download. Borderless, half-screen, centered.

Bind in i3 config:
  bindsym $mod+shift+e exec --no-startup-id python3 ~/.local/bin/emoji-picker-tk.py
"""

import sys, os, re, json, hashlib, shutil, socket, subprocess, webbrowser
import time, urllib.request, threading, random as _random
from pathlib import Path
import tkinter as tk
from tkinter import ttk
import customtkinter as ctk

try:
    from PIL import Image, ImageTk, ImageDraw, ImageFont as _ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

_REPO        = Path(__file__).resolve().parent
if not (_REPO / "data").exists():
    _REPO = Path(sys.executable).resolve().parent
DATA_DIR     = _REPO / "data" / "embeddings"
CACHE_DIR    = _REPO / "data" / "cache"
_VENV_PY     = _REPO / ".venv" / "bin" / "python3"
_PYTHON      = str(_VENV_PY) if _VENV_PY.exists() else sys.executable
SEARCH_INDEX = DATA_DIR / "search-index.tsv"
THUMB_DIR    = CACHE_DIR / "thumbs"
WALLPAPER_PATH   = CACHE_DIR / "wallpaper.png"
SOCK_PATH     = CACHE_DIR / "split-daemon.sock"
DAEMON_STATUS = CACHE_DIR / "split-daemon-loading.json"
DAEMON_PY    = _REPO / "emoji-split-daemon.py"
DAEMON_BIN   = _REPO / "emoji-split-daemon"
DAEMON_PID   = CACHE_DIR / "split-daemon.pid"
DAEMON_LOG   = CACHE_DIR / "split-daemon.log"
DATA_TARBALL_URL = "https://github.com/morganrivers/kitchensearch/releases/latest/download/data.tar.gz"

TILE_SIZE   = 200
MAX_RESULTS = 5000
SHOW_BROKEN_THUMBS = False
BATCH_SIZE     = 100
LOAD_MORE      = "⬇  load more results..."
HEADER_MARKER  = "__HEADER__"
STORY_PY    = _REPO / "emoji-story.py"
STORY_BIN   = _REPO / "emoji-story"
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


_xlib_clipboard_state = None

def _copy_image_xlib(png_data):
    """Own the X11 CLIPBOARD selection and serve PNG bytes to any requestor."""
    global _xlib_clipboard_state
    import select as _select
    from Xlib import display as _Xdisplay, X as _X, Xatom as _Xatom
    from Xlib.protocol import event as _Xevent

    disp   = _Xdisplay.Display()
    screen = disp.screen()
    win    = screen.root.create_window(0, 0, 1, 1, 0, screen.root_depth)

    CLIPBOARD = disp.intern_atom("CLIPBOARD")
    TARGETS   = disp.intern_atom("TARGETS")
    PNG       = disp.intern_atom("image/png")

    win.set_selection_owner(CLIPBOARD, _X.CurrentTime)
    disp.flush()
    if disp.get_selection_owner(CLIPBOARD) != win:
        disp.close()
        raise RuntimeError("could not acquire CLIPBOARD ownership")

    state = {"data": png_data, "running": True}
    if _xlib_clipboard_state:
        _xlib_clipboard_state["running"] = False
    _xlib_clipboard_state = state

    def _loop():
        while state["running"]:
            r, _, _ = _select.select([disp.fileno()], [], [], 0.5)
            if not r:
                continue
            while disp.pending_events():
                ev = disp.next_event()
                if ev.type == _X.SelectionRequest:
                    prop = ev.property if ev.property != _X.NONE else ev.target
                    if ev.target == TARGETS:
                        ev.requestor.change_property(prop, _Xatom.ATOM, 32,
                                                     [TARGETS, PNG])
                    elif ev.target == PNG:
                        ev.requestor.change_property(prop, PNG, 8, state["data"])
                    else:
                        prop = _X.NONE
                    notify = _Xevent.SelectionNotify(
                        time=ev.time, requestor=ev.requestor,
                        selection=ev.selection, target=ev.target, property=prop)
                    ev.requestor.send_event(notify)
                    disp.flush()
                elif ev.type == _X.SelectionClear:
                    state["running"] = False
        disp.close()

    threading.Thread(target=_loop, daemon=True).start()


def copy_image_to_clipboard(path):
    png_data = Path(path).read_bytes()

    # macOS
    if sys.platform == "darwin":
        r = subprocess.run(
            ["osascript", "-e",
             f'set the clipboard to (read (POSIX file "{path}") as «class PNGf»)'],
            capture_output=True)
        if r.returncode == 0:
            return
        _notify("Clipboard failed (osascript error)")
        return

    # Windows
    if sys.platform == "win32":
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            f"$img = [System.Drawing.Image]::FromFile('{path}');"
            "[System.Windows.Forms.Clipboard]::SetImage($img)"
        )
        subprocess.run(["powershell", "-Command", ps],
                       capture_output=True, check=True)
        return

    # Linux: try python-xlib (pure Python, no external tools needed)
    if os.environ.get("DISPLAY"):
        try:
            _copy_image_xlib(png_data)
            return
        except Exception:
            pass

    # Fallback to external tools
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
    cmd = [str(DAEMON_BIN)] if DAEMON_BIN.exists() else [_PYTHON, str(DAEMON_PY)]
    proc = subprocess.Popen(cmd,
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
_THUMB_DL_SEM = threading.Semaphore(8)  # cap concurrent thumbnail downloads to avoid rate-limiting


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
    with _THUMB_DL_SEM:
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "emojikitchen-picker"})
                with urllib.request.urlopen(req, timeout=10) as resp, open(tmp, "wb") as f:
                    shutil.copyfileobj(resp, f)
                if tmp.stat().st_size > 0:
                    tmp.replace(path)
                    return str(path)
            except Exception as e:
                print(f"[thumb fail] attempt={attempt} url={url[:60]} err={e}", flush=True)
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
    THUMB    = 96

    ROW_COLORS    = ["#f5f5f5", "#ffffff"]
    SEL_BG        = "#dde0ff"
    RAINBOW_VIVID = ["#FF0000", "#FF8C00", "#FFD700", "#32CD32", "#1E90FF", "#8B00FF"]
    TITLE_H       = 52

    def __init__(self, floating=False, frameless=True):
        root = tk.Tk()
        root.configure(bg=self.BG)
        root.title("Kitchen Search")
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        side = min(sw, sh) // 2
        root.geometry(f"{side}x{side}+{(sw - side)//2}+{(sh - side)//2}")
        self._floating  = self._setup_floating(root, floating=floating, frameless=frameless)
        self.root       = root
        self._result    = None
        self._mode      = "input"
        self._rows      = []
        self._sel       = -1
        self._img_refs  = []
        self._options   = []
        self._trace_id  = None
        self._on_select = None
        self._build()

    def _build(self):
        root = self.root

        # ── rainbow border + content frame ────────────────────────────────
        self._make_rainbow_border(root)
        cf = self._content_frame

        # ── rainbow title bar ─────────────────────────────────────────────
        self._title_canvas = tk.Canvas(cf, height=self.TITLE_H,
                                       highlightthickness=0, bd=0, bg=self.BG)
        self._title_canvas.pack(fill="x")
        self._title_canvas.bind("<Configure>",
            lambda e: self._draw_title(e.width, e.height))

        # ── top bar ───────────────────────────────────────────────────────
        top = tk.Frame(cf, bg=self.BG)
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

        self._entry.bind("<Escape>", self._cancel)
        self._entry.bind("<Return>", self._on_return)
        self._entry.bind("<space>",  self._on_space_key)
        self._entry.bind("<Up>",     lambda e: (self._up(),   "break")[1])
        self._entry.bind("<Down>",   lambda e: (self._down(), "break")[1])

        # ── progress bar (hidden until explicitly shown) ──────────────────
        self._prog_frame = tk.Frame(cf, bg=self.BG)
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
        list_outer = tk.Frame(cf, bg=self.BG)
        list_outer.pack(fill="both", expand=True, padx=2)

        self._canvas = tk.Canvas(list_outer, bg=self.BG, highlightthickness=0, bd=0)
        self._sb = ctk.CTkScrollbar(list_outer, orientation="vertical",
                                    command=self._canvas.yview,
                                    fg_color="#e0e0e0",
                                    button_color="#888888",
                                    button_hover_color="#555555",
                                    corner_radius=6,
                                    width=14)
        self._canvas.configure(yscrollcommand=self._on_yscroll)
        self._canvas.pack(side="left", fill="both", expand=True)

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

    def _make_rainbow_border(self, root):
        colors = self.RAINBOW_VIVID

        tb = tk.Frame(root, height=2, bd=0, highlightthickness=0)
        tb.pack(fill="x", side="top")
        tb.pack_propagate(False)
        for c in colors:
            tk.Frame(tb, bg=c, bd=0, highlightthickness=0).pack(
                side="left", fill="both", expand=True)

        bb = tk.Frame(root, height=2, bd=0, highlightthickness=0)
        bb.pack(fill="x", side="bottom")
        bb.pack_propagate(False)
        for c in colors:
            tk.Frame(bb, bg=c, bd=0, highlightthickness=0).pack(
                side="left", fill="both", expand=True)

        mid = tk.Frame(root, bd=0, highlightthickness=0, bg=self.BG)
        mid.pack(fill="both", expand=True)

        lb = tk.Frame(mid, width=2, bd=0, highlightthickness=0)
        lb.pack(side="left", fill="y")
        lb.pack_propagate(False)
        for c in colors:
            tk.Frame(lb, bg=c, bd=0, highlightthickness=0).pack(
                side="top", fill="both", expand=True)

        rb = tk.Frame(mid, width=2, bd=0, highlightthickness=0)
        rb.pack(side="right", fill="y")
        rb.pack_propagate(False)
        for c in colors:
            tk.Frame(rb, bg=c, bd=0, highlightthickness=0).pack(
                side="top", fill="both", expand=True)

        self._content_frame = tk.Frame(mid, bg=self.BG, bd=0, highlightthickness=0)
        self._content_frame.pack(fill="both", expand=True)

    def _draw_title(self, W, H):
        c = self._title_canvas
        c.delete("all")
        if W <= 1 or H <= 1:
            return
        colors = self.RAINBOW_VIVID
        n = len(colors)
        stripe_w = max(20, (W + H) // (n * 3))
        i = 0
        x = -H
        while x < W:
            col = colors[i % n]
            x0, x1 = x, x + stripe_w
            c.create_polygon(x0, 0, x1, 0, x1 + H, H, x0 + H, H,
                             fill=col, outline="")
            x += stripe_w
            i += 1
        if HAS_PIL:
            font_path = _REPO / "fonts" / "BubblegumSans-Regular.ttf"
            try:
                pil_font = _ImageFont.truetype(str(font_path), 30)
                img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                draw.text((W // 2, H // 2), "Kitchen Search",
                          font=pil_font, fill=(255, 255, 255, 255),
                          stroke_width=3, stroke_fill=(148, 0, 211, 255),
                          anchor="mm")
                photo = ImageTk.PhotoImage(img)
                c.create_image(0, 0, image=photo, anchor="nw")
                c._title_photo = photo
                return
            except Exception:
                pass
        c.create_text(W // 2 + 1, H // 2 + 1,
                      text="Kitchen Search",
                      font=("Helvetica", 18, "bold"),
                      fill="#000000", anchor="center")
        c.create_text(W // 2, H // 2,
                      text="Kitchen Search",
                      font=("Helvetica", 18, "bold"),
                      fill="#ffffff", anchor="center")

    @staticmethod
    def _setup_floating(root, floating=False, frameless=True):
        if frameless:
            # Bypasses WM entirely — no title bar on any WM including i3.
            # focus_force + grab_set_global in _run() handle keyboard focus.
            root.overrideredirect(True)
            root.attributes("-topmost", True)
        elif floating and sys.platform.startswith("linux"):
            # WM hint: ask WM to float the window (i3 obeys this).
            # Title bar visibility depends on the WM.
            try:
                root.wm_attributes("-type", "splash")
            except tk.TclError:
                pass
        return frameless or floating

    def _run(self):
        # Defer focus/grab so the window is fully mapped before we grab input.
        # With -type splash the WM handles focus; with overrideredirect we need
        # focus_force + grab_set to capture keyboard events.
        def _activate():
            self.root.focus_force()
            self._entry.focus_force()
            try:
                self.root.grab_set_global()
            except tk.TclError:
                try:
                    self.root.grab_set()
                except tk.TclError:
                    pass
        self.root.after(50, _activate)
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

    def _on_space_key(self, e=None):
        if not self._entry_var.get() and self._mode in ("list", "imagelist"):
            self._on_return()
            return "break"

    # ── scrolling ─────────────────────────────────────────────────────────────

    def _on_yscroll(self, first, last):
        if float(first) <= 0.0 and float(last) >= 1.0:
            self._sb.pack_forget()
        else:
            self._sb.pack(side="right", fill="y")
        self._sb.set(first, last)

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
        elif self._mode == "list":
            if self._sel >= 0 and self._rows:
                self._result = self._rows[self._sel]["label"]
            else:
                val = self._entry_var.get().strip()
                self._result = val or None
            self.root.quit()
        elif self._mode == "imagelist":
            val = self._entry_var.get().strip()
            if val:
                self._result = val
            elif self._sel >= 0 and self._rows:
                self._result = self._rows[self._sel]["label"]
            else:
                self._result = None
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
        bg = self.SEL_BG if selected else rd["row_bg"]
        for w in rd["all_widgets"]:
            try:
                w.configure(bg=bg)
            except tk.TclError:
                pass

    def _click_row(self, idx):
        self._select(idx, scroll=False)
        label = self._rows[idx]["label"]
        if self._mode == "imagelist" and self._on_select is not None and label != LOAD_MORE:
            self._on_select(label)
        else:
            self._result = label
            self.root.quit()

    # ── state management ──────────────────────────────────────────────────────

    def _reset(self):
        if self._trace_id:
            try: self._entry_var.trace_remove("write", self._trace_id)
            except Exception: pass
            self._trace_id = None
        for w in self._inner.winfo_children():
            w.destroy()
        self._canvas.yview_moveto(0)
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
                 font=("Helvetica", 11, "bold"), anchor="w", padx=10, pady=20
                 ).pack(fill="x")
        self._entry.pack_forget()
        return self._run()

    def pick(self, prompt, options, filter=True, initial_sel=0):
        self._reset()
        self._mode    = "list"
        self._options = list(options)
        self._set_prompt(prompt)
        self._build_text_rows(self._options)
        if self._rows:
            self._select(min(initial_sel, len(self._rows) - 1))
        if filter:
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
            rbg = self.ROW_COLORS[i % len(self.ROW_COLORS)]
            row = tk.Frame(self._inner, bg=rbg, cursor="hand2")
            row.pack(fill="x", padx=2, pady=1)
            inner_widgets = self._pack_rich_label(row, label, rbg,
                                                  font=("Helvetica", 12, "bold"), pady=5)
            all_widgets = [row] + inner_widgets
            self._rows.append({"frame": row, "label": label,
                                "row_bg": rbg, "all_widgets": all_widgets})
            for w in all_widgets:
                w.bind("<Button-1>",   lambda e, i=i: self._click_row(i))
                w.bind("<MouseWheel>", self._on_scroll)
                w.bind("<Button-4>",   self._on_scroll)
                w.bind("<Button-5>",   self._on_scroll)

    def pick_with_images(self, prompt, entries, on_url, on_select=None, thumb_size=None, patterns=None):
        thumb = thumb_size if thumb_size is not None else self.THUMB
        self._reset()
        self._mode = "imagelist"
        self._on_select = on_select
        self._set_prompt(prompt)

        entries   = list(entries)
        next_rank = [0]
        pending   = {}

        def _append_header_row(text, color, image_path=None):
            hr = tk.Frame(self._inner, bg=self.BG, bd=0, highlightthickness=0)
            hr.pack(fill="x", padx=6, pady=(6, 2))
            if image_path and HAS_PIL:
                try:
                    img_size = 30
                    img = Image.open(image_path).convert("RGBA")
                    img = img.resize((img_size, img_size), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    self._img_refs.append(photo)
                    tk.Label(hr, image=photo, bg=self.BG).pack(side="left", padx=(0, 8))
                except Exception:
                    pass
            lbl = tk.Label(hr, text=text, bg=self.BG, fg=color or self.FG_DIM,
                           font=("Helvetica", 11, "bold"), anchor="w")
            lbl.pack(side="left", fill="x", expand=True)

        def _append_row(label, photo, score=None):
            i = len(self._rows)
            rbg = self.ROW_COLORS[i % len(self.ROW_COLORS)]
            row = tk.Frame(self._inner, bg=rbg, cursor="hand2")
            row.pack(fill="x", padx=2, pady=1)
            img_lbl = tk.Label(row, image=photo, bg=rbg, width=thumb, height=thumb)
            img_lbl.pack(side="left", padx=(6, 10), pady=4)
            self._img_refs.append(photo)

            # Parse "alt  🔮🫐  (keywords...)" into name part and keywords part
            sep_idx = label.find("  (")
            if sep_idx >= 0:
                name_part = label[:sep_idx]
                kw_part   = label[sep_idx + 2:]
            else:
                name_part = label
                kw_part   = ""

            font_main    = ("Helvetica", 11, "bold")
            font_kw      = ("Helvetica", 10)
            font_kw_bold = ("Helvetica", 10, "bold")
            em_size      = 15

            txt = tk.Text(row, height=2, wrap="word",
                          bg=rbg, fg=self.FG,
                          font=font_main,
                          relief="flat", bd=0,
                          highlightthickness=0,
                          spacing1=1, spacing3=1,
                          cursor="hand2")
            txt.tag_configure("alt_bold",  font=font_main,    foreground=self.FG)
            txt.tag_configure("kw_normal", font=font_kw,      foreground=self.FG_DIM)
            txt.tag_configure("kw_bold",   font=font_kw_bold, foreground=self.FG)
            txt.bind("<Key>",     lambda e: "break")
            txt.bind("<Button-2>", lambda e: "break")
            txt.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=0)

            def _auto_height(e, t=txt):
                dlines = t.count("1.0", "end", "displaylines")
                if dlines:
                    new_h = max(1, dlines[0])
                    if int(t.cget("height")) != new_h:
                        t.configure(height=new_h)
            txt.bind("<Configure>", _auto_height)

            # Insert alt name + inline emojis as images
            segs, buf, in_em = [], "", False
            for ch in name_part:
                is_e = self._is_emoji_char(ch)
                if is_e != in_em:
                    if buf:
                        segs.append(("e" if in_em else "t", buf))
                    buf, in_em = ch, is_e
                else:
                    buf += ch
            if buf:
                segs.append(("e" if in_em else "t", buf))

            for kind, content in segs:
                if kind == "e":
                    for ch in content:
                        pil_img = render_emoji_pil(ch, size=em_size)
                        if pil_img and HAS_PIL:
                            photo_e = ImageTk.PhotoImage(pil_img)
                            self._img_refs.append(photo_e)
                            txt.image_create("end", image=photo_e, pady=1)
                        else:
                            txt.insert("end", ch, "alt_bold")
                else:
                    txt.insert("end", content, "alt_bold")

            # Keywords: bold the exact pattern matches
            if kw_part:
                txt.insert("end", "  ")
                if patterns:
                    kw_lower = kw_part.lower()
                    spans = []
                    for p in patterns:
                        for m in p.finditer(kw_lower):
                            spans.append((m.start(), m.end()))
                    spans.sort()
                    merged = []
                    for s, e in spans:
                        if merged and s <= merged[-1][1]:
                            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                        else:
                            merged.append([s, e])
                    pos = 0
                    for s, e in merged:
                        if pos < s:
                            txt.insert("end", kw_part[pos:s], "kw_normal")
                        txt.insert("end", kw_part[s:e], "kw_bold")
                        pos = e
                    if pos < len(kw_part):
                        txt.insert("end", kw_part[pos:], "kw_normal")
                else:
                    txt.insert("end", kw_part, "kw_normal")

            all_widgets = [row, img_lbl, txt]
            self._rows.append({"frame": row, "label": label,
                                "row_bg": rbg, "all_widgets": all_widgets})
            for w in all_widgets:
                w.bind("<Button-1>",   lambda e, i=i: self._click_row(i))
                w.bind("<MouseWheel>", self._on_scroll)
                w.bind("<Button-4>",   self._on_scroll)
                w.bind("<Button-5>",   self._on_scroll)
            if len(self._rows) == 1:
                self._select(0)

        def _flush():
            while next_rank[0] in pending:
                item = pending.pop(next_rank[0])
                next_rank[0] += 1
                if item is None:
                    pass
                elif isinstance(item, tuple) and item[0] == "__HEADER__":
                    _append_header_row(item[1], item[2], item[3] if len(item) > 3 else None)
                else:
                    _append_row(*item)

        def _on_image_ready(rank, label, path, score):
            photo = None
            if path and HAS_PIL:
                try:
                    img   = Image.open(path).convert("RGBA")
                    img   = img.resize((thumb, thumb), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                except Exception:
                    pass
            pending[rank] = None if photo is None else (label, photo, score)
            _flush()

        for rank, entry in enumerate(entries):
            label      = entry[0]
            url        = entry[1] if len(entry) > 1 else None
            score      = entry[2] if len(entry) > 2 else None
            image_path = entry[3] if len(entry) > 3 else None
            if url == HEADER_MARKER:
                pending[rank] = ("__HEADER__", label, score, image_path)
                _flush()
                continue
            if url is None:
                # LOAD_MORE or similar text-only entry — invisible 1×1 image
                if HAS_PIL:
                    photo = ImageTk.PhotoImage(
                        Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
                else:
                    photo = tk.PhotoImage(width=1, height=1)
                self._img_refs.append(photo)
                pending[rank] = (label, photo, None)
                _flush()
                continue

            def _worker(rank=rank, label=label, url=url, score=score):
                path = on_url(url)
                self.root.after(0, lambda: _on_image_ready(rank, label, path, score))

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
        self._prog_lbl_var.set("Starting...")
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

    def show_model_loading_progress(self):
        """Poll the daemon status file and show a progress bar until the daemon is ready."""
        self._reset()
        self._mode = "loading"
        self._set_prompt("Loading search models...")
        self._entry.pack_forget()

        self._prog_var.set(0)
        self._progbar.configure(maximum=100, mode="determinate")
        self._prog_lbl_var.set("")
        self._prog_frame.pack(fill="x")

        desc_var = tk.StringVar(value="Starting...")
        tk.Label(
            self._inner, textvariable=desc_var,
            bg=self.BG, fg=self.FG,
            font=("Helvetica", 11), anchor="w", padx=14, pady=14,
        ).pack(fill="x")

        def _poll():
            if SOCK_PATH.exists():
                self._prog_var.set(100)
                desc_var.set("Ready!")
                self.root.after(350, self.root.quit)
                return
            if not _daemon_alive():
                desc_var.set("Daemon failed to start.")
                self._prog_var.set(0)
                self.root.after(1500, self.root.quit)
                return
            try:
                data = json.loads(DAEMON_STATUS.read_text())
                pct  = float(data.get("pct", 0))
                step = data.get("step", "Starting...")
                self._prog_var.set(pct)
                desc_var.set(step)
            except Exception:
                pass
            self.root.after(150, _poll)

        self.root.after(150, _poll)
        self.root.mainloop()

    def show_story_progress(self, cmd):
        """Run a story command and show phrase-by-phrase progress. Returns None or error string."""
        self._reset()
        self._mode = "story_progress"
        self._set_prompt("Generating emoji story...")
        self._entry.pack_forget()

        self._prog_var.set(0)
        self._progbar.configure(maximum=100, mode="indeterminate")
        self._progbar.start(12)
        self._prog_lbl_var.set("")
        self._prog_frame.pack(fill="x")

        desc_var = tk.StringVar(value="Searching phrases...")
        tk.Label(
            self._inner, textvariable=desc_var,
            bg=self.BG, fg=self.FG,
            font=("Helvetica", 11), anchor="w", padx=14, pady=14,
        ).pack(fill="x")

        total  = [0]
        done   = [0]
        error  = [None]

        def _update():
            if total[0] > 0:
                self._progbar.stop()
                self._progbar.configure(mode="determinate", maximum=total[0])
                self._prog_var.set(done[0])
                desc_var.set(f"Fetched {done[0]} / {total[0]} emojis")

        def _worker():
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in proc.stdout:
                    m = re.match(r'(\d+) phrases', line)
                    if m:
                        total[0] = int(m.group(1))
                        self.root.after(0, _update)
                    elif line.startswith("  "):
                        done[0] += 1
                        self.root.after(0, _update)
                proc.wait()
                if proc.returncode != 0:
                    error[0] = "Story generation failed."
            except Exception as exc:
                error[0] = str(exc)
            self.root.after(0, self.root.quit)

        threading.Thread(target=_worker, daemon=True).start()
        self.root.mainloop()
        self._progbar.stop()
        return error[0]

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
            img = Image.open(image_path)
            if img.width > avail_w:
                img = img.resize(
                    (avail_w, int(img.height * avail_w / img.width)),
                    Image.LANCZOS,
                )
            photo = ImageTk.PhotoImage(img)
            self._img_refs.append(photo)
            tk.Label(self._inner, image=photo, bg=self.BG).pack(pady=(10, 6))
        else:
            tk.Label(self._inner, text=f"[image: {image_path}]",
                     bg=self.BG, fg=self.FG,
                     font=("Helvetica", 11)).pack(pady=20)

        tk.Label(self._inner,
                 text="Enter to copy  |  Esc to cancel",
                 bg=self.BG, fg=self.FG_DIM,
                 font=("Helvetica", 10), anchor="center"
                 ).pack()

        self.root.update_idletasks()
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


# ── settings ──────────────────────────────────────────────────────────────────

SETTINGS_FILE = CACHE_DIR / "picker-settings.json"

_DEFAULT_SETTINGS = {
    "exit_on_select": False,
    "show_keyword":   True,
    "show_combo":     True,
    "show_semantic":  True,
    "show_story":     True,
    "floating":       False,
    "frameless":      True,
}


def load_settings():
    try:
        data = json.loads(SETTINGS_FILE.read_text())
        return {**_DEFAULT_SETTINGS, **data}
    except Exception:
        return dict(_DEFAULT_SETTINGS)


def save_settings(s):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(s, indent=2))
    except Exception:
        pass


def _find_combo_url(entries, name1, name2):
    for url, alt, _ in entries:
        parts = alt.split("-", 1)
        if len(parts) == 2:
            if (parts[0] == name1 and parts[1] == name2) or \
               (parts[0] == name2 and parts[1] == name1):
                return url
    return None


# ── main ──────────────────────────────────────────────────────────────────────

def _run_settings(picker, settings):
    sel_idx = 0
    while True:
        def _lbl(key, text):
            return f"{'[x]' if settings[key] else '[ ]'} {text}"
        items = [
            _lbl("show_keyword",   "Show keyword search on front menu"),
            _lbl("show_combo",     "Show combo search on front menu"),
            _lbl("show_semantic",  "Show semantic search on front menu"),
            _lbl("show_story",     "Show emoji story on front menu"),
            _lbl("exit_on_select", "Exit app when emoji selected"),
            _lbl("floating",       "Start as floating window (takes effect on restart)"),
            _lbl("frameless",      "Start frameless & no title bar (takes effect on restart)"),
            "buy me a coffee ☕",
        ]
        choice = picker.pick("settings:", items, filter=False, initial_sel=sel_idx)
        if not choice:
            return
        try:
            sel_idx = items.index(choice)
        except ValueError:
            sel_idx = 0
        if "Exit app when emoji selected"   in choice: settings["exit_on_select"] = not settings["exit_on_select"]
        elif "keyword search" in choice: settings["show_keyword"]   = not settings["show_keyword"]
        elif "combo"          in choice: settings["show_combo"]     = not settings["show_combo"]
        elif "semantic"       in choice: settings["show_semantic"]  = not settings["show_semantic"]
        elif "emoji story"    in choice: settings["show_story"]     = not settings["show_story"]
        elif "floating window" in choice: settings["floating"]      = not settings["floating"]
        elif "no title bar"   in choice: settings["frameless"]      = not settings["frameless"]
        elif "buy me a coffee" in choice:
            webbrowser.open("https://buymeacoffee.com/morganrivers")
            return
        save_settings(settings)


def main():
    if not SEARCH_INDEX.exists():
        _notify("No search index - run emoji-wallpaper.py first.")
        sys.exit(1)

    entries  = load_index()
    settings = load_settings()
    picker   = TkPicker(floating=settings["floating"], frameless=settings["frameless"])

    try:
        while True:
            has_sem     = _has_semantic_models()
            sem_suffix  = ("" if has_sem else "  [models not downloaded]")
            sem_label   = "semantic search (better, slow)" + sem_suffix
            story_label = "emoji story"

            # build menu entries with combo thumbnail icons
            menu_entries = []
            if settings["show_keyword"]:
                menu_entries.append(("keyword search",
                                     _find_combo_url(entries, "tornado", "mag_right")))
            if settings["show_combo"]:
                menu_entries.append(("combo",
                                     _find_combo_url(entries, "fire", "slot_machine")))
            if settings["show_semantic"]:
                menu_entries.append((sem_label,
                                     _find_combo_url(entries, "sunrise_over_mountains", "mag_right")))
            if settings["show_story"]:
                menu_entries.append((story_label,
                                     _find_combo_url(entries, "llama", "fire")))
            menu_entries.append(("settings",
                                 _find_combo_url(entries, "computer", "face_with_raised_eyebrow")))

            mode = picker.pick_with_images("Use quick keyword search directly or select an option below.", menu_entries, get_thumb,
                                           thumb_size=48)
            if not mode:
                sys.exit(0)

            # ── settings ─────────────────────────────────────────────────
            if mode == "settings":
                _run_settings(picker, settings)
                continue

            # ── typed text → keyword search ───────────────────────────────
            mode_names = {e[0] for e in menu_entries}
            if mode not in mode_names:
                query   = mode
                results = search(entries, query)
                if not results:
                    picker.message(f"No results for '{query}'")
                    continue
                patterns    = [re.compile(r'\b' + re.escape(w) + r'\b')
                                for w in query.lower().split()]
                query_label = f"'{query}'"
                mode = "_results"

            # ── story ────────────────────────────────────────────────────
            elif mode == story_label:
                text = picker.ask("story text:")
                if not text:
                    continue
                _story_cmd = ([str(STORY_BIN)] if STORY_BIN.exists() else [_PYTHON, str(STORY_PY)]) + ["--output", str(STORY_OUT), text]
                err = picker.show_story_progress(_story_cmd)
                if err:
                    picker.message("Story generation failed.")
                    continue
                action = picker.show_image(
                    "Emoji story  (Enter to copy  |  Esc to cancel)",
                    str(STORY_OUT))
                if action == "copy":
                    copy_image_to_clipboard(str(STORY_OUT))
                    _notify("Story copied to clipboard")
                continue

            # ── combo ────────────────────────────────────────────────────
            elif mode == "combo":
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
                patterns    = [re.compile(re.escape(term1)), re.compile(re.escape(term2))]
                query_label = f"'{term1}+{term2}'"

                # Build combo icon_entries with match/no-match header
                _ui_assets = _REPO / "data" / "ui_assets"
                _match_img    = str(_ui_assets / "face_holding_back_tears_turtle.png")
                _no_match_img = str(_ui_assets / "cry_turtle.png")
                all_combo = exact + rest
                if exact:
                    combo_entries = [
                        ("Match found!", HEADER_MARKER, "#228844", _match_img),
                        *[(format_label(alt, url, text), url, ts)
                          for ts, alt, url, text in exact],
                        ("Other similar combos", HEADER_MARKER, "#999999"),
                        *[(format_label(alt, url, text), url, ts)
                          for ts, alt, url, text in rest],
                    ]
                else:
                    combo_entries = [
                        ("Match could not be found", HEADER_MARKER, "#8B0000", _no_match_img),
                        ("Other similar combos", HEADER_MARKER, "#999999"),
                        *[(format_label(alt, url, text), url, ts)
                          for ts, alt, url, text in rest],
                    ]
                count = f"({len(exact)} exact, {len(rest)} similar)"

                def _copy_combo(label, _all=all_combo):
                    m = re.match(r'^\S+', label)
                    sel_alt = m.group(0) if m else label
                    for _, alt, url, _ in _all:
                        if alt == sel_alt:
                            path = get_thumb(url)
                            if path:
                                copy_image_to_clipboard(path)
                                _notify("Copied to clipboard")
                            break

                on_sel_combo = None if settings["exit_on_select"] else _copy_combo
                result = picker.pick_with_images(
                    f"{query_label} {count}:", combo_entries, get_thumb,
                    on_select=on_sel_combo, patterns=patterns)
                if result and settings["exit_on_select"]:
                    _copy_combo(result)
                _trim_thumb_cache()
                if settings["exit_on_select"] and result and result != LOAD_MORE:
                    break
                continue

            # ── semantic ─────────────────────────────────────────────────
            elif mode == sem_label:
                if not has_sem:
                    err = picker.show_download_progress(
                        "Downloading semantic models (~150 MB)...",
                        download_data_with_progress)
                    if err:
                        picker.message(f"Download failed:\n{err}")
                        continue
                    if not _has_semantic_models():
                        picker.message("Download finished but models not found.")
                        continue
                query = picker.ask("emoji search (semantic):")
                if not query:
                    continue
                results = query_daemon(query)
                if results == "loading":
                    picker.show_model_loading_progress()
                    results = query_daemon(query)
                if not results or results == "loading":
                    picker.message(
                        f"Search daemon failed to start.\nSee {DAEMON_LOG} for details.")
                    continue
                alt_to_text = {alt: text for _, alt, text in entries}
                results = [(rank, alt, url, alt_to_text.get(alt, ""))
                           for rank, alt, url, _ in results]
                patterns    = []
                query_label = f"'{query}' (semantic)"

            # ── keyword ──────────────────────────────────────────────────
            else:
                query = picker.ask("emoji search:")
                if not query:
                    continue
                results = search(entries, query)
                if not results:
                    picker.message(f"No results for '{query}'")
                    continue
                patterns    = [re.compile(r'\b' + re.escape(w) + r'\b')
                                for w in query.lower().split()]
                query_label = f"'{query}'"

            # ── results ──────────────────────────────────────────────────
            offset = 0
            while True:
                batch = results[offset:offset + BATCH_SIZE]
                count = f"({offset+1}-{offset+len(batch)} of {len(results)})"
                icon_entries = [(format_label(alt, url, text), url, ts)
                                for ts, alt, url, text in batch]
                if offset + BATCH_SIZE < len(results):
                    icon_entries.append((LOAD_MORE, None))

                def _copy_selected(label, _results=results):
                    m = re.match(r'^\S+', label)
                    sel_alt = m.group(0) if m else label
                    for _, alt, url, _ in _results:
                        if alt == sel_alt:
                            path = get_thumb(url)
                            if path:
                                copy_image_to_clipboard(path)
                                _notify("Copied to clipboard")
                            break

                on_sel = None if settings["exit_on_select"] else _copy_selected
                result = picker.pick_with_images(
                    f"{query_label} {count}:", icon_entries, get_thumb,
                    on_select=on_sel, patterns=patterns)

                if result == LOAD_MORE:
                    offset += BATCH_SIZE
                    continue
                if result and settings["exit_on_select"]:
                    _copy_selected(result)
                break

            _trim_thumb_cache()
            if settings["exit_on_select"] and result and result != LOAD_MORE:
                break

    finally:
        picker.destroy()


if __name__ == "__main__":
    main()
