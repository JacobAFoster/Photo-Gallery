"""
Pinterest-style photo gallery  (Tkinter + Pillow)      pip install Pillow

How it works
------------
* SCAN thread     reads only image *headers* (width/height) and streams them to the
                  UI in small batches, so the grid starts filling immediately.
* LAYOUT          masonry: fixed-width columns, each photo goes into the currently
                  shortest column. Appending is O(1) per photo.
* VIRTUALISING    only tiles near the viewport exist as canvas items. Scroll away and
                  they are deleted; scroll back and they are re-created.
* THUMB workers   4 background threads, fed by a LIFO queue, so whatever you scrolled
                  to *most recently* loads first. Jobs for tiles that already scrolled
                  out of view are skipped instead of piling up.
* SAFETY          - JPEGs are decoded already shrunk (Pillow "draft" mode)
                  - other formats above MAX_DECODE_PIXELS get a placeholder tile
                    instead of being decoded (a huge PNG decode can stall the UI)
                  - thumbnails live in a byte-limited LRU cache
                  - the UI thread does a strictly time-boxed amount of work per tick
"""
import bisect
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
import tkinter.font as tkfont
from collections import OrderedDict
from functools import lru_cache
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageDraw, ImageOps, ImageTk
except ImportError:
    try:
        _r = tk.Tk(); _r.withdraw()
        messagebox.showerror("Missing dependency",
                             "This app requires Pillow.\n\n    pip install Pillow")
        _r.destroy()
    except Exception:
        print("Install Pillow:  pip install Pillow")
    sys.exit(1)

Image.MAX_IMAGE_PIXELS = None            # we apply our own, stricter guard below
LANCZOS = getattr(Image, "Resampling", Image).LANCZOS

# ------------------------------------------------------------------ settings
SUPPORTED         = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
PAD, GAP, RADIUS  = 20, 14, 12            # layout (scaled for HiDPI at runtime)
DEFAULT_TILE_W    = 260
MIN_H, MAX_H      = 0.45, 2.6             # tile height limits, as a multiple of width
VIEW_MARGIN       = 500                   # px of tiles kept alive above/below the view
WORKERS           = 4
POLL_MS           = 30
TICK_BUDGET       = 0.012                 # max seconds of UI work per poll tick
MAX_DECODE_PIXELS = 36_000_000            # non-JPEG images larger than this are skipped
CACHE_BYTES       = 250 * 1024 * 1024

BG_RGB   = (244, 246, 249)
ROTATED  = (5, 6, 7, 8)                   # EXIF orientations that swap width/height
EXIF_FMT = ("JPEG", "MPO", "TIFF")        # formats where reading EXIF is cheap


class T:
    BG, SURFACE, SURFACE_ALT = "#f4f6f9", "#ffffff", "#e9edf3"
    HOVER, BORDER, BORDER_STRONG = "#eef2f8", "#e3e7ed", "#cfd4dd"
    TEXT, TEXT_MUTED, TEXT_FAINT = "#1f2430", "#6b7280", "#9aa1ab"
    ACCENT, ACCENT_HOVER, BAD = "#2f6fed", "#1f5ad6", "#f1dddd"


# ------------------------------------------------------------------- helpers
def pick_font(root, *names, default="TkDefaultFont"):
    fams = set(tkfont.families(root))
    for n in names:
        if n in fams:
            return n
    return tkfont.nametofont(default).actual("family")


def enable_hidpi(root):
    if sys.platform != "win32":
        return 1.0
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
        return root.winfo_fpixels("1i") / 96.0
    except Exception:
        return 1.0


