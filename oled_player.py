"""OLED Video Player.

Streams any video file (or the live PC screen) to an Arduino Nano with a 128x64 OLED
(running OledStream.ino) and plays the video's sound on the PC, kept in sync.
"""
import ctypes
import ctypes.wintypes
import os
import shutil
import subprocess
import tempfile
import threading
import time

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import imageio_ffmpeg
import mss
import numpy as np
import pygame
import serial
import serial.tools.list_ports
import yt_dlp

# Draw at real screen pixels (sharp on scaled displays). Screen capture needs this too,
# so it sees the whole screen. UI is the display scale, e.g. 1.5 at 150%.
UI = 1.0
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    UI = ctypes.windll.user32.GetDpiForSystem() / 96
except (AttributeError, OSError):
    pass


def px(n):
    return round(n * UI)


W, H = 128, 64
BAUD = 1_000_000            # must match BAUD in OledStream.ino
SCALE = max(1, px(4))       # OLED preview pixel size
VIEW_H = H * SCALE          # real-video view, same height as the OLED preview
VIEW_W = VIEW_H * 16 // 9
MOUSE_AREA = (512, 256)     # screen pixels captured in "Around mouse" mode (2:1 like the OLED)
OLED_BLUE = (90, 200, 255)
KNOWN_USB_SERIAL = {0x0403, 0x1A86, 0x2341, 0x10C4, 0x067B}  # FTDI, CH340, Arduino, CP210x, PL2303

STYLES = ["Threshold", "Auto threshold", "Dither", "Outline"]
FITS = ["Fit (black bars)", "Fill (crop)", "Stretch"]
VIDEO_TYPES = [("Videos", "*.mp4 *.mov *.mkv *.avi *.webm *.gif *.m4v *.wmv *.flv"), ("All files", "*.*")]


def _bayer(n):
    m = np.array([[0, 2], [3, 1]])
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return (m + 0.5) / m.size


