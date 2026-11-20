import bisect
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageDraw, ImageOps, ImageTk
except ImportError:
    try:
        _r = tk.Tk(); _r.withdraw()
        messagebox.showerror("Missing dependency",
                             "This app requires Pillow.\n\n"
                             "    pip install Pillow")
        _r.destroy()
    except Exception:
        print("Install Pillow:  pip install Pillow")
    sys.exit(1)


# ---------------------------------------------------------------- theme
class T:
    BG            = "#f4f6f9"
    SURFACE       = "#ffffff"
    SURFACE_ALT   = "#fafbfd"
    HOVER         = "#eef2f8"
    BORDER        = "#e3e7ed"
    BORDER_STRONG = "#cfd4dd"
    TEXT          = "#1f2430"
    TEXT_MUTED    = "#6b7280"
    TEXT_FAINT    = "#9aa1ab"
    ACCENT        = "#2f6fed"
    ACCENT_HOVER  = "#1f5ad6"
    ACCENT_SOFT   = "#e7efff"


BG_RGB    = (244, 246, 249)
SUPPORTED = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
PADDING   = 20
GAP       = 14
CORNER_R  = 12
POLL_MS   = 40          # main-thread queue drain interval
POLL_BUDGET = 0.02      # max seconds spent draining per tick


def pick_font(root, *candidates, default="TkDefaultFont"):
    fams = set(tkfont.families(root))
    for c in candidates:
        if c in fams:
            return c
    return tkfont.nametofont(default).actual("family")


def enable_hidpi(root):
    if sys.platform != "win32":
        return 1.0
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        return 1.0
    try:
        scale = root.winfo_fpixels("1i") / 96.0
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
        return scale
    except Exception:
        return 1.0


def setup_style(root, font_family):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure("TFrame",  background=T.BG)
    style.configure("TLabel",  background=T.BG, foreground=T.TEXT,
                    font=(font_family, 10))

    style.configure("Accent.TButton",
        background=T.ACCENT, foreground="white", borderwidth=0,
        focuscolor=T.ACCENT, padding=(16, 8),
        font=(font_family, 10, "bold"))
    style.map("Accent.TButton",
        background=[("pressed", T.ACCENT_HOVER), ("active", T.ACCENT_HOVER)])

    style.configure("Secondary.TButton",
        background=T.SURFACE, foreground=T.TEXT, borderwidth=1,
        bordercolor=T.BORDER, focuscolor=T.SURFACE, padding=(12, 6),
        font=(font_family, 10))
    style.map("Secondary.TButton",
        background=[("pressed", T.HOVER), ("active", T.SURFACE_ALT)],
        bordercolor=[("active", T.BORDER_STRONG)])

    style.configure("Vertical.TScrollbar",
        background=T.SURFACE_ALT, troughcolor=T.BG,
        borderwidth=0, arrowcolor=T.TEXT_MUTED)


def make_thumbnail(path, display_w, display_h, radius=CORNER_R):
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        scale = max(display_w / im.width, display_h / im.height)
        new_w = max(display_w, int(im.width  * scale + 0.5))
        new_h = max(display_h, int(im.height * scale + 0.5))
        im = im.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - display_w) // 2
        top  = (new_h - display_h) // 2
        im = im.crop((left, top, left + display_w, top + display_h))

        mask = Image.new("L", (display_w, display_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, display_w - 1, display_h - 1), radius=radius, fill=255)
        out = Image.new("RGB", (display_w, display_h), BG_RGB)
        out.paste(im, (0, 0), mask)
        return out


class PhotoItem:
    __slots__ = ("path", "orig_w", "orig_h", "x", "y", "w", "h")
    def __init__(self, path, orig_w, orig_h):
        self.path, self.orig_w, self.orig_h = path, orig_w, orig_h
        self.x = self.y = self.w = self.h = 0


