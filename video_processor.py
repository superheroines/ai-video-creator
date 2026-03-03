#!/usr/bin/env python3
"""
Video Batch Processor
─────────────────────
A simple desktop app that processes raw videos by:
  1. Adding a logo watermark (bottom-left)
  2. Extracting 24 evenly-spaced thumbnail snapshots + contact sheet
  3. Exporting with sequential naming (Video-001, Video-002, …)

Requirements:
    Python 3.8+
    FFmpeg  –  macOS:  brew install ffmpeg
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess
import sys
import os
import json
import math
import shutil
import threading
import logging
import re
from datetime import datetime
from pathlib import Path

# ── Constants ───────────────────────────────────────────────────────────────

CONFIG_FILE = os.path.join(Path.home(), ".video_processor_config.json")
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv",
    ".flv", ".webm", ".m4v", ".mts", ".ts",
}
THUMBNAIL_COUNT = 24


# ── FFmpeg discovery ────────────────────────────────────────────────────────

def _find_tool(name: str) -> str:
    """
    Find an FFmpeg tool binary. Priority:
    1. Bundled inside .app (same dir as executable)
    2. System PATH
    3. Common Homebrew locations
    """
    if getattr(sys, "frozen", False):
        app_dir = os.path.dirname(sys.executable)
        bundled = os.path.join(app_dir, name)
        if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
            return bundled

    found = shutil.which(name)
    if found:
        return found

    for path in [f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"]:
        if os.path.isfile(path):
            return path

    return name  # fall back — subprocess will raise a clear error


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


# ── Utility helpers ─────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def check_ffmpeg() -> bool:
    """Return True if ffmpeg and ffprobe are reachable."""
    for tool in (FFMPEG, FFPROBE):
        try:
            subprocess.run([tool, "-version"], capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    return True


def probe(filepath: str) -> tuple[int, int, float, float]:
    """Return (width, height, duration_seconds, fps) via ffprobe."""
    cmd = [
        FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration,r_frame_rate",
        "-show_entries", "format=duration",
        "-of", "json",
        str(filepath),
    ]
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        stderr = r.stderr.strip() if r.stderr else "no output"
        raise RuntimeError(f"ffprobe failed: {stderr}")
    data = json.loads(r.stdout)
    s = data.get("streams", [{}])[0]
    w = int(s.get("width", 0))
    h = int(s.get("height", 0))
    dur = float(s.get("duration", 0) or data.get("format", {}).get("duration", 0))
    fps_raw = s.get("r_frame_rate", "0/1")
    try:
        num, den = fps_raw.split("/")
        fps = float(num) / float(den) if float(den) != 0 else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return w, h, dur, fps


# ── Safety checks ──────────────────────────────────────────────────────────

def check_black_start(filepath: str, scan_duration: float = 2.0) -> float | None:
    """Detect if *filepath* starts with black frames.

    Returns the end timestamp (seconds) of the initial black period,
    or None if the video does not start with black.

    If black is detected, extracts the first visible frame as a PNG
    at ``{filepath}.first_frame.png``.
    """
    cmd = [
        FFMPEG,
        "-i", str(filepath),
        "-t", str(scan_duration),
        "-vf", "blackdetect=d=0.05:pix_th=0.10",
        "-an", "-f", "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                       errors="replace", timeout=30)

    black_end = None
    for line in (r.stderr or "").splitlines():
        if "black_start:0" in line or "black_start: 0" in line:
            match = re.search(r"black_end[:\s]+([\d.]+)", line)
            if match:
                black_end = float(match.group(1))
                break

    if black_end is None or black_end <= 0:
        return None

    # Extract first visible frame
    seek_to = black_end + 0.05
    first_frame_path = str(filepath) + ".first_frame.png"
    r2 = subprocess.run(
        [
            FFMPEG, "-y",
            "-ss", f"{seek_to:.3f}",
            "-i", str(filepath),
            "-vframes", "1",
            "-q:v", "1",
            str(first_frame_path),
        ],
        capture_output=True, encoding="utf-8", errors="replace", timeout=15,
    )
    if r2.returncode != 0 or not os.path.exists(first_frame_path):
        return None

    return black_end


def validate_export(filepath: str) -> dict[str, bool]:
    """Validate that *filepath* meets the Bunny Stream target spec.

    Returns a dict of check_name -> pass/fail boolean.
    """
    cmd = [
        FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,profile,pix_fmt,r_frame_rate,width,height,"
        "color_primaries,color_transfer,color_space",
        "-show_entries", "format=format_name",
        "-of", "json",
        str(filepath),
    ]
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                       errors="replace", timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        return {"probe_readable": False}

    data = json.loads(r.stdout)
    s = data.get("streams", [{}])[0]
    fmt = data.get("format", {})

    results: dict[str, bool] = {}

    results["codec_h264"] = s.get("codec_name") == "h264"
    results["profile_high"] = "high" in (s.get("profile") or "").lower()
    results["pix_fmt_yuv420p"] = s.get("pix_fmt") == "yuv420p"

    w = int(s.get("width", 0))
    h = int(s.get("height", 0))
    results["even_dimensions"] = (w % 2 == 0) and (h % 2 == 0) and w > 0

    results["colour_bt709"] = s.get("color_primaries") == "bt709"

    fps_raw = s.get("r_frame_rate", "0/1")
    try:
        num, den = fps_raw.split("/")
        fps = float(num) / float(den) if float(den) != 0 else 0
    except (ValueError, ZeroDivisionError):
        fps = 0
    results["fps_sensible"] = 15 <= fps <= 120

    fmt_name = fmt.get("format_name", "")
    results["container_mp4"] = "mp4" in fmt_name or "mov" in fmt_name

    failures = [k for k, v in results.items() if not v]
    if failures:
        logger = logging.getLogger("video_processor")
        for f in failures:
            logger.warning("Export validation FAIL: %s (%s)", f, filepath)

    return results


# ── Core processing ─────────────────────────────────────────────────────────

def watermark(src: str, logo: str, dst: str, logo_pct: int = 10,
              padding: int = 20, black_end: float | None = None) -> None:
    """Overlay *logo* on *src* video; write Bunny Stream-safe MP4 to *dst*.

    Produces H.264 High Profile 4.1, yuv420p, Rec.709, CFR, with
    strict keyframe control and AAC audio re-encode.

    If *black_end* is set, composites the first visible frame over
    the black leader so frame 0 is not pure black.
    """
    w, h, _dur, fps = probe(src)
    if w == 0:
        raise RuntimeError("Cannot read video dimensions")
    if fps <= 0:
        fps = 30.0

    gop = max(int(round(fps * 2)), 1)
    logo_w = max(int(w * logo_pct / 100), 20)

    # Common tail: even-pad, yuv420p, CFR, Rec.709 tags
    tail = (
        f"pad=ceil(iw/2)*2:ceil(ih/2)*2,"
        f"format=yuv420p,"
        f"fps={fps},"
        f"setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709"
    )

    if black_end is not None and black_end > 0:
        first_frame = str(src) + ".first_frame.png"
        filt = (
            f"[2:v]scale={w}:{h}:flags=lanczos[fill];"
            f"[0:v][fill]overlay=0:0:enable='lt(t,{black_end:.3f})'[base];"
            f"[1:v]scale={logo_w}:-1:flags=lanczos,format=rgba[logo];"
            f"[base][logo]overlay={padding}:main_h-overlay_h-{padding},"
            f"{tail}[out]"
        )
    else:
        first_frame = None
        filt = (
            f"[1:v]scale={logo_w}:-1:flags=lanczos,format=rgba[logo];"
            f"[0:v][logo]overlay={padding}:main_h-overlay_h-{padding},"
            f"{tail}[out]"
        )

    cmd = [FFMPEG, "-y", "-i", str(src), "-i", str(logo)]
    if first_frame:
        cmd += ["-i", first_frame]

    cmd += [
        "-filter_complex", filt,
        "-map", "[out]", "-map", "0:a?",
        # Video codec
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-profile:v", "high",
        "-level:v", "4.1",
        "-pix_fmt", "yuv420p",
        # Keyframe control
        "-g", str(gop),
        "-keyint_min", str(gop),
        "-sc_threshold", "0",
        "-force_key_frames", "0,0.5",
        # Audio re-encode
        "-c:a", "aac",
        "-b:a", "192k",
        "-ac", "2",
        "-ar", "48000",
        # Container
        "-movflags", "+faststart",
        # Timestamps
        "-avoid_negative_ts", "make_zero",
        "-fflags", "+genpts",
        # Metadata
        "-map_metadata", "-1",
        # CFR enforcement
        "-vsync", "cfr",
        str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                       errors="replace", timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])

    # Clean up temp first-frame PNG
    if first_frame:
        try:
            os.remove(first_frame)
        except OSError:
            pass


def snapshots(video: str, out_dir: str, prefix: str,
              count: int = THUMBNAIL_COUNT) -> None:
    """
    Extract *count* evenly-spaced PNG snapshots from *video*.
    Also creates a 6x4 contact-sheet JPEG.
    Files are named {prefix}-snapshot-01.png, etc.
    """
    os.makedirs(out_dir, exist_ok=True)
    _w, _h, dur, _fps = probe(video)
    if dur <= 0:
        raise RuntimeError("Cannot determine video duration")

    ok_count = 0
    for i in range(count):
        t = (i / max(count - 1, 1)) * dur
        out = os.path.join(out_dir, f"{prefix}-snapshot-{i + 1:02d}.png")
        r = subprocess.run(
            [
                FFMPEG, "-y",
                "-ss", f"{t:.3f}",
                "-i", str(video),
                "-vframes", "1",
                "-q:v", "2",
                str(out),
            ],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if r.returncode == 0 and os.path.exists(out):
            ok_count += 1

    if ok_count >= 2:
        sheet_in = os.path.join(out_dir, f"{prefix}-snapshot-%02d.png")
        sheet_out = os.path.join(out_dir, f"{prefix}-contact-sheet.jpg")
        cols = 6
        rows = math.ceil(count / cols)
        subprocess.run(
            [
                FFMPEG, "-y",
                "-start_number", "1",
                "-i", sheet_in,
                "-vf", f"scale=320:-1,tile={cols}x{rows}",
                "-q:v", "3",
                str(sheet_out),
            ],
            capture_output=True, encoding="utf-8", errors="replace", timeout=60,
        )


def detect_next_seq(folder: str, prefix: str) -> int | None:
    """Scan *folder* for existing Video-NNN dirs; return next number."""
    if not folder or not os.path.isdir(folder):
        return None
    hi = 0
    for name in os.listdir(folder):
        if os.path.isdir(os.path.join(folder, name)) and name.startswith(prefix):
            try:
                hi = max(hi, int(name[len(prefix):]))
            except ValueError:
                pass
    return hi + 1 if hi else None


# ── Ledger ─────────────────────────────────────────────────────────────────

LEDGER_FILE = "ledger.json"


def load_ledger(output_folder: str, prefix: str) -> dict:
    """Load ledger.json from the output folder.
    If missing, seeds 'next' from existing folders (migration) or defaults to 1."""
    path = os.path.join(output_folder, LEDGER_FILE)
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # First-run migration: detect from existing folders
        nxt = detect_next_seq(output_folder, prefix)
        return {"next": nxt or 1, "videos": []}


def save_ledger(output_folder: str, ledger: dict) -> None:
    """Write ledger.json to the output folder."""
    path = os.path.join(output_folder, LEDGER_FILE)
    with open(path, "w") as f:
        json.dump(ledger, f, indent=2)


# ── GUI ─────────────────────────────────────────────────────────────────────

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Video Batch Processor")
        self.root.geometry("700x620")
        self.root.resizable(False, False)
        self.processing = False
        self.cfg = load_config()
        self._build_ui()
        self._restore()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _ui(self, *, status: str | None = None, log: str | None = None,
            progress: float | None = None) -> None:
        """Thread-safe UI update."""
        def _do() -> None:
            if status is not None:
                self.status.set(status)
            if log is not None:
                self._append(log)
            if progress is not None:
                self.pbar["value"] = progress
        self.root.after(0, _do)

    def _append(self, msg: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    # ── build UI ────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        m = ttk.Frame(self.root, padding=16)
        m.pack(fill="both", expand=True)

        ttk.Label(m, text="Video Batch Processor",
                  font=("Helvetica", 16, "bold")).pack(pady=(0, 12))

        # ── Files & Folders ──
        ff = ttk.LabelFrame(m, text="Files & Folders", padding=8)
        ff.pack(fill="x", padx=6, pady=4)

        self.inp_var = self._dir_row(ff, "Input Folder:")
        self.out_var = self._dir_row(ff, "Output Folder:",
                                     on_change=self._refresh_ledger_label)
        self.logo_var = self._file_row(
            ff, "Logo File:",
            [("Images", "*.png *.jpg *.jpeg *.webp *.svg"), ("All", "*.*")],
        )

        # ── Settings ──
        sf = ttk.LabelFrame(m, text="Settings", padding=8)
        sf.pack(fill="x", padx=6, pady=4)

        r1 = ttk.Frame(sf)
        r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="Prefix:", width=14, anchor="e").pack(side="left")
        self.prefix_var = tk.StringVar(value="Video-")
        ttk.Entry(r1, textvariable=self.prefix_var, width=14).pack(side="left", padx=5)
        self.ledger_lbl = ttk.Label(r1, text="", foreground="gray")
        self.ledger_lbl.pack(side="left", padx=(20, 0))

        r2 = ttk.Frame(sf)
        r2.pack(fill="x", pady=2)
        ttk.Label(r2, text="Logo Size:", width=14, anchor="e").pack(side="left")
        self.pct_var = tk.IntVar(value=10)
        ttk.Scale(r2, from_=3, to=30, variable=self.pct_var,
                  orient="horizontal", length=160).pack(side="left", padx=5)
        self.pct_lbl = ttk.Label(r2, text="10 %")
        self.pct_lbl.pack(side="left")
        self.pct_var.trace_add("write",
            lambda *_: self.pct_lbl.config(text=f"{self.pct_var.get()} %"))

        r3 = ttk.Frame(sf)
        r3.pack(fill="x", pady=2)
        ttk.Label(r3, text="Edge Padding:", width=14, anchor="e").pack(side="left")
        self.pad_var = tk.StringVar(value="20")
        ttk.Entry(r3, textvariable=self.pad_var, width=6).pack(side="left", padx=5)
        ttk.Label(r3, text="px").pack(side="left")

        # ── Progress ──
        pf = ttk.LabelFrame(m, text="Progress", padding=8)
        pf.pack(fill="x", padx=6, pady=4)

        self.pbar = ttk.Progressbar(pf, maximum=100)
        self.pbar.pack(fill="x", pady=(0, 4))
        self.status = tk.StringVar(value="Ready")
        ttk.Label(pf, textvariable=self.status).pack(anchor="w")
        self.log = tk.Text(pf, height=8, font=("Courier", 9),
                           state="disabled", bg="#f5f5f5")
        self.log.pack(fill="x", pady=(4, 0))

        # ── Buttons ──
        bf = ttk.Frame(m)
        bf.pack(fill="x", pady=(8, 0))
        self.go_btn = ttk.Button(bf, text="  Process Videos  ",
                                 command=self._start)
        self.go_btn.pack(side="right", padx=4)
        self.stop_btn = ttk.Button(bf, text="Cancel",
                                   command=self._cancel, state="disabled")
        self.stop_btn.pack(side="right")

    # ── row builders ────────────────────────────────────────────────────────

    def _dir_row(self, parent: ttk.Frame, label: str,
                 on_change: object = None) -> tk.StringVar:
        r = ttk.Frame(parent)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text=label, width=14, anchor="e").pack(side="left")
        var = tk.StringVar()
        ttk.Entry(r, textvariable=var, width=48).pack(side="left", padx=5)
        def pick() -> None:
            d = filedialog.askdirectory()
            if d:
                var.set(d)
                if on_change:
                    on_change()
        ttk.Button(r, text="Browse", command=pick).pack(side="left")
        return var

    def _file_row(self, parent: ttk.Frame, label: str,
                  ftypes: list) -> tk.StringVar:
        r = ttk.Frame(parent)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text=label, width=14, anchor="e").pack(side="left")
        var = tk.StringVar()
        ttk.Entry(r, textvariable=var, width=48).pack(side="left", padx=5)
        def pick() -> None:
            f = filedialog.askopenfilename(filetypes=ftypes)
            if f:
                var.set(f)
        ttk.Button(r, text="Browse", command=pick).pack(side="left")
        return var

    # ── config persistence ──────────────────────────────────────────────────

    def _restore(self) -> None:
        c = self.cfg
        for key, var in [("input", self.inp_var), ("output", self.out_var),
                         ("logo", self.logo_var), ("prefix", self.prefix_var),
                         ("padding", self.pad_var)]:
            if key in c:
                var.set(str(c[key]))
        if "pct" in c:
            self.pct_var.set(c["pct"])
        self._refresh_ledger_label()

    def _persist(self) -> None:
        save_config({
            "input": self.inp_var.get(), "output": self.out_var.get(),
            "logo": self.logo_var.get(), "prefix": self.prefix_var.get(),
            "padding": self.pad_var.get(), "pct": self.pct_var.get(),
        })

    # ── ledger status ────────────────────────────────────────────────────────

    def _refresh_ledger_label(self) -> None:
        out = self.out_var.get().strip()
        prefix = self.prefix_var.get()
        if out and os.path.isdir(out):
            ledger = load_ledger(out, prefix)
            nxt = ledger["next"]
            count = len(ledger["videos"])
            self.ledger_lbl.config(
                text=f"Next: {prefix}{nxt:03d}  ({count} in ledger)")
        else:
            self.ledger_lbl.config(text="")

    # ── processing ──────────────────────────────────────────────────────────

    def _start(self) -> None:
        inp = self.inp_var.get().strip()
        out = self.out_var.get().strip()
        logo = self.logo_var.get().strip()

        if not inp or not os.path.isdir(inp):
            messagebox.showerror("Error", "Select a valid input folder.")
            return
        if not out:
            messagebox.showerror("Error", "Select an output folder.")
            return
        if not logo or not os.path.isfile(logo):
            messagebox.showerror("Error",
                "Select a valid logo image.\n(PNG with transparency recommended)")
            return
        if not check_ffmpeg():
            messagebox.showerror(
                "FFmpeg not found",
                "Install FFmpeg and make sure it's on your PATH.\n\n"
                "macOS:  brew install ffmpeg")
            return

        videos = sorted(
            f for f in os.listdir(inp)
            if Path(f).suffix.lower() in VIDEO_EXTENSIONS
        )
        if not videos:
            messagebox.showinfo("Nothing to do",
                                "No video files found in the input folder.")
            return

        self._persist()
        self.processing = True
        self.go_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self.pbar["value"] = 0

        threading.Thread(
            target=self._run,
            args=(inp, out, logo, videos),
            daemon=True,
        ).start()

    def _run(self, inp: str, out: str, logo: str,
             videos: list[str]) -> None:
        prefix = self.prefix_var.get()
        pct = self.pct_var.get()
        pad = int(self.pad_var.get() or 20)
        total = len(videos)

        os.makedirs(out, exist_ok=True)
        ledger = load_ledger(out, prefix)
        self._ui(log=f"Found {total} video(s) to process")
        self._ui(log=f"Ledger: starting at {prefix}{ledger['next']:03d}\n")

        completed = 0
        for i, fname in enumerate(videos):
            if not self.processing:
                self._ui(log="— Cancelled —")
                break

            num = ledger["next"]
            seq = f"{prefix}{num:03d}"
            src = os.path.join(inp, fname)
            vdir = os.path.join(out, seq)
            os.makedirs(vdir, exist_ok=True)
            dst = os.path.join(vdir, f"{seq}.mp4")

            self._ui(
                status=f"Processing {seq}  ({i + 1}/{total})",
                log=f"[{i + 1}/{total}]  {seq}  ←  {fname}",
            )

            try:
                # Black frame detection
                self._ui(log="        checking for black start…")
                black_end = check_black_start(src)
                if black_end is not None:
                    self._ui(log=f"        black detected: {black_end:.2f}s — will replace")

                # Watermark + encode (broadcast-safe)
                self._ui(log="        adding watermark…")
                watermark(src, logo, dst, pct, pad, black_end=black_end)

                # Validate export
                self._ui(log="        validating export…")
                checks = validate_export(dst)
                failures = [k for k, v in checks.items() if not v]
                if failures:
                    self._ui(log=f"        ⚠ validation: {', '.join(failures)}")
                else:
                    self._ui(log="        export validation passed")

                # Extract thumbnails
                self._ui(log="        extracting thumbnails…")
                snapshots(dst, vdir, seq, THUMBNAIL_COUNT)

                # Move raw video into the output folder
                raw_ext = Path(fname).suffix
                raw_dst = os.path.join(vdir, f"{seq}-raw{raw_ext}")
                shutil.move(src, raw_dst)
                self._ui(log="        moved raw video")

                # Update ledger
                ledger["videos"].append({
                    "seq": seq,
                    "original": fname,
                    "processed": datetime.now().isoformat(timespec="seconds"),
                    "validation": checks,
                })
                ledger["next"] = num + 1
                save_ledger(out, ledger)

                self._ui(log=f"        ✓ {seq} complete\n")
                completed += 1
            except Exception as exc:
                self._ui(log=f"        ✗ ERROR: {exc}\n")

            self._ui(progress=(i + 1) / total * 100)

        self._ui(log=f"Done — {completed}/{total} videos processed successfully.")
        self.root.after(0, lambda c=completed: self._done(c))

    def _done(self, completed: int) -> None:
        self.processing = False
        self.go_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status.set(f"Complete ✓  ({completed} videos)")
        self._refresh_ledger_label()

    def _cancel(self) -> None:
        self.processing = False
        self.status.set("Cancelling…")


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    style = ttk.Style()
    if "clam" in style.theme_names():
        style.theme_use("clam")

    if not check_ffmpeg():
        messagebox.showerror(
            "FFmpeg Not Found",
            "FFmpeg is required but was not found.\n\n"
            "Install it with:\n  brew install ffmpeg\n\n"
            "Then restart the application.")
        sys.exit(1)

    App(root)
    root.mainloop()
