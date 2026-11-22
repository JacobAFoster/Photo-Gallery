# Gallery

A fast, Pinterest-style photo gallery for your desktop. Point it at a folder and it lays your photos out in a masonry grid that keeps every photo's original proportions. It is built with Python, Tkinter and Pillow, and stays smooth even with thousands of photos.

<!-- Add a screenshot: ![Gallery screenshot](docs/screenshot.png) -->

## Features

- **Masonry layout** – photos keep their aspect ratio and pack into columns, with rounded corners
- **Opens instantly** – the grid starts filling while the folder is still being scanned
- **Handles big folders** – only the photos near the screen are kept in memory, so thousands of images scroll smoothly
- **Handles big images** – large JPEGs are decoded pre-shrunk, and oversized images are skipped instead of freezing the app
- **Adjustable size** – a slider changes the column width on the fly
- **Responsive layout** – the number of columns adapts as you resize the window
- **EXIF-aware** – phone photos taken in portrait are shown the right way up
- **Natural sorting** – `img2.jpg` comes before `img10.jpg`
- **HiDPI aware** on Windows
- Supports **JPG, PNG, GIF, BMP, WebP and TIFF**

## Requirements

- Python 3.8 or newer
- [Pillow](https://pypi.org/project/Pillow/) 9.0 or newer
- Tkinter (included with the standard Python installer on Windows and macOS; on Debian/Ubuntu install it with `sudo apt install python3-tk`)

## Installation

```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
pip install Pillow
```

## Usage

```bash
python main.py
```

Click **Open Folder** and choose a folder of photos.

### Controls

| Action | How |
| --- | --- |
| Open a folder | **Open Folder** button or `Ctrl+O` |
| Reload the folder | **Refresh** button, `Ctrl+R` or `F5` |
| Stop a scan | **Cancel scan** button |
| Change photo size | **Size** slider |
| Scroll | Mouse wheel, scrollbar, arrow keys, `Page Up` / `Page Down`, `Home` / `End` |
| Select a photo | Click |
| Clear selection | `Esc` |
| Open a photo in your default viewer | Double-click |

## How it works

| Part | What it does |
| --- | --- |
| **Scan thread** | Reads only image headers (width and height) and sends them to the UI in small batches. |
| **Layout** | Each photo is placed in the currently shortest column, so adding a photo is cheap. |
| **Virtualised canvas** | Only tiles near the viewport exist as canvas items. Scroll away and they are deleted; scroll back and they are recreated. |
| **Thumbnail workers** | Four background threads fed by a last-in-first-out queue, so the photos you just scrolled to load first. Jobs for tiles that have already scrolled out of view are skipped. |
| **Cache** | Thumbnails are kept in a 250 MB least-recently-used cache. |
| **UI budget** | The main thread does at most about 12 ms of work per tick, which keeps scrolling responsive. |

## Configuration

Settings are constants at the top of `main.py`:

| Constant | Default | Meaning |
| --- | --- | --- |
| `DEFAULT_TILE_W` | `260` | Starting column width in pixels |
| `MAX_DECODE_PIXELS` | `36_000_000` | Non-JPEG images larger than this get a placeholder tile instead of being decoded |
| `CACHE_BYTES` | `250 MB` | Memory limit for cached thumbnails |
| `WORKERS` | `4` | Number of thumbnail threads |
| `VIEW_MARGIN` | `500` | Pixels above and below the window that are kept loaded |

## Limitations

- Non-JPEG images above `MAX_DECODE_PIXELS` (for example a very large PNG) are not thumbnailed and show a pink placeholder. Decoding them can stall the interface.
- Subfolders are not scanned; only images directly inside the chosen folder are shown.
- Animated GIFs and WebPs show their first frame.

## Planned updates

### Performance
- [ ] On-disk thumbnail cache so reopening a big folder is instant
- [ ] Decode thumbnails in separate processes so very large PNG/TIFF files can be shown instead of skipped
- [ ] Fade-in animation when thumbnails finish loading

### Viewing
- [ ] Full-size viewer (lightbox) with next/previous, zoom and pan
- [ ] Show the file name and size on hover
- [ ] Info panel with EXIF data (camera, date, dimensions)
- [ ] Play animated GIFs and WebPs
- [ ] HEIC/HEIF and RAW format support

### Organising
- [ ] Scan subfolders (optional recursive mode)
- [ ] Sort by name, date taken, date modified or file size
- [ ] Search and filter by file name
- [ ] Multi-select with Shift/Ctrl-click
- [ ] Favourites and simple albums

### File actions
- [ ] Right-click menu: open, show in folder, copy path, rename, delete (to the recycle bin)
- [ ] Drag and drop a folder onto the window to open it
- [ ] Drag photos out of the gallery into other apps

### App polish
- [ ] Remember the last folder, window size and photo size between sessions
- [ ] Dark mode
- [ ] Arrow-key navigation between photos
- [ ] Settings window for the options currently set as constants
- [ ] Packaged `.exe` / app builds with PyInstaller

### Project
- [ ] Unit tests for layout, hit-testing and thumbnail generation
- [ ] Continuous integration (lint and tests on every push)
- [ ] Screenshots and a short demo GIF in this README

## Contributing

Issues and pull requests are welcome.

## License

Add a license of your choice (for example MIT) as a `LICENSE` file.