# ------------------------------------------------------------------ app
class PhotoGallery(tk.Tk):
    def __init__(self):
        super().__init__()
        self._scale   = enable_hidpi(self)
        self._ui_font = pick_font(
            self, "Segoe UI Variable", "Segoe UI", "SF Pro Text",
            "Helvetica Neue", "Inter", "Cantarell", "DejaVu Sans")
        setup_style(self, self._ui_font)

        self.title("Gallery")
        self.configure(bg=T.BG)
        self.geometry(f"{int(1200 * self._scale)}x{int(820 * self._scale)}")
        self.minsize(int(500 * self._scale), int(400 * self._scale))

        # ---- state ----
        self.folder       = None
        self.photos       = []
        self.column_width = 260

        self._photoimages = {}   # path -> ImageTk.PhotoImage
        self._visible     = {}   # path -> {bg,img,hover,sel,photo}
        self._pending     = set()
        self._executor    = ThreadPoolExecutor(max_workers=4)

        self._columns    = []    # [[photos, tops, bottoms], ...] per column
        self._col_heights = []

        self._render_job = None
        self._zoom_job   = None
        self._scan_gen   = 0
        self._selected   = None
        self._last_width = 0
        self._last_cols  = None

        # thread <-> ui
        self._inbox        = queue.Queue()
        self._cancel_scan  = threading.Event()
        self._scan_finished = True
        self._closing      = False

        self._build_ui()
        self._bind_events()
        self._show_empty_state()

        self.after(POLL_MS, self._poll_queue)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        bar = tk.Frame(self, bg=T.SURFACE, height=60)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        left = tk.Frame(bar, bg=T.SURFACE)
        left.pack(side="left", fill="y", padx=20)
        ttk.Button(left, text="📁   Open Folder", style="Accent.TButton",
                   command=self._choose_folder).pack(side="left", pady=12)
        self.folder_label = tk.Label(
            left, text="No folder selected", bg=T.SURFACE, fg=T.TEXT_MUTED,
            font=(self._ui_font, 10))
        self.folder_label.pack(side="left", padx=(18, 0))

        right = tk.Frame(bar, bg=T.SURFACE)
        right.pack(side="right", fill="y", padx=20)

        self.cancel_btn = ttk.Button(
            right, text="Cancel scan", style="Secondary.TButton",
            command=self._cancel_current_scan)

        ttk.Button(right, text="Refresh", style="Secondary.TButton",
                   command=self._refresh).pack(side="right", pady=12)

        zoom_wrap = tk.Frame(right, bg=T.SURFACE)
        zoom_wrap.pack(side="right", padx=(0, 16), pady=18)
        tk.Label(zoom_wrap, text="Size", bg=T.SURFACE, fg=T.TEXT_MUTED,
                 font=(self._ui_font, 10)).pack(side="left", padx=(0, 8))
        self.zoom_var = tk.IntVar(value=self.column_width)
        ttk.Scale(zoom_wrap, from_=160, to=460, length=140,
                  variable=self.zoom_var,
                  command=self._on_zoom).pack(side="left")

        tk.Frame(self, bg=T.BORDER, height=1).pack(fill="x")

        wrap = tk.Frame(self, bg=T.BG)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=T.BG, highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(wrap, orient="vertical",
                                       command=self.canvas.yview)
        self.canvas.config(yscrollcommand=self._on_yscroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.status = tk.Label(self, text="Ready", anchor="w",
                               bg=T.SURFACE, fg=T.TEXT_MUTED,
                               font=(self._ui_font, 9), padx=20, pady=6)
        self.status.pack(side="bottom", fill="x")

        self.empty_frame = tk.Frame(self.canvas, bg=T.BG)
        self._empty_window = None

    def _bind_events(self):
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", lambda e: self._scroll(-3))
        self.canvas.bind("<Button-5>", lambda e: self._scroll(3))
        self.bind("<Control-o>", lambda e: self._choose_folder())
        self.bind("<Control-r>", lambda e: self._refresh())
        self.bind("<Escape>",    lambda e: self._clear_selection())

    # -------------------------------------------------------- empty state
    def _show_empty_state(self):
        if self._empty_window is not None:
            self.canvas.delete(self._empty_window)
            self._empty_window = None
        for w in self.empty_frame.winfo_children():
            w.destroy()

        tk.Label(self.empty_frame, text="🖼", font=(self._ui_font, 64),
                 bg=T.BG, fg=T.TEXT_FAINT).pack(pady=(0, 12))
        tk.Label(self.empty_frame, text="No folder selected",
                 font=(self._ui_font, 18, "bold"),
                 bg=T.BG, fg=T.TEXT).pack()
        tk.Label(self.empty_frame,
                 text="Click Open Folder to browse your photos",
                 font=(self._ui_font, 11), bg=T.BG,
                 fg=T.TEXT_MUTED).pack(pady=(4, 20))
        ttk.Button(self.empty_frame, text="Choose Folder",
                   style="Accent.TButton",
                   command=self._choose_folder).pack()

        self.empty_frame.update_idletasks()
        self._empty_window = self.canvas.create_window(
            0, 0, window=self.empty_frame, anchor="nw")
        self._center_empty()

    def _center_empty(self):
        if self._empty_window is None:
            return
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w <= 1 or h <= 1:
            self.after(50, self._center_empty)
            return
        self.empty_frame.update_idletasks()
        fw = self.empty_frame.winfo_reqwidth()
        fh = self.empty_frame.winfo_reqheight()
        self.canvas.coords(self._empty_window, (w - fw) // 2, (h - fh) // 2)

    def _hide_empty_state(self):
        if self._empty_window is not None:
            self.canvas.delete(self._empty_window)
            self._empty_window = None

    # ----------------------------------------------------------- actions
    def _choose_folder(self):
        folder = filedialog.askdirectory(
            title="Select a folder of photos", parent=self)
        if folder:
            self._load_folder(folder)

    def _refresh(self):
        if self.folder:
            self._load_folder(self.folder)

    def _update_cancel_btn(self):
        if self._scan_finished:
            self.cancel_btn.pack_forget()
        else:
            self.cancel_btn.pack(side="right", pady=12, padx=(0, 8))

    def _load_folder(self, folder):
        # cancel any prior scan
        self._cancel_scan.set()
        self._cancel_scan = threading.Event()
        self._scan_gen   += 1
        gen = self._scan_gen

        self.folder = folder
        self.folder_label.config(text=folder)
        self.status.config(text="Scanning folder…")
        self.title(f"{Path(folder).name} — Gallery")

        # clear view
        for path in list(self._visible.keys()):
            self._remove_visible(path)
        self._hide_empty_state()
        self.canvas.delete("all")
        self.canvas.config(scrollregion=(0, 0, 0, 0))

        self.photos        = []
        self._photoimages.clear()
        self._pending.clear()
        self._columns     = []
        self._col_heights = []
        self._selected    = None
        self._last_width  = 0
        self._last_cols   = None

        self._scan_finished = False
        self._update_cancel_btn()

        self._executor.submit(self._scan_worker, folder, gen)

    def _cancel_current_scan(self):
        self._cancel_scan.set()
        self._scan_finished = True
        n = len(self.photos)
        if n == 0:
            self.status.config(text="Cancelled")
            self._show_empty_state()
        else:
            self.status.config(text=f"Cancelled · {n} photos shown")
        self._update_cancel_btn()

    # -------------------------------------------------------- scan worker
    def _scan_worker(self, folder, gen):
        cancel = self._cancel_scan
        try:
            with os.scandir(folder) as it:
                paths = []
                for entry in it:
                    if cancel.is_set():
                        return
                    try:
                        if entry.is_file() and \
                                Path(entry.name).suffix.lower() in SUPPORTED:
                            paths.append(entry.path)
                    except OSError:
                        continue
            paths.sort(key=lambda p: Path(p).name.lower())
        except OSError as e:
            self._inbox.put(("scan_error", str(e), gen))
            return

        total = len(paths)
        self._inbox.put(("scan_total", total, gen))

        found = 0
        for i, p in enumerate(paths):
            if cancel.is_set():
                return
            try:
                with Image.open(p) as im:
                    w, h = im.size
                if w > 0 and h > 0:
                    self._inbox.put(("photo", PhotoItem(p, w, h), gen))
                    found += 1
            except Exception:
                pass

            if (i & 15) == 0:
                self._inbox.put(("scan_progress", i + 1, total, gen))

        self._inbox.put(("scan_done", gen))

    # ------------------------------------------------------- thumb worker
    def _load_thumbnail(self, photo, gen):
        if photo.path in self._pending:
            return
        self._pending.add(photo.path)
        self._executor.submit(self._thumb_worker, photo, photo.w, photo.h, gen)

    def _thumb_worker(self, photo, w, h, gen):
        try:
            pil = make_thumbnail(photo.path, w, h)
            self._inbox.put(("thumb", photo, pil, w, h, gen))
        except Exception:
            self._inbox.put(("thumb_fail", photo, w, h, gen))

    # --------------------------------------------------------- poll queue
    def _poll_queue(self):
        if self._closing:
            return
        deadline = time.time() + POLL_BUDGET

        new_photos  = []
        thumbs      = []
        scan_done   = False
        progress    = None
        scan_total  = None
        error       = None

        while time.time() < deadline:
            try:
                item = self._inbox.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind == "photo":
                _, p, gen = item
                if gen == self._scan_gen:
                    new_photos.append(p)
            elif kind == "scan_done":
                _, gen = item
                if gen == self._scan_gen:
                    scan_done = True
            elif kind == "scan_total":
                _, total, gen = item
                if gen == self._scan_gen:
                    scan_total = total
            elif kind == "scan_progress":
                _, done, total, gen = item
                if gen == self._scan_gen:
                    progress = (done, total)
            elif kind == "scan_error":
                _, err, gen = item
                if gen == self._scan_gen:
                    error = err
            elif kind == "thumb":
                _, p, pil, w, h, gen = item
                if gen == self._scan_gen:
                    thumbs.append((p, pil, w, h))
                # if gen mismatched, drop silently
            elif kind == "thumb_fail":
                _, p, w, h, gen = item
                if gen == self._scan_gen:
                    self._pending.discard(p.path)
                    entry = self._visible.get(p.path)
                    if entry:
                        try:
                            self.canvas.itemconfigure(entry["bg"], fill="#f5dede")
                        except tk.TclError:
                            pass

        # ---- apply ----
        if error is not None:
            self.status.config(text=f"Scan error: {error}")
            self._scan_finished = True
            self._update_cancel_btn()

        if new_photos:
            self._hide_empty_state()
            self.photos.extend(new_photos)
            self._append_batch(new_photos)

        for p, pil, w, h in thumbs:
            self._finish_thumb(p, pil, w, h)

        if progress:
            done, total = progress
            self.status.config(
                text=f"Scanning… {done}/{total}  ·  {len(self.photos)} added")

        if scan_done:
            self._scan_finished = True
            n = len(self.photos)
            if n == 0:
                self.status.config(text="No images found in this folder")
                self._show_empty_state()
            else:
                self.status.config(text=f"{n} photo{'s' if n != 1 else ''}")
            self._update_cancel_btn()
            if self._render_job is None:
                self._render()

        self.after(POLL_MS, self._poll_queue)

    def _finish_thumb(self, photo, pil, req_w, req_h):
        self._pending.discard(photo.path)
        if photo.w != req_w or photo.h != req_h:
            return
        try:
            tkimg = ImageTk.PhotoImage(pil)
        except Exception:
            return
        self._photoimages[photo.path] = tkimg
        if photo.path in self._visible:
            self._draw_image(photo)

    # ----------------------------------------------------------- layout
    def _column_count(self):
        cw = self.canvas.winfo_width()
        if cw <= 1:
            cw = int(1200 * self._scale)
        usable = cw - PADDING * 2 + GAP
        return max(1, int(usable // (self.column_width + GAP)))

    def _place_photo(self, p, col_idx):
        cw = self.column_width
        x  = PADDING + col_idx * (cw + GAP)
        y  = self._col_heights[col_idx]
        h  = max(1, int(p.orig_h * (cw / p.orig_w)))
        p.x, p.y, p.w, p.h = x, y, cw, h
        self._col_heights[col_idx] = y + h + GAP
        col = self._columns[col_idx]
        col[0].append(p)
        col[1].append(y)
        col[2].append(y + h)

    def _update_scrollregion(self):
        n = len(self._columns)
        if n == 0:
            return
        cw = self.column_width
        total_h = max(self._col_heights) + PADDING - GAP
        total_w = PADDING * 2 + n * cw + (n - 1) * GAP
        self.canvas.config(scrollregion=(0, 0, total_w, total_h))

    def _append_batch(self, new_photos):
        """Incremental layout — O(new) instead of O(all)."""
        if not new_photos:
            return
        n = self._column_count()
        if not self._columns or len(self._columns) != n:
            # column count changed (or first batch) → full relayout
            self._relayout()
            return

        for p in new_photos:
            col_idx = self._col_heights.index(min(self._col_heights))
            self._place_photo(p, col_idx)

        self._update_scrollregion()
        self._schedule_render()

    def _relayout(self):
        if not self.photos:
            return
        yview = self.canvas.yview()
        self.canvas.delete("all")
        self._visible.clear()

        n  = self._column_count()
        self._columns     = [[[], [], []] for _ in range(n)]
        self._col_heights = [PADDING] * n

        for p in self.photos:
            col_idx = self._col_heights.index(min(self._col_heights))
            self._place_photo(p, col_idx)

        self._last_cols = n
        self._update_scrollregion()
        self._render()
        if yview:
            self.canvas.yview_moveto(yview[0])

    # -------------------------------------------------------- virtualise
    def _iter_visible(self, top, bottom):
        for col, tops, bottoms in self._columns:
            if not col:
                continue
            i0 = bisect.bisect_left(bottoms, top)
            i1 = bisect.bisect_right(tops, bottom)
            for i in range(i0, i1):
                yield col[i]

    def _render(self):
        self._render_job = None
        if not self._columns:
            return

        top    = self.canvas.canvasy(0) - 400
        bottom = self.canvas.canvasy(max(1, self.canvas.winfo_height())) + 400

        visible_paths = set()
        visible_photos = []
        for p in self._iter_visible(top, bottom):
            visible_paths.add(p.path)
            visible_photos.append(p)

        for path in list(self._visible.keys()):
            if path not in visible_paths:
                self._remove_visible(path)

        for p in visible_photos:
            if p.path not in self._visible:
                self._add_visible(p)

    def _schedule_render(self):
        if self._render_job or self._closing:
            return
        self._render_job = self.after(30, self._render)

    def _add_visible(self, photo):
        bg_id = self.canvas.create_rectangle(
            photo.x, photo.y,
            photo.x + photo.w, photo.y + photo.h,
            fill=T.SURFACE_ALT, outline="", width=0)
        self._visible[photo.path] = {
            "bg": bg_id, "img": None, "hover": None, "sel": None,
        }
        if photo.path in self._photoimages:
            self._draw_image(photo)
        else:
            self._load_thumbnail(photo, self._scan_gen)

    def _remove_visible(self, path):
        entry = self._visible.pop(path, None)
        if not entry:
            return
        for key in ("bg", "img", "hover", "sel"):
            if entry.get(key) is not None:
                try:
                    self.canvas.delete(entry[key])
                except tk.TclError:
                    pass

    def _draw_image(self, photo):
        entry = self._visible.get(photo.path)
        if not entry:
            return
        tkimg = self._photoimages.get(photo.path)
        if tkimg is None:
            return
        if entry["img"] is not None:
            self.canvas.delete(entry["img"])
        img_id = self.canvas.create_image(
            photo.x, photo.y, anchor="nw", image=tkimg)
        entry["img"] = img_id
        self.canvas.tag_bind(img_id, "<Button-1>",
            lambda e, p=photo: self._on_photo_click(p))
        self.canvas.tag_bind(img_id, "<Double-Button-1>",
            lambda e, p=photo: self._on_photo_double_click(p))
        self.canvas.tag_bind(img_id, "<Enter>",
            lambda e, p=photo: self._on_photo_hover(p, True))
        self.canvas.tag_bind(img_id, "<Leave>",
            lambda e, p=photo: self._on_photo_hover(p, False))

    # ----------------------------------------------------------- events
    def _on_yscroll(self, first, last):
        self.scrollbar.set(first, last)
        self._schedule_render()

    def _on_mousewheel(self, event):
        if event.delta == 0:
            return
        self._scroll(-3 if event.delta > 0 else 3)

    def _scroll(self, units):
        self.canvas.yview_scroll(units, "units")

    def _on_canvas_resize(self, event):
        if self._empty_window is not None:
            self._center_empty()
        if not self.photos:
            return
        if self._last_width == event.width:
            return
        self._last_width = event.width
        new_cols = self._column_count()
        if new_cols != self._last_cols:
            self._relayout()

    def _on_zoom(self, _value=None):
        new_width = int(self.zoom_var.get())
        if abs(new_width - self.column_width) < 4:
            return
        self.column_width = new_width
        self._photoimages.clear()
        if self._zoom_job:
            self.after_cancel(self._zoom_job)
        self._zoom_job = self.after(150, self._relayout)

    # -------------------------------------------------- item interactions
    def _on_photo_click(self, photo):
        self._clear_selection()
        self._selected = photo.path
        entry = self._visible.get(photo.path)
        if entry:
            entry["sel"] = self.canvas.create_rectangle(
                photo.x, photo.y,
                photo.x + photo.w, photo.y + photo.h,
                outline=T.ACCENT, width=3)

    def _clear_selection(self):
        self._selected = None
        for entry in self._visible.values():
            if entry.get("sel") is not None:
                self.canvas.delete(entry["sel"])
                entry["sel"] = None

    def _on_photo_hover(self, photo, entering):
        entry = self._visible.get(photo.path)
        if not entry:
            return
        if entering and entry.get("hover") is None:
            entry["hover"] = self.canvas.create_rectangle(
                photo.x, photo.y,
                photo.x + photo.w, photo.y + photo.h,
                outline=T.ACCENT_HOVER, width=2)
        elif not entering and entry.get("hover") is not None:
            self.canvas.delete(entry["hover"])
            entry["hover"] = None

    def _on_photo_double_click(self, photo):
        try:
            if sys.platform == "win32":
                os.startfile(photo.path)
            elif sys.platform == "darwin":
                subprocess.run(["open", photo.path], check=False)
            else:
                subprocess.run(["xdg-open", photo.path], check=False)
        except Exception as e:
            messagebox.showerror("Could not open image", str(e), parent=self)

    # -------------------------------------------------------------- close
    def on_close(self):
        self._closing = True
        self._cancel_scan.set()
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self._executor.shutdown(wait=False)
        self.destroy()


if __name__ == "__main__":
    app = PhotoGallery()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()