BAYER = np.tile(_bayer(8), (H // 8, W // 8)) * 255


def fit_image(src, w, h, fit):
    """Resize a gray or color image into a w x h box, using one of FITS."""
    sh, sw = src.shape[:2]
    if fit == FITS[2]:
        return cv2.resize(src, (w, h), interpolation=cv2.INTER_AREA)
    scale = (max if fit == FITS[1] else min)(w / sw, h / sh)
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    img = cv2.resize(src, (nw, nh), interpolation=cv2.INTER_AREA)
    out = np.zeros((h, w) + src.shape[2:], np.uint8)
    # Paste img centered, cropping whatever overflows.
    ox, oy = (w - nw) // 2, (h - nh) // 2
    sx, sy = max(0, -ox), max(0, -oy)
    dx, dy = max(0, ox), max(0, oy)
    cw, ch = min(w - dx, nw - sx), min(h - dy, nh - sy)
    out[dy:dy + ch, dx:dx + cw] = img[sy:sy + ch, sx:sx + cw]
    return out


def to_bits(frame, s):
    """Turn a BGR video frame into a 64x128 bool image using settings dict s."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    level = s["level"]
    if s["style"] == "Outline":
        big = fit_image(gray, W * 2, H * 2, s["fit"])
        edges = cv2.Canny(cv2.GaussianBlur(big, (3, 3), 0), level // 2, level)
        bits = edges.reshape(H, 2, W, 2).max(axis=(1, 3)) > 0
    else:
        img = fit_image(gray, W, H, s["fit"])
        if s["style"] == "Threshold":
            bits = img > level
        elif s["style"] == "Auto threshold":
            _, t = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            bits = t > 0
        else:  # Dither: level acts as brightness
            bits = (img.astype(np.int16) + (128 - level)) > BAYER
    return ~bits if s["invert"] else bits


def pack(bits):
    """Pack a 64x128 bool image into the OLED's page layout (8 pages x 128 columns, LSB on top)."""
    b = bits.reshape(8, 8, W).astype(np.uint8)
    out = np.zeros((8, W), np.uint8)
    for i in range(8):
        out |= b[:, i, :] << i
    return out.tobytes()


def preview_ppm(bits):
    rgb = np.zeros((H, W, 3), np.uint8)
    rgb[bits] = OLED_BLUE
    big = np.repeat(np.repeat(rgb, SCALE, 0), SCALE, 1)
    big[SCALE - 1::SCALE, :] //= 3   # faint pixel grid like a real OLED
    big[:, SCALE - 1::SCALE] //= 3
    return b"P6 %d %d 255\n" % (W * SCALE, H * SCALE) + big.tobytes()


def video_view(frame):
    """Shrink a BGR frame to fit the real-video view, with black bars."""
    return fit_image(frame, VIEW_W, VIEW_H, FITS[0])


def video_ppm(view):
    rgb = cv2.cvtColor(view, cv2.COLOR_BGR2RGB)
    return b"P6 %d %d 255\n" % (VIEW_W, VIEW_H) + rgb.tobytes()


def fmt_time(sec):
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


def extract_audio(path, out_path):
    """Write the video's sound track to an .ogg file. Returns False if the video has no sound."""
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", path,
           "-vn", "-ac", "2", "-ar", "44100", "-c:a", "libvorbis", "-q:a", "5", out_path]
    r = subprocess.run(cmd, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode == 0 and os.path.getsize(out_path) > 0


def download(url, folder, on_progress):
    """Download a YouTube (or Instagram, TikTok, ...) link as a small mp4. Returns (path, title)."""
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                on_progress(d.get("downloaded_bytes", 0) / total)

    opts = {
        "format": "bv*[height<=480]+ba/b[height<=480]/bv*+ba/b",   # the OLED is tiny, no need for HD
        "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "noplaylist": True,
        "js_runtimes": {"node": {}},
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return info["requested_downloads"][0]["filepath"], info.get("title") or url


def find_ports():
    ports = list(serial.tools.list_ports.comports())
    ports.sort(key=lambda p: p.vid not in KNOWN_USB_SERIAL)
    return [f"{p.device} - {p.description}" for p in ports]


class Board:
    """Serial link to the Nano. The Nano sends 'R' whenever it's ready for the next frame."""

    def __init__(self, port):
        self.ser = serial.Serial(port, BAUD, timeout=0.5)
        time.sleep(1.8)                  # opening the port resets the Nano; wait for its bootloader
        self.ser.reset_input_buffer()
        self.bad_frames = 0

    def wait_ready(self):
        while True:
            b = self.ser.read(1)
            if b == b"!":
                self.bad_frames += 1
                continue
            return b == b"R"

    def send(self, data):
        """Send one packed frame as: 0xAA 0x55 marker, 1024 pixel bytes, XOR checksum."""
        check = int(np.bitwise_xor.reduce(np.frombuffer(data, np.uint8)))
        self.ser.write(b"\xAA\x55" + data + bytes([check]))

    def resync(self):
        self.ser.reset_input_buffer()

    def close(self):
        self.ser.close()


class Player(threading.Thread):
    def __init__(self, app, path, board):
        super().__init__(daemon=True)
        self.app, self.path, self.board = app, path, board
        self.stop_flag = threading.Event()
        self.paused = False
        self.seek_to = None
        self.has_audio = False
        self.t0 = time.perf_counter()
        self.pause_pos = 0.0
        self.packet = None          # newest frame, packed and ready for the Nano
        self.oled_fps = 0.0
        self.board_misses = 0

    # Clock: seconds into the video, frozen while paused.
    def _now(self):
        if self.paused:
            return self.pause_pos
        return time.perf_counter() - self.t0

    def _start_at(self, sec):
        self.t0 = time.perf_counter() - sec
        if self.has_audio:
            pygame.mixer.music.play(start=sec)
            if self.paused:
                pygame.mixer.music.pause()
        if self.paused:
            self.pause_pos = sec

    def set_paused(self, paused):
        if paused == self.paused:
            return
        if paused:
            self.pause_pos = self._now()
            self.paused = True
            if self.has_audio:
                pygame.mixer.music.pause()
        else:
            self.paused = False
            self.t0 = time.perf_counter() - self.pause_pos
            if self.has_audio:
                pygame.mixer.music.unpause()

    def run(self):
        app = self.app
        cap = cv2.VideoCapture(self.path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        duration = total / fps

        app.set_status("Preparing sound...")
        self.has_audio = extract_audio(self.path, app.audio_file)
        if self.has_audio:
            pygame.mixer.music.load(app.audio_file)

        cur, frame, view = -1, None, None
        shown, fps_t, last_convert = 0, time.perf_counter(), 0.0
        self._start_at(0)
        if self.board:
            self.board.resync()
            sender = threading.Thread(target=self.send_loop, daemon=True)
            sender.start()

        # Video loop: decode at the video's own speed for the app; the sender thread
        # hands the newest frame to the Nano whenever it asks for one.
        while not self.stop_flag.is_set():
            if self.seek_to is not None:
                sec, self.seek_to = self.seek_to, None
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(sec * fps))
                cur = int(sec * fps) - 1
                self._start_at(sec)

            t = self._now()
            idx = int(t * fps)
            if idx >= total:
                if app.loop_var.get():
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    cur = -1
                    self._start_at(0)
                    idx = 0
                else:
                    break

            new_frame = False
            if idx > cur:
                if idx - cur > 90:              # far behind (or seek): jump instead of decoding every frame
                    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                    cur = idx - 1
                while cur < idx - 1 and cap.grab():
                    cur += 1
                ok, f = cap.read()
                if not ok:
                    if frame is None:
                        app.set_status("Could not read this video.")
                        break
                    total = cur + 1             # frame count was overestimated; treat as end
                    continue
                cur += 1
                frame, view, new_frame = f, video_view(f), True
                shown += 1

            # Convert new frames, and keep re-converting while paused so Look changes show up.
            now = time.perf_counter()
            if new_frame or now - last_convert > 0.05:
                bits = to_bits(frame, app.settings)
                self.packet = pack(bits)
                app.latest = (bits, view, min(t, duration), duration)
                last_convert = now

            if now - fps_t >= 1:
                video_fps = shown / (now - fps_t)
                shown, fps_t = 0, now
                state = "Paused" if self.paused else f"Playing {video_fps:.0f} fps"
                if self.board:
                    oled = f"OLED {self.oled_fps:.0f} fps on {app.port_name()}"
                    if self.board_misses >= 3:
                        oled = "board not answering - is OledStream.ino uploaded?"
                else:
                    oled = "no board (preview only)"
                app.set_status(f"{state}  |  {oled}")

            # Sleep until the next video frame is due.
            wait = (cur + 1) / fps - self._now()
            time.sleep(min(max(wait, 0.001), 0.05))

        self.stop_flag.set()
        if self.board:
            sender.join(timeout=2)
        cap.release()
        if self.has_audio:
            pygame.mixer.music.stop()
            pygame.mixer.music.unload()
        app.player_done(self)

    def send_loop(self):
        """Send the newest frame each time the Nano asks. The frame is always ready in advance."""
        sent, fps_t = 0, time.perf_counter()
        while not self.stop_flag.is_set() and self.board:
            if self.packet is None:
                time.sleep(0.01)
                continue
            try:
                if not self.board.wait_ready():
                    self.board_misses += 1
                    continue
                self.board_misses = 0
                self.board.send(self.packet)
            except serial.SerialException:
                self.app.board_lost()
                self.board = None
                break
            sent += 1
            now = time.perf_counter()
            if now - fps_t >= 1:
                self.oled_fps = sent / (now - fps_t)
                sent, fps_t = 0, now


def screen_areas():
    with mss.MSS() as sct:
        count = len(sct.monitors) - 1
    return [f"Screen {i}" for i in range(1, count + 1)] + ["Around mouse"]


class ScreenPlayer(Player):
    """Streams the live PC screen instead of a video file."""

    def __init__(self, app, board):
        super().__init__(app, None, board)

    def _area(self, sct):
        area = self.app.settings["area"]
        if area != "Around mouse":
            return sct.monitors[int(area.split()[-1])]
        p = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetPhysicalCursorPos(ctypes.byref(p))
        v = sct.monitors[0]                 # bounding box of all screens
        w, h = MOUSE_AREA
        left = min(max(p.x - w // 2, v["left"]), v["left"] + v["width"] - w)
        top = min(max(p.y - h // 2, v["top"]), v["top"] + v["height"] - h)
        return {"left": left, "top": top, "width": w, "height": h}

    def run(self):
        app = self.app
        sct = mss.MSS()
        if self.board:
            self.board.resync()
            sender = threading.Thread(target=self.send_loop, daemon=True)
            sender.start()

        frame, view = None, None
        shown, fps_t = 0, time.perf_counter()
        while not self.stop_flag.is_set():
            start = time.perf_counter()
            if not self.paused or frame is None:
                try:
                    shot = sct.grab(self._area(sct))
                except mss.ScreenShotError:
                    time.sleep(0.1)             # e.g. the lock screen; try again
                    continue
                frame = cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR)
                view = video_view(frame)
                shown += 1

            bits = to_bits(frame, app.settings)
            self.packet = pack(bits)
            app.latest = (bits, view, 0, 0)

            now = time.perf_counter()
            if now - fps_t >= 1:
                cap_fps = shown / (now - fps_t)
                shown, fps_t = 0, now
                state = "Screen paused" if self.paused else f"Streaming screen {cap_fps:.0f} fps"
                if self.board:
                    oled = f"OLED {self.oled_fps:.0f} fps on {app.port_name()}"
                    if self.board_misses >= 3:
                        oled = "board not answering - is OledStream.ino uploaded?"
                else:
                    oled = "no board (preview only)"
                app.set_status(f"{state}  |  {oled}")

            time.sleep(max(0.001, 1 / 30 - (time.perf_counter() - start)))

        self.stop_flag.set()
        if self.board:
            sender.join(timeout=2)
        sct.close()
        app.player_done(self)


class App:
    def __init__(self, root):
        self.root = root
        root.title("OLED Video Player")
        root.resizable(False, False)
        pygame.mixer.init(frequency=44100)
        self.audio_file = os.path.join(tempfile.gettempdir(), "oled_player_audio.ogg")
        self.download_dir = os.path.join(tempfile.gettempdir(), "oled_player_downloads")
        self.ui_calls = []           # callbacks queued by background threads, run on the UI thread

        self.path = None
        self.board = None
        self.board_port = None
        self.player = None
        self.latest = None
        self.last_view = None
        self.still = None
        self.status = "Open a video to start."
        self.settings = {}

        pad = dict(padx=6, pady=4)
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        ttk.Button(top, text="Open video...", command=self.open_video).pack(side="left")
        self.file_label = ttk.Label(top, text="No video selected", width=50)
        self.file_label.pack(side="left", padx=8)
        areas = screen_areas()
        self.area_var = tk.StringVar(value=areas[0])
        ttk.Combobox(top, textvariable=self.area_var, values=areas, state="readonly", width=14).pack(side="right")
        ttk.Button(top, text="Stream screen", command=self.stream_screen).pack(side="right", padx=6)

        link_row = ttk.Frame(root)
        link_row.pack(fill="x", **pad)
        ttk.Label(link_row, text="Link").pack(side="left")
        self.link_var = tk.StringVar()
        link_entry = ttk.Entry(link_row, textvariable=self.link_var, width=58)
        link_entry.pack(side="left", padx=6, fill="x", expand=True)
        link_entry.bind("<Return>", lambda e: self.load_link())
        self.link_btn = ttk.Button(link_row, text="Play link", command=self.load_link)
        self.link_btn.pack(side="left")

        views = ttk.Frame(root)
        views.pack(padx=10, pady=6)
        ttk.Label(views, text="Real video").grid(row=0, column=0, sticky="w")
        ttk.Label(views, text="OLED preview").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.video_label = tk.Label(views, bg="black", bd=0)
        self.video_label.grid(row=1, column=0)
        self.canvas = tk.Label(views, bg="black", bd=0)
        self.canvas.grid(row=1, column=1, padx=(12, 0))

        self.seek_var = tk.DoubleVar()
        seek_row = ttk.Frame(root)
        seek_row.pack(fill="x", **pad)
        self.seek = ttk.Scale(seek_row, from_=0, to=1, variable=self.seek_var, orient="horizontal")
        self.seek.pack(side="left", fill="x", expand=True)
        self.seek.bind("<ButtonPress-1>", self.on_seek_drag)
        self.seek.bind("<B1-Motion>", self.on_seek_drag)
        self.seek.bind("<ButtonRelease-1>", self.on_seek)
        self.dragging = False
        self.duration = 0
        self.time_label = ttk.Label(seek_row, text="0:00 / 0:00", width=12, anchor="e")
        self.time_label.pack(side="left")

        ctl = ttk.Frame(root)
        ctl.pack(fill="x", **pad)
        self.play_btn = ttk.Button(ctl, text="Play", width=8, command=self.play_pause)
        self.play_btn.pack(side="left")
        ttk.Button(ctl, text="Stop", width=8, command=self.stop).pack(side="left", padx=4)
        self.loop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="Loop", variable=self.loop_var).pack(side="left", padx=8)
        ttk.Label(ctl, text="Volume").pack(side="left", padx=(16, 4))
        self.vol_var = tk.DoubleVar(value=80)
        ttk.Scale(ctl, from_=0, to=100, variable=self.vol_var, length=px(140),
                  command=lambda v: pygame.mixer.music.set_volume(float(v) / 100)).pack(side="left")
        pygame.mixer.music.set_volume(0.8)

        look = ttk.LabelFrame(root, text="Look")
        look.pack(fill="x", **pad)
        self.style_var = tk.StringVar(value=STYLES[1])
        self.fit_var = tk.StringVar(value=FITS[0])
        self.invert_var = tk.BooleanVar(value=False)
        self.level_var = tk.IntVar(value=128)
        ttk.Label(look, text="Style").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        ttk.Combobox(look, textvariable=self.style_var, values=STYLES, state="readonly", width=15).grid(row=0, column=1)
        ttk.Label(look, text="Fit").grid(row=0, column=2, sticky="w", padx=(12, 4))
        ttk.Combobox(look, textvariable=self.fit_var, values=FITS, state="readonly", width=15).grid(row=0, column=3)
        ttk.Checkbutton(look, text="Invert", variable=self.invert_var).grid(row=0, column=4, padx=12)
        ttk.Label(look, text="Level").grid(row=1, column=0, sticky="w", padx=4, pady=3)
        ttk.Scale(look, from_=10, to=245, variable=self.level_var, length=px(420),
                  command=lambda v: self.update_settings()).grid(row=1, column=1, columnspan=4, sticky="we")
        for v in (self.style_var, self.fit_var, self.invert_var, self.area_var):
            v.trace_add("write", lambda *a: self.update_settings())

        conn = ttk.LabelFrame(root, text="Arduino")
        conn.pack(fill="x", **pad)
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(conn, textvariable=self.port_var, state="readonly", width=48)
        self.port_box.pack(side="left", padx=4, pady=4)
        ttk.Button(conn, text="Refresh", command=self.refresh_ports).pack(side="left")
        self.board_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(conn, text="Send to OLED", variable=self.board_var).pack(side="left", padx=10)

        self.status_label = ttk.Label(root, anchor="w")
        self.status_label.pack(fill="x", padx=10, pady=(0, 8))

        self.update_settings()
        self.refresh_ports()
        self.show_bits(np.zeros((H, W), bool))
        self.show_video(np.zeros((VIEW_H, VIEW_W, 3), np.uint8))
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.tick()

    # --- helpers callable from the player thread -------------------------------------------
    def set_status(self, text):
        self.status = text

    def board_lost(self):
        self.status = "Lost connection to the board."
        self.board, self.board_port = None, None

    def player_done(self, player):
        if self.player is player:
            self.player = None

    def port_name(self):
        return self.port_var.get().split(" - ")[0]

    # --- UI ---------------------------------------------------------------------------------
    def update_settings(self):
        self.settings = {"style": self.style_var.get(), "fit": self.fit_var.get(),
                         "invert": self.invert_var.get(), "level": int(self.level_var.get()),
                         "area": self.area_var.get()}
        if self.still is not None and not self.player:
            self.show_bits(to_bits(self.still, self.settings))

    def refresh_ports(self):
        ports = find_ports()
        self.port_box["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def show_bits(self, bits):
        self.photo = tk.PhotoImage(data=preview_ppm(bits), format="PPM")
        self.canvas.configure(image=self.photo)

    def show_video(self, view):
        self.video_photo = tk.PhotoImage(data=video_ppm(view), format="PPM")
        self.video_label.configure(image=self.video_photo)

    def open_video(self):
        path = filedialog.askopenfilename(title="Choose a video", filetypes=VIDEO_TYPES)
        if path:
            self.load_file(path, os.path.basename(path))

    def load_file(self, path, name):
        self.stop()
        cap = cv2.VideoCapture(path)
        ok, frame = cap.read()
        cap.release()
        if not ok:
            messagebox.showerror("OLED Video Player", "Could not open that video.")
            return False
        self.path, self.still = path, frame
        self.show_video(video_view(frame))
        self.file_label.configure(text=name)
        self.update_settings()
        self.status = "Ready. Press Play."
        return True

    def load_link(self):
        url = self.link_var.get().strip()
        if not url:
            self.status = "Paste a YouTube link into the Link box first."
            return
        self.link_btn.configure(state="disabled")
        self.status = "Getting video info..."
        os.makedirs(self.download_dir, exist_ok=True)

        def progress(frac):
            self.status = f"Downloading... {frac * 100:.0f}%"

        def work():
            try:
                path, title = download(url, self.download_dir, progress)
            except Exception as e:
                msg = str(e).replace("ERROR: ", "")
                self.ui_calls.append(lambda: self.link_failed(msg))
                return
            self.ui_calls.append(lambda: self.link_ready(path, title))

        threading.Thread(target=work, daemon=True).start()

    def link_ready(self, path, title):
        self.link_btn.configure(state="normal")
        if self.load_file(path, title):
            self.play_pause()

    def link_failed(self, msg):
        self.link_btn.configure(state="normal")
        self.status = "Download failed."
        messagebox.showerror("OLED Video Player", f"Could not download that link.\n\n{msg}")

    def connect(self):
        port = self.port_name()
        if not self.board_var.get() or not port:
            return None
        if self.board and self.board_port == port:
            return self.board
        if self.board:
            self.board.close()
        self.status = f"Connecting to {port}..."
        self.root.update_idletasks()
        try:
            self.board, self.board_port = Board(port), port
        except serial.SerialException as e:
            messagebox.showwarning("OLED Video Player",
                                   f"Could not open {port}.\nClose the Arduino IDE Serial Monitor if it's open.\n\n{e}")
            self.board, self.board_port = None, None
        return self.board

    def play_pause(self):
        if self.player:
            self.player.set_paused(not self.player.paused)
            self.play_btn.configure(text="Play" if self.player.paused else "Pause")
            return
        if not self.path:
            self.open_video()
            if not self.path:
                return
        board = self.connect()
        self.player = Player(self, self.path, board)
        self.player.start()
        self.play_btn.configure(text="Pause")

    def stream_screen(self):
        self.stop()
        board = self.connect()
        self.file_label.configure(text="Live screen")
        self.player = ScreenPlayer(self, board)
        self.player.start()
        self.play_btn.configure(text="Pause")

    def stop(self):
        p = self.player
        if p:
            p.stop_flag.set()
            p.join(timeout=3)
            self.player = None
        self.play_btn.configure(text="Play")

    def on_seek_drag(self, event):
        # Jump straight to the mouse position (ttk's own trough click only steps a little).
        self.dragging = True
        frac = min(1.0, max(0.0, event.x / max(1, self.seek.winfo_width())))
        self.seek_var.set(frac)
        t = frac * self.duration
        self.time_label.configure(text=f"{fmt_time(t)} / {fmt_time(self.duration)}")
        return "break"

    def on_seek(self, event):
        self.on_seek_drag(event)
        self.dragging = False
        if self.player and self.duration:
            self.player.seek_to = self.seek_var.get() * self.duration
        return "break"

    def tick(self):
        while self.ui_calls:
            self.ui_calls.pop(0)()
        if self.latest is not None:
            bits, view, t, dur = self.latest
            self.latest = None
            self.show_bits(bits)
            if view is not self.last_view:
                self.show_video(view)
                self.last_view = view
            self.duration = dur
            if not dur:
                self.seek_var.set(0)
                self.time_label.configure(text="LIVE")
            elif not self.dragging:
                self.seek_var.set(t / dur)
                self.time_label.configure(text=f"{fmt_time(t)} / {fmt_time(dur)}")
        if not self.player and self.play_btn.cget("text") != "Play":
            self.play_btn.configure(text="Play")
        self.status_label.configure(text=self.status)
        self.root.after(15, self.tick)

    def quit(self):
        self.stop()
        if self.board:
            self.board.close()
        pygame.mixer.quit()
        try:
            os.remove(self.audio_file)
        except OSError:
            pass
        shutil.rmtree(self.download_dir, ignore_errors=True)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