def setup_style(root, font):
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    st.configure("TFrame", background=T.BG)
    st.configure("Accent.TButton", background=T.ACCENT, foreground="white",
                 borderwidth=0, focuscolor=T.ACCENT, padding=(16, 8),
                 font=(font, 10, "bold"))
    st.map("Accent.TButton",
           background=[("pressed", T.ACCENT_HOVER), ("active", T.ACCENT_HOVER)])
    st.configure("Secondary.TButton", background=T.SURFACE, foreground=T.TEXT,
                 borderwidth=1, bordercolor=T.BORDER, focuscolor=T.SURFACE,
                 padding=(12, 6), font=(font, 10))
    st.map("Secondary.TButton",
           background=[("pressed", T.HOVER), ("active", T.SURFACE_ALT)],
           bordercolor=[("active", T.BORDER_STRONG)])
    st.configure("Vertical.TScrollbar", background=T.SURFACE_ALT,
                 troughcolor=T.BG, borderwidth=0, arrowcolor=T.TEXT_MUTED)


def natural_key(path):
    parts = re.split(r"(\d+)", os.path.basename(path).lower())
    return [int(p) if p.isdigit() else p for p in parts]


def exif_orientation(im):
    try:
        return im.getexif().get(0x0112, 1)
    except Exception:
        return 1


def read_size(path):
    """Header-only read. Never calls getexif() on PNG/WebP (that can decode them)."""
    try:
        with Image.open(path) as im:
            w, h = im.size
            if im.format in EXIF_FMT and exif_orientation(im) in ROTATED:
                w, h = h, w
        return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


@lru_cache(maxsize=512)
def rounded_mask(w, h, r):
    ss = 4                                       # supersample for smooth corners
    m = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1),
                                        radius=r * ss, fill=255)
    return m.resize((w, h), LANCZOS)


