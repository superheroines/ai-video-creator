#!/usr/bin/env python3
"""
Video Batch Processor
─────────────────────
A simple desktop app that processes raw videos by:
  1. Adding a logo watermark (bottom-left)
  2. Extracting 24 evenly-spaced thumbnail snapshots + contact sheet
  3. Exporting with auto-generated names: <shape>-<class>-<timestamp>-<NNN>.mp4

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
import tempfile
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


def detect_aspect_ratio(w: int, h: int) -> str:
    """Return the common aspect ratio as 'WxH' string (e.g. '16x9', '9x16', '1x1').

    Snaps to the nearest common ratio if within tolerance, otherwise
    uses the simplified ratio from GCD.
    """
    if w <= 0 or h <= 0:
        return "0x0"

    common = [
        (16, 9), (9, 16),
        (4, 3), (3, 4),
        (4, 5), (5, 4),
        (1, 1),
        (21, 9), (9, 21),
        (3, 2), (2, 3),
    ]
    ratio = w / h
    for cw, ch in common:
        if abs(ratio - cw / ch) < 0.02:
            return f"{cw}x{ch}"

    # Fall back to simplified ratio via GCD
    g = math.gcd(w, h)
    rw, rh = w // g, h // g
    # Cap very large ratios (e.g. 683x384 from odd resolutions)
    if rw > 99 or rh > 99:
        # Approximate to nearest integer ratio
        if w >= h:
            rh_approx = round(h * 16 / w)
            return f"16x{rh_approx}" if rh_approx > 0 else f"{rw}x{rh}"
        else:
            rw_approx = round(w * 16 / h)
            return f"{rw_approx}x16" if rw_approx > 0 else f"{rw}x{rh}"
    return f"{rw}x{rh}"


def scan_input_videos(inp: str) -> list[tuple[str, str, str]]:
    """Scan input folder for videos, supporting optional sfw/nsfw subfolders.

    Returns list of (filename, full_path, class_code) tuples.
    class_code is 's' for sfw subfolder, 'a' for nsfw subfolder,
    or None for files in the root (caller supplies default).
    """
    results = []
    sfw_dir = os.path.join(inp, "sfw")
    nsfw_dir = os.path.join(inp, "nsfw")
    has_subfolders = os.path.isdir(sfw_dir) or os.path.isdir(nsfw_dir)

    if has_subfolders:
        for subdir, cls in [(sfw_dir, "s"), (nsfw_dir, "a")]:
            if os.path.isdir(subdir):
                for f in sorted(os.listdir(subdir)):
                    if Path(f).suffix.lower() in VIDEO_EXTENSIONS:
                        results.append((f, os.path.join(subdir, f), cls))
    else:
        for f in sorted(os.listdir(inp)):
            if Path(f).suffix.lower() in VIDEO_EXTENSIONS:
                results.append((f, os.path.join(inp, f), None))

    return results


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
              padding: int = 20, black_end: float | None = None,
              on_progress: object = None) -> None:
    """Overlay *logo* on *src* video; write Bunny Stream-safe MP4 to *dst*.

    Produces H.264 High Profile 4.1, yuv420p, Rec.709, CFR, with
    strict keyframe control and AAC audio re-encode.

    If *black_end* is set, composites the first visible frame over
    the black leader so frame 0 is not pure black.

    If *on_progress* is a callable, it is called with a float 0.0–1.0
    representing encoding progress.
    """
    w, h, dur, fps = probe(src)
    if w == 0:
        raise RuntimeError("Cannot read video dimensions")
    if fps <= 0:
        fps = 30.0

    gop = max(int(round(fps * 2)), 1)
    logo_w = max(int(w * logo_pct / 100), 20)

    # Common tail: even-pad, yuv420p, CFR, Rec.709 tags, reset PTS to zero
    tail = (
        f"pad=ceil(iw/2)*2:ceil(ih/2)*2,"
        f"format=yuv420p,"
        f"fps={fps},"
        f"setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709,"
        f"setpts=PTS-STARTPTS"
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
        # Reset audio timestamps to zero (matches video setpts)
        "-af", "asetpts=PTS-STARTPTS",
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
        "-movflags", "+faststart+negative_cts_offsets",
        # Timestamps
        "-avoid_negative_ts", "make_zero",
        "-fflags", "+genpts",
        # Metadata
        "-map_metadata", "-1",
        # CFR enforcement (fps_mode replaces deprecated -vsync)
        "-fps_mode", "cfr",
        str(dst),
    ]
    time_re = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    stderr_buf = []
    buf = ""
    for char in iter(lambda: proc.stderr.read(1), ""):
        if char in ("\r", "\n"):
            if buf.strip():
                stderr_buf.append(buf)
                if on_progress and dur > 0:
                    match = time_re.search(buf)
                    if match:
                        hh, mm, ss = match.groups()
                        elapsed = int(hh) * 3600 + int(mm) * 60 + float(ss)
                        on_progress(min(elapsed / dur, 1.0))
            buf = ""
        else:
            buf += char
    proc.wait()
    if proc.returncode != 0:
        err_text = "\n".join(stderr_buf[-20:])
        raise RuntimeError(err_text[-800:])

    # Clean up temp first-frame PNG
    if first_frame:
        try:
            os.remove(first_frame)
        except OSError:
            pass


def watermark_4x5(src: str, logo: str, dst: str, logo_pct: int = 10,
                  padding: int = 20, on_progress: object = None) -> None:
    """Centre-crop *src* to 4:5, overlay *logo*, write Bunny-safe MP4 to *dst*.

    Uses the same encoding settings as watermark() but crops the video
    to the largest 4:5 rectangle centred in the frame, with the logo
    sized independently for the cropped dimensions.
    """
    w, h, dur, fps = probe(src)
    if w == 0:
        raise RuntimeError("Cannot read video dimensions")
    if fps <= 0:
        fps = 30.0

    # Calculate 4:5 centre crop
    if w / h > 4 / 5:
        crop_h = h
        crop_w = int(h * 4 / 5)
    else:
        crop_w = w
        crop_h = int(w * 5 / 4)
    crop_w = crop_w - (crop_w % 2)
    crop_h = crop_h - (crop_h % 2)

    gop = max(int(round(fps * 2)), 1)
    logo_w = max(int(crop_w * logo_pct / 100), 20)

    tail = (
        f"pad=ceil(iw/2)*2:ceil(ih/2)*2,"
        f"format=yuv420p,"
        f"fps={fps},"
        f"setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709,"
        f"setpts=PTS-STARTPTS"
    )

    filt = (
        f"[0:v]crop={crop_w}:{crop_h}[cropped];"
        f"[1:v]scale={logo_w}:-1:flags=lanczos,format=rgba[logo];"
        f"[cropped][logo]overlay={padding}:main_h-overlay_h-{padding},"
        f"{tail}[out]"
    )

    cmd = [
        FFMPEG, "-y", "-i", str(src), "-i", str(logo),
        "-filter_complex", filt,
        "-map", "[out]", "-map", "0:a?",
        "-af", "asetpts=PTS-STARTPTS",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-profile:v", "high",
        "-level:v", "4.1",
        "-pix_fmt", "yuv420p",
        "-g", str(gop),
        "-keyint_min", str(gop),
        "-sc_threshold", "0",
        "-force_key_frames", "0,0.5",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ac", "2",
        "-ar", "48000",
        "-movflags", "+faststart+negative_cts_offsets",
        "-avoid_negative_ts", "make_zero",
        "-fflags", "+genpts",
        "-map_metadata", "-1",
        "-fps_mode", "cfr",
        str(dst),
    ]
    time_re = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    stderr_buf = []
    buf = ""
    for char in iter(lambda: proc.stderr.read(1), ""):
        if char in ("\r", "\n"):
            if buf.strip():
                stderr_buf.append(buf)
                if on_progress and dur > 0:
                    match = time_re.search(buf)
                    if match:
                        hh, mm, ss = match.groups()
                        elapsed = int(hh) * 3600 + int(mm) * 60 + float(ss)
                        on_progress(min(elapsed / dur, 1.0))
            buf = ""
        else:
            buf += char
    proc.wait()
    if proc.returncode != 0:
        err_text = "\n".join(stderr_buf[-20:])
        raise RuntimeError(err_text[-800:])


def watermark_preview_frame(src: str, logo: str, logo_pct: int = 10,
                            padding: int = 20) -> str | None:
    """Extract one frame from *src*, overlay *logo*, return path to temp PNG.

    Returns None on failure. Caller is responsible for deleting the temp file.
    """
    w, h, dur, fps = probe(src)
    if w == 0:
        return None
    if fps <= 0:
        fps = 30.0

    seek = min(dur * 0.10, 5.0) if dur > 0 else 0
    logo_w = max(int(w * logo_pct / 100), 20)

    # Preview at max 960px wide for display
    filt = (
        f"[1:v]scale={logo_w}:-1:flags=lanczos,format=rgba[logo];"
        f"[0:v][logo]overlay={padding}:main_h-overlay_h-{padding},"
        f"scale=960:-2:flags=lanczos,format=rgb24[out]"
    )

    tmp = tempfile.mktemp(suffix=".png", prefix="wm_preview_")
    cmd = [
        FFMPEG, "-y",
        "-ss", f"{seek:.3f}",
        "-i", str(src),
        "-i", str(logo),
        "-filter_complex", filt,
        "-map", "[out]",
        "-vframes", "1",
        str(tmp),
    ]
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                       errors="replace", timeout=30)
    if r.returncode != 0 or not os.path.exists(tmp):
        return None
    return tmp


def snapshots(video: str, out_dir: str, prefix: str,
              count: int = THUMBNAIL_COUNT,
              raw_video: str | None = None,
              logo: str | None = None,
              logo_pct: int = 10, padding: int = 20,
              prefix_4x5: str | None = None) -> None:
    """
    Extract *count* evenly-spaced PNG snapshots from *video*.
    Also creates:
      - a 6x4 contact-sheet JPEG (native aspect ratio)
      - 4:5 centre-cropped versions with independent logo overlay
    Native snapshots come from *video* (already watermarked).
    4:5 crops come from *raw_video* (no watermark), centre-cropped,
    with the logo overlaid at a size appropriate for the 4:5 frame.
    """
    os.makedirs(out_dir, exist_ok=True)
    w, h, dur, _fps = probe(video)
    if dur <= 0:
        raise RuntimeError("Cannot determine video duration")

    # Calculate the 4:5 centre crop dimensions
    if w / h > 4 / 5:
        crop_h = h
        crop_w = int(h * 4 / 5)
    else:
        crop_w = w
        crop_h = int(w * 5 / 4)
    crop_w = crop_w - (crop_w % 2)
    crop_h = crop_h - (crop_h % 2)

    # For 4:5 crops: use raw video + logo overlay if available
    pfx_4x5 = prefix_4x5 or f"{prefix}-4x5"
    do_4x5 = raw_video and logo and os.path.isfile(raw_video) and os.path.isfile(logo)
    if do_4x5:
        logo_w_4x5 = max(int(crop_w * logo_pct / 100), 20)
        crop_filt = (
            f"[0:v]crop={crop_w}:{crop_h}[cropped];"
            f"[1:v]scale={logo_w_4x5}:-1:flags=lanczos,format=rgba[logo];"
            f"[cropped][logo]overlay={padding}:main_h-overlay_h-{padding}[out]"
        )

    ok_count = 0
    ok_4x5 = 0
    for i in range(count):
        t = (i / max(count - 1, 1)) * dur
        native_out = os.path.join(out_dir, f"{prefix}-snapshot-{i + 1:02d}.png")
        crop_out = os.path.join(out_dir, f"{pfx_4x5}-snapshot-{i + 1:02d}.png")

        # Native snapshot from watermarked video
        r = subprocess.run(
            [
                FFMPEG, "-y",
                "-ss", f"{t:.3f}",
                "-i", str(video),
                "-vframes", "1",
                "-q:v", "2",
                str(native_out),
            ],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if r.returncode == 0 and os.path.exists(native_out):
            ok_count += 1

        # 4:5 crop from raw video with independent logo overlay
        if do_4x5:
            r2 = subprocess.run(
                [
                    FFMPEG, "-y",
                    "-ss", f"{t:.3f}",
                    "-i", str(raw_video),
                    "-i", str(logo),
                    "-filter_complex", crop_filt,
                    "-map", "[out]",
                    "-vframes", "1",
                    "-q:v", "2",
                    str(crop_out),
                ],
                capture_output=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if r2.returncode == 0 and os.path.exists(crop_out):
                ok_4x5 += 1

    if ok_count >= 2:
        # Native contact sheet
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

    if ok_4x5 >= 2:
        # 4:5 contact sheet
        sheet_4x5_in = os.path.join(out_dir, f"{pfx_4x5}-snapshot-%02d.png")
        sheet_4x5_out = os.path.join(out_dir, f"{pfx_4x5}-contact-sheet.jpg")
        cols = 6
        rows = math.ceil(count / cols)
        subprocess.run(
            [
                FFMPEG, "-y",
                "-start_number", "1",
                "-i", sheet_4x5_in,
                "-vf", f"scale=256:-1,tile={cols}x{rows}",
                "-q:v", "3",
                str(sheet_4x5_out),
            ],
            capture_output=True, encoding="utf-8", errors="replace", timeout=60,
        )


# ── Ledger ─────────────────────────────────────────────────────────────────

LEDGER_FILE = "ledger.json"


def load_ledger(output_folder: str) -> dict:
    """Load ledger.json from the output folder."""
    path = os.path.join(output_folder, LEDGER_FILE)
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"videos": []}


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
        self.root.geometry("750x780")
        self.root.minsize(700, 620)
        self.root.resizable(True, True)
        self.processing = False
        self._failed_videos: list[tuple[str, str, str]] = []  # (fname, path, cls)
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
        ttk.Label(r1, text="Default Class:", width=14, anchor="e").pack(side="left")
        self.class_var = tk.StringVar(value="adult")
        ttk.OptionMenu(r1, self.class_var, "adult", "adult", "safe").pack(
            side="left", padx=5)
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
        ttk.Button(r2, text="Preview",
                   command=self._preview_watermark).pack(side="left", padx=(15, 0))

        r3 = ttk.Frame(sf)
        r3.pack(fill="x", pady=2)
        ttk.Label(r3, text="Edge Padding:", width=14, anchor="e").pack(side="left")
        self.pad_var = tk.StringVar(value="20")
        ttk.Entry(r3, textvariable=self.pad_var, width=6).pack(side="left", padx=5)
        ttk.Label(r3, text="px").pack(side="left")

        # ── Progress ──
        pf = ttk.LabelFrame(m, text="Progress", padding=8)
        pf.pack(fill="both", expand=True, padx=6, pady=4)

        self.pbar = ttk.Progressbar(pf, maximum=100)
        self.pbar.pack(fill="x", pady=(0, 4))
        self.status = tk.StringVar(value="Ready")
        ttk.Label(pf, textvariable=self.status).pack(anchor="w")
        self.log = tk.Text(pf, height=10, font=("Courier", 9),
                           state="disabled", bg="#f5f5f5", fg="#1a1a1a")
        self.log.pack(fill="both", expand=True, pady=(4, 0))

        # ── Buttons ──
        bf = ttk.Frame(m)
        bf.pack(fill="x", pady=(8, 0))

        # Right side — processing buttons
        self.go_btn = ttk.Button(bf, text="  Process Videos  ",
                                 command=self._start)
        self.go_btn.pack(side="right", padx=4)
        self.stop_btn = ttk.Button(bf, text="Cancel",
                                   command=self._cancel, state="disabled")
        self.stop_btn.pack(side="right")
        self.retry_btn = ttk.Button(bf, text="Retry Failed",
                                    command=self._retry_failed)
        # retry_btn is not packed until needed

        # Left side — utility buttons
        ttk.Button(bf, text="Open Output",
                   command=self._open_output).pack(side="left", padx=4)
        ttk.Button(bf, text="View Ledger",
                   command=self._view_ledger).pack(side="left", padx=4)
        ttk.Button(bf, text="Reset Ledger",
                   command=self._reset_ledger).pack(side="left", padx=4)

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
                         ("logo", self.logo_var), ("padding", self.pad_var)]:
            if key in c:
                var.set(str(c[key]))
        if "pct" in c:
            self.pct_var.set(c["pct"])
        if "default_class" in c:
            self.class_var.set(c["default_class"])
        self._refresh_ledger_label()

    def _persist(self) -> None:
        save_config({
            "input": self.inp_var.get(), "output": self.out_var.get(),
            "logo": self.logo_var.get(), "default_class": self.class_var.get(),
            "padding": self.pad_var.get(), "pct": self.pct_var.get(),
        })

    # ── ledger status ────────────────────────────────────────────────────────

    def _refresh_ledger_label(self) -> None:
        out = self.out_var.get().strip()
        if out and os.path.isdir(out):
            ledger = load_ledger(out)
            count = len(ledger["videos"])
            self.ledger_lbl.config(text=f"{count} in ledger")
        else:
            self.ledger_lbl.config(text="")

    # ── Open Output Folder ───────────────────────────────────────────────────

    def _open_output(self) -> None:
        out = self.out_var.get().strip()
        if out and os.path.isdir(out):
            subprocess.run(["open", out])
        else:
            messagebox.showinfo("No Folder", "Set a valid output folder first.")

    # ── View Ledger ──────────────────────────────────────────────────────────

    def _view_ledger(self) -> None:
        out = self.out_var.get().strip()
        if not out or not os.path.isdir(out):
            messagebox.showinfo("No Folder", "Set a valid output folder first.")
            return

        ledger = load_ledger(out)
        videos = ledger.get("videos", [])

        win = tk.Toplevel(self.root)
        win.title("Ledger")
        win.geometry("820x420")
        win.transient(self.root)

        ttk.Label(win, text=f"Ledger: {len(videos)} videos",
                  font=("Helvetica", 12, "bold"),
                  padding=10).pack(anchor="w")

        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)

        cols = ("name", "shape", "rating", "original", "processed", "validation")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=15)
        tree.heading("name", text="Name")
        tree.heading("shape", text="Shape")
        tree.heading("rating", text="Class")
        tree.heading("original", text="Original File")
        tree.heading("processed", text="Date")
        tree.heading("validation", text="Validation")
        tree.column("name", width=220)
        tree.column("shape", width=70, anchor="center")
        tree.column("rating", width=50, anchor="center")
        tree.column("original", width=180)
        tree.column("processed", width=140, anchor="center")
        tree.column("validation", width=80, anchor="center")

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for v in videos:
            checks = v.get("validation", {})
            all_pass = all(checks.values()) if checks else None
            val_status = "PASS" if all_pass is True else (
                "FAIL" if all_pass is False else "?")
            tree.insert("", "end", values=(
                v.get("name", v.get("seq", "")),
                v.get("shape", ""),
                v.get("rating", ""),
                v.get("original", ""),
                v.get("processed", ""),
                val_status,
            ))

    # ── Reset Ledger ─────────────────────────────────────────────────────────

    def _reset_ledger(self) -> None:
        out = self.out_var.get().strip()
        if not out or not os.path.isdir(out):
            messagebox.showinfo("No Folder", "Set a valid output folder first.")
            return

        ledger = load_ledger(out)
        count = len(ledger.get("videos", []))
        if count == 0:
            messagebox.showinfo("Empty", "Ledger is already empty.")
            return

        if not messagebox.askyesno(
            "Reset Ledger",
            f"This will clear {count} entries from the ledger.\n\n"
            "Processed files will NOT be deleted, but the app will\n"
            "no longer recognise them as already processed.\n\n"
            "Continue?",
        ):
            return

        save_ledger(out, {"videos": []})
        self._refresh_ledger_label()
        self.status.set("Ledger reset")

    # ── Watermark Preview ────────────────────────────────────────────────────

    def _preview_watermark(self) -> None:
        inp = self.inp_var.get().strip()
        logo = self.logo_var.get().strip()
        if not inp or not os.path.isdir(inp):
            messagebox.showerror("Error", "Select a valid input folder.")
            return
        if not logo or not os.path.isfile(logo):
            messagebox.showerror("Error", "Select a valid logo file.")
            return

        found = scan_input_videos(inp)
        if not found:
            messagebox.showinfo("No Videos", "No video files in the input folder.")
            return

        fname, src, _cls = found[0]
        pct = self.pct_var.get()
        try:
            pad = int(self.pad_var.get() or 20)
        except ValueError:
            pad = 20

        self.status.set("Generating preview...")
        self.root.update_idletasks()

        def _generate() -> None:
            tmp = watermark_preview_frame(src, logo, pct, pad)
            self.root.after(0, lambda: self._show_preview(tmp, fname))

        threading.Thread(target=_generate, daemon=True).start()

    def _show_preview(self, img_path: str | None, filename: str) -> None:
        self.status.set("Ready")
        if img_path is None:
            messagebox.showerror("Preview Failed",
                                 "Could not generate preview frame.")
            return

        try:
            win = tk.Toplevel(self.root)
            win.title(f"Watermark Preview — {filename}")
            win.transient(self.root)

            photo = tk.PhotoImage(file=img_path)
            win._photo = photo

            label = ttk.Label(win, image=photo)
            label.pack(padx=10, pady=10)

            def _refresh() -> None:
                win.destroy()
                self._preview_watermark()

            ttk.Button(win, text="Refresh Preview",
                       command=_refresh).pack(pady=(0, 10))
        finally:
            try:
                os.remove(img_path)
            except OSError:
                pass

    # ── File Selection Dialog ────────────────────────────────────────────────

    def _show_file_selection(self, out: str, logo: str,
                             found: list[tuple[str, str, str]]) -> None:
        win = tk.Toplevel(self.root)
        win.title("Select Videos to Process")
        win.geometry("600x450")
        win.transient(self.root)
        win.grab_set()

        ttk.Label(win, text=f"Found {len(found)} videos",
                  font=("Helvetica", 12, "bold"),
                  padding=10).pack(anchor="w")

        btn_frame = ttk.Frame(win, padding=(10, 0))
        btn_frame.pack(fill="x")

        # key = index in found list
        check_vars: dict[int, tk.BooleanVar] = {}

        def select_all() -> None:
            for var in check_vars.values():
                var.set(True)

        def select_none() -> None:
            for var in check_vars.values():
                var.set(False)

        ttk.Button(btn_frame, text="Select All",
                   command=select_all).pack(side="left", padx=2)
        ttk.Button(btn_frame, text="Select None",
                   command=select_none).pack(side="left", padx=2)

        # Check which videos are already processed
        ledger = load_ledger(out)
        processed = {v["original"] for v in ledger.get("videos", [])}

        # Scrollable checkbox list
        canvas = tk.Canvas(win)
        scrollbar = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)

        scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True, padx=10, pady=5)
        scrollbar.pack(side="right", fill="y", pady=5)

        def _on_mousewheel(event: tk.Event) -> None:
            canvas.yview_scroll(-1 * event.delta, "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        win.bind("<Destroy>",
                 lambda e: canvas.unbind_all("<MouseWheel>") if e.widget is win else None)

        for idx, (fname, _path, cls) in enumerate(found):
            already = fname in processed
            var = tk.BooleanVar(value=not already)
            check_vars[idx] = var
            cls_label = f"[{cls}]" if cls else ""
            suffix = "  (already processed)" if already else ""
            text = f"{cls_label} {fname}{suffix}".strip()
            ttk.Checkbutton(scroll_frame, text=text,
                            variable=var).pack(anchor="w", padx=5, pady=1)

        bottom = ttk.Frame(win, padding=10)
        bottom.pack(fill="x")

        def _go() -> None:
            selected = [found[idx] for idx, var in check_vars.items() if var.get()]
            win.destroy()
            if not selected:
                messagebox.showinfo("Nothing Selected", "No videos selected.")
                return
            self._begin_processing(out, logo, selected)

        ttk.Button(bottom, text="Cancel",
                   command=win.destroy).pack(side="right", padx=4)
        ttk.Button(bottom, text="Process Selected",
                   command=_go).pack(side="right", padx=4)

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

        found = scan_input_videos(inp)
        if not found:
            messagebox.showinfo("Nothing to do",
                                "No video files found in the input folder.")
            return

        # Apply default class to files without subfolder-inferred class
        default_cls = "a" if self.class_var.get() == "adult" else "s"
        found = [(f, p, c if c else default_cls) for f, p, c in found]

        self._show_file_selection(out, logo, found)

    def _begin_processing(self, out: str, logo: str,
                          found: list[tuple[str, str, str]]) -> None:
        self._persist()
        self.processing = True
        self._failed_videos = []
        self.go_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.retry_btn.pack_forget()
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self.pbar["value"] = 0

        threading.Thread(
            target=self._run,
            args=(out, logo, found),
            daemon=True,
        ).start()

    def _run(self, out: str, logo: str,
             found: list[tuple[str, str, str]]) -> None:
        pct = self.pct_var.get()
        try:
            pad = int(self.pad_var.get() or 20)
        except ValueError:
            pad = 20
        total = len(found)

        os.makedirs(out, exist_ok=True)
        ledger = load_ledger(out)

        # Batch timestamp — shared by all videos in this run
        batch_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._ui(log=f"Found {total} video(s) to process")
        self._ui(log=f"Batch: {batch_ts}\n")

        # Build set of already-processed originals
        processed_originals = {v["original"] for v in ledger["videos"]}

        completed = 0
        skipped = 0
        idx = 0  # per-batch counter
        for i, (fname, src, cls) in enumerate(found):
            if not self.processing:
                self._ui(log="— Cancelled —")
                break

            # Skip already-processed
            if fname in processed_originals:
                self._ui(
                    log=f"[{i + 1}/{total}]  {fname} — already processed, skipping",
                    progress=(i + 1) / total * 100,
                )
                skipped += 1
                continue

            idx += 1

            # Probe to get dimensions for shape classification
            try:
                w, h, _dur, _fps = probe(src)
                shape = detect_aspect_ratio(w, h)
            except Exception:
                shape = "unknown"
                w, h = 0, 0

            # Build the new name: aspect-class-timestamp-index
            seq = f"{shape}-{cls}-{batch_ts}-{idx:03d}"
            seq_4x5 = f"4x5-{cls}-{batch_ts}-{idx:03d}"
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

                # Progress callback for encoding
                def _on_encode_progress(frac: float,
                                        _i: int = i, _total: int = total) -> None:
                    pct_str = f"{frac * 100:.0f}%"
                    file_base = _i / _total
                    file_weight = 1.0 / _total
                    overall = (file_base + frac * file_weight * 0.8) * 100
                    self._ui(
                        status=f"Encoding {seq}  ({_i + 1}/{_total})  {pct_str}",
                        progress=overall,
                    )

                # Watermark + encode (broadcast-safe)
                self._ui(log="        encoding with watermark…")
                watermark(src, logo, dst, pct, pad, black_end=black_end,
                          on_progress=_on_encode_progress)

                # 4:5 centre-cropped video with independent logo
                dst_4x5 = os.path.join(vdir, f"{seq_4x5}.mp4")
                self._ui(log="        encoding 4:5 version…")
                watermark_4x5(src, logo, dst_4x5, pct, pad)

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
                snapshots(dst, vdir, seq, THUMBNAIL_COUNT,
                          raw_video=src, logo=logo,
                          logo_pct=pct, padding=pad,
                          prefix_4x5=seq_4x5)

                # Move raw video into the output folder
                raw_ext = Path(fname).suffix
                raw_dst = os.path.join(vdir, f"{seq}-raw{raw_ext}")
                shutil.move(src, raw_dst)
                self._ui(log="        moved raw video")

                # Update ledger with rich metadata
                ledger["videos"].append({
                    "name": seq,
                    "original": fname,
                    "shape": shape,
                    "rating": "adult" if cls == "a" else "safe",
                    "width": w,
                    "height": h,
                    "batch_id": batch_ts,
                    "processed": datetime.now().isoformat(timespec="seconds"),
                    "validation": checks,
                })
                save_ledger(out, ledger)

                self._ui(log=f"        ✓ {seq} complete\n")
                completed += 1
            except Exception as exc:
                self._ui(log=f"        ✗ ERROR: {exc}\n")
                self._failed_videos.append((fname, src, cls))
                shutil.rmtree(vdir, ignore_errors=True)

            self._ui(progress=(i + 1) / total * 100)

        # Summary
        parts = [f"{completed} processed"]
        if skipped:
            parts.append(f"{skipped} skipped")
        if self._failed_videos:
            parts.append(f"{len(self._failed_videos)} failed")
        self._ui(log=f"Done — {', '.join(parts)}.")
        self.root.after(0, lambda c=completed: self._done(c))

    def _done(self, completed: int) -> None:
        self.processing = False
        self.go_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._refresh_ledger_label()

        if self._failed_videos:
            self.retry_btn.pack(side="right", padx=4)
            self.status.set(
                f"Complete ({completed} ok, {len(self._failed_videos)} failed)")
        else:
            self.retry_btn.pack_forget()
            self.status.set(f"Complete ✓  ({completed} videos)")

    def _cancel(self) -> None:
        self.processing = False
        self.status.set("Cancelling…")

    # ── Retry Failed ─────────────────────────────────────────────────────────

    def _retry_failed(self) -> None:
        if not self._failed_videos:
            return

        out = self.out_var.get().strip()
        logo = self.logo_var.get().strip()

        still_exist = [(f, p, c) for f, p, c in self._failed_videos
                       if os.path.isfile(p)]

        if not still_exist:
            messagebox.showinfo("Nothing to Retry",
                                "Failed videos are no longer available.")
            self._failed_videos = []
            self.retry_btn.pack_forget()
            return

        self._begin_processing(out, logo, still_exist)


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