def make_thumb(path, tw, th, radius):
    """Return an RGB PIL image of exactly (tw, th), cover-cropped, rounded."""
    with Image.open(path) as im:
        rotated = im.format in EXIF_FMT and exif_orientation(im) in ROTATED
        if im.format == "JPEG":                  # decode pre-shrunk: huge speed/RAM win
            try:
                im.draft("RGB", (th, tw) if rotated else (tw, th))
            except Exception:
                pass
        if im.width * im.height > MAX_DECODE_PIXELS:
            raise ValueError("image too large to decode safely")

        im = ImageOps.exif_transpose(im)
        has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
        if im.mode not in ("RGB", "RGBA", "L", "LA"):
            im = im.convert("RGBA" if has_alpha else "RGB")

        f = min(im.width // (tw * 2), im.height // (th * 2))
        if f >= 2:                               # cheap box-shrink before the good resize
            im = im.reduce(f)
        im = ImageOps.fit(im, (tw, th), LANCZOS)

        if im.mode in ("RGBA", "LA"):
            im = im.convert("RGBA")
            flat = Image.new("RGB", im.size, BG_RGB)
            flat.paste(im, mask=im.getchannel("A"))
            im = flat
        elif im.mode != "RGB":
            im = im.convert("RGB")

        out = Image.new("RGB", (tw, th), BG_RGB)
        out.paste(im, (0, 0), rounded_mask(tw, th, radius))
        return out


class Photo:
    __slots__ = ("idx", "path", "w", "h", "x", "y", "th")

    def __init__(self, idx, path, w, h):
        self.idx, self.path, self.w, self.h = idx, path, w, h
        self.x = self.y = self.th = 0


class Column:
    __slots__ = ("idx", "tops", "bots")

    def __init__(self):
        self.idx, self.tops, self.bots = [], [], []


# ----------------------------------------------------------------------- app
class Gallery(tk.Tk):
    def __init__(self):
        super().__init__()
        self.s = enable_hidpi(self)
        self.font = pick_font(self, "Segoe UI Variable", "Segoe UI", "SF Pro Text",
                              "Helvetica Neue", "Inter", "Cantarell", "DejaVu Sans")
        setup_style(self, self.font)
        self.title("Gallery")
        self.configure(bg=T.BG)
        self.geometry(f"{int(1200 * self.s)}x{int(820 * self.s)}")
        self.minsize(int(500 * self.s), int(400 * self.s))

        self.pad, self.gap, self.radius = (int(v * self.s) for v in (PAD, GAP, RADIUS))
        self.tile_w = int(DEFAULT_TILE_W * self.s)

        # data
        self.folder = None
        self.gen = 0                    # bumps on every folder load; stale work is dropped
        self.photos = []
        self.cols, self.heights = [], []
        self.ncols, self.x0 = 0, 0
        self.live = {}                  # idx -> [rect_id, image_id | None]
        self.cache = OrderedDict()      # idx -> (PhotoImage, nbytes)   (LRU)
        self.cache_bytes = 0
        self.bad = set()
        self.requested = set()          # (gen, idx) with a queued/running job
        self.lock = threading.Lock()
        self.selected = None
        self.hovered = None
        self.scan_cancel = threading.Event()
        self.scanning = False
        self.closing = False
        self._view_job = self._zoom_job = self._resize_job = None
        self._last_w = 0
        self._empty = False
        self._status = ""

        # threads
        self.events = queue.Queue()     # worker -> UI
        self.jobs = queue.LifoQueue()   # UI -> workers (newest first)
        for _ in range(WORKERS):
            threading.Thread(target=self._worker, daemon=True).start()

        self._build_ui()
        self._setup_bindings()
        self._show_empty("No folder selected", "Click Open Folder to browse your photos")
        self.after(POLL_MS, self._poll)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        bar = tk.Frame(self, bg=T.SURFACE, height=60)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        left = tk.Frame(bar, bg=T.SURFACE)
        left.pack(side="left", fill="y", padx=20)
        ttk.Button(left, text="📁   Open Folder", style="Accent.TButton",
                   command=self.choose_folder).pack(side="left", pady=12)
        self.folder_lbl = tk.Label(left, text="No folder selected", bg=T.SURFACE,
                                   fg=T.TEXT_MUTED, font=(self.font, 10))
        self.folder_lbl.pack(side="left", padx=(18, 0))

        right = tk.Frame(bar, bg=T.SURFACE)
        right.pack(side="right", fill="y", padx=20)
        ttk.Button(right, text="Refresh", style="Secondary.TButton",
                   command=self.refresh).pack(side="right", pady=12)
        self.cancel_btn = ttk.Button(right, text="Cancel scan",
                                     style="Secondary.TButton", command=self.cancel_scan_now)
        zoom = tk.Frame(right, bg=T.SURFACE)
        zoom.pack(side="right", padx=(0, 16), pady=18)
        tk.Label(zoom, text="Size", bg=T.SURFACE, fg=T.TEXT_MUTED,
                 font=(self.font, 10)).pack(side="left", padx=(0, 8))
        self.zoom_var = tk.DoubleVar(value=DEFAULT_TILE_W)
        ttk.Scale(zoom, from_=160, to=460, length=140, variable=self.zoom_var,
                  command=self._on_zoom).pack(side="left")

        tk.Frame(self, bg=T.BORDER, height=1).pack(fill="x")

        self.status = tk.Label(self, text="Ready", anchor="w", bg=T.SURFACE,
                               fg=T.TEXT_MUTED, font=(self.font, 9), padx=20, pady=6)
        self.status.pack(side="bottom", fill="x")

        wrap = tk.Frame(self, bg=T.BG)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=T.BG, highlightthickness=0, bd=0,
                                yscrollincrement=int(30 * self.s))
        self.vbar = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.config(yscrollcommand=self._on_yscroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")

        # one reusable hover box and one selection box (instead of per-tile items)
        self.hover_id = self.canvas.create_rectangle(
            0, 0, 0, 0, outline=T.ACCENT_HOVER, width=2, state="hidden", tags="overlay")
        self.sel_id = self.canvas.create_rectangle(
            0, 0, 0, 0, outline=T.ACCENT, width=3, state="hidden", tags="overlay")

        self.empty_btn = ttk.Button(self.canvas, text="Choose Folder",
                                    style="Accent.TButton", command=self.choose_folder)

    def _setup_bindings(self):
        c = self.canvas
        c.bind("<Configure>", self._on_configure)
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", lambda e: self._set_hover(None))
        c.bind("<Button-1>", self._on_click)
        c.bind("<Double-Button-1>", self._on_double)
        self.bind_all("<MouseWheel>", self._on_wheel)
        self.bind_all("<Button-4>", lambda e: c.yview_scroll(-3, "units"))
        self.bind_all("<Button-5>", lambda e: c.yview_scroll(3, "units"))
        self.bind("<Prior>", lambda e: c.yview_scroll(-1, "pages"))
        self.bind("<Next>",  lambda e: c.yview_scroll(1, "pages"))
        self.bind("<Up>",    lambda e: c.yview_scroll(-3, "units"))
        self.bind("<Down>",  lambda e: c.yview_scroll(3, "units"))
        self.bind("<Home>",  lambda e: c.yview_moveto(0))
        self.bind("<End>",   lambda e: c.yview_moveto(1))
        self.bind("<Control-o>", lambda e: self.choose_folder())
        self.bind("<Control-r>", lambda e: self.refresh())
        self.bind("<F5>",        lambda e: self.refresh())
        self.bind("<Escape>",    lambda e: self._select(None))

    def _set_status(self, text):
        if text != self._status:
            self._status = text
            self.status.config(text=text)

    # ------------------------------------------------------- empty state
    def _show_empty(self, title, sub):
        c = self.canvas
        c.delete("empty")
        self._empty = True
        c.create_text(0, 0, text="🖼", font=(self.font, 56), fill=T.TEXT_FAINT, tags=("empty", "e_icon"))
        c.create_text(0, 0, text=title, font=(self.font, 18, "bold"), fill=T.TEXT, tags=("empty", "e_title"))
        c.create_text(0, 0, text=sub, font=(self.font, 11), fill=T.TEXT_MUTED, tags=("empty", "e_sub"))
        c.create_window(0, 0, window=self.empty_btn, tags=("empty", "e_btn"))
        self._center_empty()

    def _center_empty(self):
        if not self._empty:
            return
        c = self.canvas
        w, h = max(1, c.winfo_width()), max(1, c.winfo_height())
        cx, cy = w // 2, h // 2
        for tag, dy in (("e_icon", -90), ("e_title", -20), ("e_sub", 14), ("e_btn", 60)):
            c.coords(tag, cx, cy + int(dy * self.s))

    def _hide_empty(self):
        if self._empty:
            self.canvas.delete("empty")
            self._empty = False

    # ----------------------------------------------------------- actions
    def choose_folder(self):
        folder = filedialog.askdirectory(title="Select a folder of photos", parent=self)
        if folder:
            self.load_folder(folder)

    def refresh(self):
        if self.folder:
            self.load_folder(self.folder)

    def load_folder(self, folder):
        self.scan_cancel.set()                       # stop any previous scan
        self.scan_cancel = threading.Event()
        self.gen += 1

        self.folder = folder
        self.folder_lbl.config(text=folder)
        name = os.path.basename(folder.rstrip("/\\")) or folder
        self.title(f"{name} — Gallery")

        self._drop_all_tiles()
        self.photos, self.cols, self.heights = [], [], []
        self._cache_clear()
        self.bad.clear()
        self.selected = self.hovered = None
        for i in (self.hover_id, self.sel_id):
            self.canvas.itemconfigure(i, state="hidden")
        self.canvas.config(scrollregion=(0, 0, 0, 0))
        self.canvas.yview_moveto(0)
        self._hide_empty()

        self._set_scanning(True)
        self._set_status("Scanning folder…")
        threading.Thread(target=self._scan, args=(folder, self.gen, self.scan_cancel),
                         daemon=True).start()

    def cancel_scan_now(self):
        self.scan_cancel.set()
        self._set_scanning(False)
        n = len(self.photos)
        if n:
            self._set_status(f"Cancelled · {n} photos shown")
        else:
            self._set_status("Cancelled")
            self._show_empty("Scan cancelled", "Click Open Folder to try again")

    def _set_scanning(self, on):
        self.scanning = on
        if on:
            self.cancel_btn.pack(side="right", pady=12, padx=(0, 8))
        else:
            self.cancel_btn.pack_forget()

    # --------------------------------------------------------- scan thread
    def _scan(self, folder, gen, cancel):
        try:
            paths = []
            with os.scandir(folder) as it:
                for e in it:
                    if cancel.is_set():
                        return
                    try:
                        if e.is_file() and os.path.splitext(e.name)[1].lower() in SUPPORTED:
                            paths.append(e.path)
                    except OSError:
                        pass
            paths.sort(key=natural_key)
        except Exception as ex:
            self.events.put(("scan_error", gen, str(ex)))
            return

        total, batch = len(paths), []
        for i, p in enumerate(paths):
            if cancel.is_set() or self.closing:
                return
            size = read_size(p)
            if size:
                batch.append((p, size[0], size[1]))
            if len(batch) >= 48:
                self.events.put(("photos", gen, batch, i + 1, total))
                batch = []
                time.sleep(0.002)                    # be polite to the UI thread
        self.events.put(("photos", gen, batch, total, total))
        self.events.put(("scan_done", gen))

    # ------------------------------------------------------- thumb workers
    def _worker(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            gen, photo, tw, th = job
            try:
                # skip stale work: folder changed, zoom changed, or scrolled away
                if (self.closing or gen != self.gen or tw != self.tile_w
                        or photo.idx not in self.live):
                    continue
                try:
                    img = make_thumb(photo.path, tw, th, self.radius)
                except Exception:                    # includes MemoryError
                    img = None
                self.events.put(("thumb", gen, photo.idx, tw, img))
            finally:
                with self.lock:
                    self.requested.discard((gen, photo.idx))

    def _request(self, photo):
        key = (self.gen, photo.idx)
        with self.lock:
            if key in self.requested:
                return
            self.requested.add(key)
        self.jobs.put((self.gen, photo, self.tile_w, photo.th))

    # -------------------------------------------------------------- poll
    def _poll(self):
        if self.closing:
            return
        try:
            self._drain()
        except Exception:
            traceback.print_exc()
        finally:
            if not self.closing:
                self.after(POLL_MS, self._poll)

    def _drain(self):
        end = time.perf_counter() + TICK_BUDGET
        added = False
        while time.perf_counter() < end:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            kind, gen = ev[0], ev[1]
            if gen != self.gen:
                continue
            if kind == "photos":
                if ev[2]:
                    self._append(ev[2])
                    added = True
                if self.scanning:
                    self._set_status(f"Scanning… {ev[3]}/{ev[4]}  ·  {len(self.photos)} photos")
            elif kind == "thumb":
                self._apply_thumb(ev[2], ev[3], ev[4])
            elif kind == "scan_done":
                self._set_scanning(False)
                n = len(self.photos)
                if n:
                    self._set_status(f"{n} photo{'s' if n != 1 else ''}")
                else:
                    self._set_status("No images found in this folder")
                    self._show_empty("No images found", "Try a different folder")
            elif kind == "scan_error":
                self._set_scanning(False)
                self._set_status(f"Scan error: {ev[2]}")
                if not self.photos:
                    self._show_empty("Couldn't read that folder", ev[2])
        if added:
            self._update_scrollregion()
            self._schedule_view()

    # ------------------------------------------------------------ layout
    def _begin_layout(self):
        w = self.canvas.winfo_width()
        if w <= 1:
            w = int(1200 * self.s)
        self.ncols = max(1, (w - 2 * self.pad + self.gap) // (self.tile_w + self.gap))
        used = self.ncols * self.tile_w + (self.ncols - 1) * self.gap
        self.x0 = max(self.pad, (w - used) // 2)             # centre the grid
        self.cols = [Column() for _ in range(self.ncols)]
        self.heights = [self.pad] * self.ncols

    def _place(self, p):
        c = self.heights.index(min(self.heights))            # shortest column
        th = int(self.tile_w * p.h / p.w)
        th = max(int(self.tile_w * MIN_H), min(th, int(self.tile_w * MAX_H)))
        p.x = self.x0 + c * (self.tile_w + self.gap)
        p.y, p.th = self.heights[c], th
        self.heights[c] += th + self.gap
        col = self.cols[c]
        col.idx.append(p.idx); col.tops.append(p.y); col.bots.append(p.y + th)

    def _append(self, batch):
        if not self.cols:
            self._begin_layout()
        for path, w, h in batch:
            p = Photo(len(self.photos), path, w, h)
            self.photos.append(p)
            self._place(p)

    def _relayout(self, clear_cache=False):
        if not self.photos:
            return
        frac = self.canvas.yview()[0]
        self._drop_all_tiles()
        if clear_cache:
            self._cache_clear()
            self.bad.clear()
        self._begin_layout()
        for p in self.photos:
            self._place(p)
        self._update_scrollregion()
        self.canvas.yview_moveto(frac)
        self._sync_overlays()
        self._schedule_view()

    def _update_scrollregion(self):
        if not self.cols:
            return
        total_h = max(self.heights) - self.gap + self.pad
        self.canvas.config(scrollregion=(0, 0, max(1, self.canvas.winfo_width()), total_h))

    # ------------------------------------------------------- virtualising
    def _schedule_view(self):
        if self._view_job is None and not self.closing:
            self._view_job = self.after(16, self._update_view)

    def _update_view(self):
        self._view_job = None
        if not self.cols:
            return
        c = self.canvas
        top = c.canvasy(0) - VIEW_MARGIN
        bottom = c.canvasy(max(1, c.winfo_height())) + VIEW_MARGIN

        want = set()
        for col in self.cols:
            i0 = bisect.bisect_left(col.bots, top)
            i1 = bisect.bisect_right(col.tops, bottom)
            want.update(col.idx[i0:i1])

        for idx in [i for i in self.live if i not in want]:
            self._drop_tile(idx)

        new = [self.photos[i] for i in want if i not in self.live]
        mid = c.canvasy(max(1, c.winfo_height()) // 2)
        new.sort(key=lambda p: abs(p.y + p.th / 2 - mid), reverse=True)  # nearest ends up last
        for p in new:                                # LIFO queue -> nearest loads first
            self._add_tile(p)
        self._cache_evict()
        c.tag_raise("overlay")

    def _add_tile(self, p):
        bad = p.idx in self.bad
        rect = self.canvas.create_rectangle(
            p.x, p.y, p.x + self.tile_w, p.y + p.th,
            fill=T.BAD if bad else T.SURFACE_ALT, outline="")
        self.live[p.idx] = [rect, None]
        if p.idx in self.cache:
            self.cache.move_to_end(p.idx)
            self._draw_image(p)
        elif not bad:
            self._request(p)

    def _drop_tile(self, idx):
        entry = self.live.pop(idx, None)
        if entry:
            for item in entry:
                if item is not None:
                    self.canvas.delete(item)

    def _drop_all_tiles(self):
        for idx in list(self.live):
            self._drop_tile(idx)

    def _draw_image(self, p):
        entry = self.live.get(p.idx)
        hit = self.cache.get(p.idx)
        if not entry or not hit:
            return
        if entry[1] is not None:
            self.canvas.delete(entry[1])
        entry[1] = self.canvas.create_image(p.x, p.y, anchor="nw", image=hit[0])

    def _apply_thumb(self, idx, tw, img):
        if tw != self.tile_w or idx >= len(self.photos):
            return
        if img is None:
            self.bad.add(idx)
            entry = self.live.get(idx)
            if entry:
                self.canvas.itemconfigure(entry[0], fill=T.BAD)
            return
        try:
            tk_img = ImageTk.PhotoImage(img)
        except Exception:
            self.bad.add(idx)
            return
        self._cache_put(idx, tk_img, img.width * img.height * 4)
        if idx in self.live:
            self._draw_image(self.photos[idx])
            self.canvas.tag_raise("overlay")

    # --------------------------------------------------------- LRU cache
    def _cache_put(self, idx, tk_img, nbytes):
        old = self.cache.pop(idx, None)
        if old:
            self.cache_bytes -= old[1]
        self.cache[idx] = (tk_img, nbytes)
        self.cache_bytes += nbytes

    def _cache_evict(self):
        if self.cache_bytes <= CACHE_BYTES:
            return
        for idx in list(self.cache):                 # oldest first
            if self.cache_bytes <= CACHE_BYTES:
                break
            if idx in self.live:
                continue
            self.cache_bytes -= self.cache.pop(idx)[1]

    def _cache_clear(self):
        self.cache.clear()
        self.cache_bytes = 0

    # ------------------------------------------------------------ events
    def _on_yscroll(self, first, last):
        self.vbar.set(first, last)
        self._set_hover(None)
        self._schedule_view()

    def _on_wheel(self, e):
        if not e.delta:
            return
        units = -e.delta if sys.platform == "darwin" else -int(e.delta / 120) or (-1 if e.delta > 0 else 1)
        self.canvas.yview_scroll(units * 3, "units")

    def _on_configure(self, e):
        self._center_empty()
        if e.width != self._last_w:
            self._last_w = e.width
            if self.photos:
                if self._resize_job:
                    self.after_cancel(self._resize_job)
                self._resize_job = self.after(120, self._relayout)
        self._schedule_view()

    def _on_zoom(self, _v=None):
        new_w = int(float(self.zoom_var.get()) * self.s)
        if abs(new_w - self.tile_w) < 4:
            return
        if self._zoom_job:
            self.after_cancel(self._zoom_job)
        self._zoom_job = self.after(150, lambda: self._apply_zoom(new_w))

    def _apply_zoom(self, new_w):
        self._zoom_job = None
        self.tile_w = new_w                         # workers see this and drop old-size jobs
        self._relayout(clear_cache=True)

    # ------------------------------------------------- hover / click / open
    def _photo_at(self, e):
        if not self.cols:
            return None
        cx, cy = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        rel = cx - self.x0
        if rel < 0:
            return None
        step = self.tile_w + self.gap
        c = int(rel // step)
        if c >= self.ncols or rel - c * step > self.tile_w:
            return None
        col = self.cols[c]
        i = bisect.bisect_right(col.tops, cy) - 1
        if i < 0 or cy > col.bots[i]:
            return None
        return self.photos[col.idx[i]]

    def _box(self, item, p):
        if p is None:
            self.canvas.itemconfigure(item, state="hidden")
        else:
            self.canvas.coords(item, p.x, p.y, p.x + self.tile_w, p.y + p.th)
            self.canvas.itemconfigure(item, state="normal")
            self.canvas.tag_raise(item)

    def _set_hover(self, p):
        idx = p.idx if p else None
        if idx == self.hovered:
            return
        self.hovered = idx
        self._box(self.hover_id, p)
        self.canvas.config(cursor="hand2" if p else "")

    def _select(self, p_or_none):
        self.selected = p_or_none.idx if p_or_none else None
        self._box(self.sel_id, p_or_none)

    def _sync_overlays(self):
        self.hovered = None
        self._box(self.hover_id, None)
        if self.selected is not None and self.selected < len(self.photos):
            self._box(self.sel_id, self.photos[self.selected])

    def _on_motion(self, e):
        self._set_hover(self._photo_at(e))

    def _on_click(self, e):
        self.canvas.focus_set()
        self._select(self._photo_at(e))

    def _on_double(self, e):
        p = self._photo_at(e)
        if not p:
            return
        try:
            if sys.platform == "win32":
                os.startfile(p.path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p.path])
            else:
                subprocess.Popen(["xdg-open", p.path])
        except Exception as ex:
            messagebox.showerror("Could not open image", str(ex), parent=self)

    # -------------------------------------------------------------- close
    def on_close(self):
        self.closing = True
        self.scan_cancel.set()
        for _ in range(WORKERS):
            self.jobs.put(None)
        self.destroy()


if __name__ == "__main__":
    app = Gallery()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()