#!/usr/bin/env python3
"""
dolphin-audio-converter - KDE/Dolphin Service Menu audio converter backend

Progress bar approach adapted from the yt-dlp KDE service menu by Fabio Mucciante:
  - ffmpeg writes progress to a temp file  (NOT pipe - pipe blocks qdbus updates)
  - a polling loop reads that file and drives qdbus independently
  - qdbus exit-code 1 means the user hit Cancel → ffmpeg is killed
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────
CONFIG_DIR = Path.home() / ".config" / "dolphin-audio-converter"
CONFIG_FILE = CONFIG_DIR / "config.json"

USER_SHARE = Path.home() / ".local" / "share"
DESKTOP_PATHS = [
    USER_SHARE / "kio" / "servicemenus" / "dolphin-audio-converter.desktop",
    USER_SHARE / "kservices5" / "ServiceMenus" / "dolphin-audio-converter.desktop",
]

# ─── Format metadata ──────────────────────────────────────────────────────────


FORMAT_DEFS = {
    "mp3": {
        "label": "MP3",
        "options": [
            ("V0", "VBR ~245 kbps (best)"),
            ("V2", "VBR ~190 kbps"),
            ("V4", "VBR ~165 kbps"),
            ("128k", "CBR 128 kbps"),
            ("192k", "CBR 192 kbps"),
            ("256k", "CBR 256 kbps"),
            ("320k", "CBR 320 kbps (max)"),
        ],
    },
    "ogg": {
        "label": "OGG (Vorbis)",
        "options": [
            ("Q6", "~192 kbps (default)"),
            ("Q3", "~112 kbps"),
            ("Q5", "~160 kbps"),
            ("Q8", "~256 kbps"),
            ("Q10", "~500 kbps (best)"),
        ],
    },
    "flac": {
        "label": "FLAC",
        "options": [("lossless", "Lossless")],
    },
    "wav": {
        "label": "WAV",
        "options": [("lossless", "Lossless (PCM 16-bit)")],
    },
    "m4a": {
        "label": "M4A (AAC)",
        "options": [
            ("192k", "192 kbps (default)"),
            ("128k", "128 kbps"),
            ("256k", "256 kbps"),
            ("320k", "320 kbps"),
        ],
    },
    "opus": {
        "label": "Opus",
        "options": [
            ("128k", "128 kbps (default)"),
            ("64k", "64 kbps (voice)"),
            ("96k", "96 kbps"),
            ("192k", "192 kbps"),
            ("256k", "256 kbps (transparent)"),
        ],
    },
    "alac": {
        "label": "ALAC (M4A)",
        "options": [("lossless", "Lossless")],
    },
}

CODEC_CATEGORY = {
    "mp3": "lossy",
    "vorbis": "lossy",
    "aac": "lossy",
    "opus": "lossy",
    "wmav1": "lossy",
    "wmav2": "lossy",
    "ac3": "lossy",
    "eac3": "lossy",
    "mp2": "lossy",
    "amrnb": "lossy",
    "amrwb": "lossy",
    "wmavoice": "lossy",
    "flac": "lossless",
    "alac": "lossless",
    "wavpack": "lossless",
    "ape": "lossless",
    "tta": "lossless",
    "truehd": "lossless",
    "mlp": "lossless",
    "pcm_s16le": "lossless",
    "pcm_s24le": "lossless",
    "pcm_s32le": "lossless",
    "pcm_f32le": "lossless",
    "pcm_f64le": "lossless",
    "pcm_s16be": "lossless",
}

# ─── qdbus binary ─────────────────────────────────────────────────────────────
# Match the yt-dlp detection order: prefer qdbus-qt5, then qdbus, then qdbus6
QDBUS = next(
    (name for name in ("qdbus-qt5", "qdbus", "qdbus6") if shutil.which(name)), None
)


# ─── Config helpers ───────────────────────────────────────────────────────────
def load_config() -> dict:
    defaults = {fmt: data["options"][0][0] for fmt, data in FORMAT_DEFS.items()}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            for k, v in defaults.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return defaults


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ─── Desktop-file patching ────────────────────────────────────────────────────
def find_desktop_file() -> Path | None:
    for p in DESKTOP_PATHS:
        if p.exists():
            return p
    return None


def quality_label(quality: str) -> str:
    return f" ({quality})" if quality != "lossless" else ""


def update_desktop_names(cfg: dict):
    desktop = find_desktop_file()
    if not desktop:
        return
    name_map = {}
    for fmt, data in FORMAT_DEFS.items():
        q = cfg[fmt]
        ql = f" ({q})" if q != "lossless" else ""
        # The key in desktop file actions is typically convertTo<FmtTitle>
        # e.g. convertToMp3, convertToOgg, convertToFlac
        key = f"convertTo{fmt.capitalize()}"
        name_map[key] = f"Convert to {data['label']}{ql}"
    lines = desktop.read_text().splitlines()
    current_action = None
    out = []
    for line in lines:
        m = re.match(r"^\[Desktop Action (\w+)\]", line)
        if m:
            current_action = m.group(1)
        if current_action and line.startswith("Name=") and current_action in name_map:
            line = f"Name={name_map[current_action]}"
        out.append(line)
    desktop.write_text("\n".join(out) + "\n")


# ─── ffprobe helpers ──────────────────────────────────────────────────────────
def get_duration(filepath: str) -> float | None:
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                filepath,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(r.stdout.strip())
    except Exception:
        return None


def probe_codec(filepath: str) -> str | None:
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                filepath,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return r.stdout.strip().lower() or None
    except Exception:
        return None


# ─── ffmpeg codec args ────────────────────────────────────────────────────────
def build_ffmpeg_args(fmt: str, quality: str) -> list:
    if fmt == "mp3":
        if re.match(r"^V\d$", quality):
            return ["-codec:a", "libmp3lame", "-q:a", quality[1:]]
        return ["-codec:a", "libmp3lame", "-b:a", quality]
    if fmt == "ogg":
        q = quality[1:] if quality.startswith("Q") else "6"
        return ["-codec:a", "libvorbis", "-q:a", q]
    if fmt == "m4a":
        return ["-codec:a", "aac", "-b:a", quality]
    if fmt == "opus":
        return ["-codec:a", "libopus", "-b:a", quality]
    if fmt == "flac":
        return ["-codec:a", "flac", "-compression_level", "8"]
    if fmt == "wav":
        return ["-codec:a", "pcm_s16le"]
    if fmt == "alac":
        return ["-codec:a", "alac"]
    return []


# ─── kdialog + notify ─────────────────────────────────────────────────────────
def kdialog(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kdialog"] + list(args), capture_output=True, text=True, env=os.environ.copy()
    )


def notify(title: str, msg: str, icon: str = "audio-x-generic"):
    subprocess.run(
        ["notify-send", "-i", icon, "-a", "Audio Converter", title, msg],
        capture_output=True,
    )


# ─── Progress bar (yt-dlp pattern) ───────────────────────────────────────────
def pbar_open(title: str, label: str) -> tuple | None:
    """
    Open a kdialog progressbar (0–100).
    Returns a (service, path) tuple for qdbus calls, or None on failure.

    kdialog --progressbar outputs TWO tokens: "org.kde.kdialog-1234 /ProgressDialog"
    In bash these get word-split automatically; in Python we must split manually.
    Passing the combined string as one qdbus argument gives a garbage service name,
    causing qdbus to exit 1 on every call - which we'd misread as Cancel.
    """
    r = kdialog("--title", title, "--progressbar", label, "100")
    parts = r.stdout.strip().split()
    if len(parts) >= 2:
        return (parts[0], parts[1])  # (service, /ObjectPath)
    if len(parts) == 1:
        return (parts[0], "/")  # older kdialog - path only
    return None


def pbar_set(handle: tuple | None, value: int, label: str | None = None) -> bool:
    """
    Push a new progress value via qdbus.
    Returns False if the dialog was closed (Cancel pressed) - qdbus exits 1.
    This is exactly how yt-dlp detects Cancel.
    """
    if not handle or not QDBUS:
        return True  # no qdbus → assume still alive
    service, path = handle
    r = subprocess.run(
        [QDBUS, service, path, "Set", "", "value", str(value)], capture_output=True
    )
    if r.returncode != 0:
        return False  # dialog gone → user cancelled
    if label is not None:
        subprocess.run(
            [QDBUS, service, path, "setLabelText", label], capture_output=True
        )
    return True


def pbar_close(handle: tuple | None):
    if handle and QDBUS:
        service, path = handle
        subprocess.run([QDBUS, service, path, "close"], capture_output=True)


# ─── Lossy warnings ───────────────────────────────────────────────────────────
def warn_if_lossy(input_codec: str | None, output_fmt: str) -> bool:
    src_cat = CODEC_CATEGORY.get(input_codec, "unknown")
    dst_data = FORMAT_DEFS.get(output_fmt)
    # Check if 'lossless' is the only option (or one of them? usually simpler to check first option)
    # For formats like FLAC, options=[('lossless', 'Lossless')]
    is_lossless = False
    if dst_data:
        opts = dst_data["options"]
        if len(opts) == 1 and opts[0][0] == "lossless":
            is_lossless = True

    dst_lossy = not is_lossless

    if src_cat == "lossy" and dst_lossy:
        r = kdialog(
            "--title",
            "Audio Converter - Warning",
            "--warningyesno",
            f"<b>Lossy → Lossy conversion</b><br><br>"
            f"Source codec: <i>{input_codec.upper()}</i><br>"
            f"Target format: <i>{output_fmt.upper()}</i><br><br>"
            "Re-encoding between lossy formats permanently degrades quality. "
            "A lossless source is strongly recommended.<br><br>"
            "<b>Convert anyway?</b>",
            "--yes-label",
            "Convert",
            "--no-label",
            "Cancel",
        )
        return r.returncode == 0

    if src_cat == "lossy" and not dst_lossy:
        r = kdialog(
            "--title",
            "Audio Converter - Warning",
            "--warningyesno",
            f"<b>Lossy → Lossless conversion</b><br><br>"
            f"Source codec: <i>{input_codec.upper()}</i><br>"
            f"Target format: <i>{output_fmt.upper()}</i><br><br>"
            "Wrapping lossy audio in a lossless container <b>will not recover quality</b> "
            "- the output will simply be larger.<br><br>"
            "<b>Convert anyway?</b>",
            "--yes-label",
            "Convert",
            "--no-label",
            "Cancel",
        )
        return r.returncode == 0

    return True  # lossless source → no warning needed


# ─── Conversion ───────────────────────────────────────────────────────────────
def convert_files(files: list, fmt: str, quality: str):
    ffmpeg_args = build_ffmpeg_args(fmt, quality)
    total = len(files)
    errors = []
    done = 0

    q_suffix = f" ({quality})" if quality != "lossless" else ""
    handle = pbar_open(
        f"Audio Converter - {fmt.upper()}{q_suffix}", f"Starting… (0 of {total})"
    )

    for idx, filepath in enumerate(files):
        input_path = Path(filepath)
        if not input_path.exists():
            errors.append(f"File not found: {filepath}")
            continue

        suffix = ".m4a" if fmt in ("m4a", "alac") else f".{fmt}"
        output_path = input_path.with_suffix(suffix)
        if output_path == input_path:
            output_path = input_path.with_stem(input_path.stem + f"_{fmt}").with_suffix(
                suffix
            )

        short_name = input_path.name[:50] + ("…" if len(input_path.name) > 50 else "")
        file_label = f"[{idx + 1}/{total}] {short_name}"

        # Show lossy warning before converting
        input_codec = probe_codec(filepath)
        if not warn_if_lossy(input_codec, fmt):
            continue

        # Each file gets an equal slice of 0–100 so the bar sweeps
        # continuously rather than resetting to 0 for every file.
        slice_start = int(idx / total * 100)
        slice_end = int((idx + 1) / total * 100)

        pbar_set(handle, slice_start, f"Preparing: {file_label}")

        prog_fd, prog_path = tempfile.mkstemp(prefix="dac_prog_", suffix=".txt")
        os.close(prog_fd)

        duration = get_duration(filepath)

        cmd = (
            [
                "ffmpeg",
                "-y",
                "-i",
                filepath,
                "-progress",
                prog_path,
                "-nostats",
                "-loglevel",
                "error",
            ]
            + ffmpeg_args
            + [str(output_path)]
        )

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        cancelled = False
        last_pct = slice_start
        while proc.poll() is None:
            time.sleep(0.5)
            try:
                with open(prog_path, "r") as pf:
                    prog_content = pf.read()
                matches = re.findall(r"^out_time_ms=(\d+)", prog_content, re.MULTILINE)
                if matches and duration and duration > 0:
                    us = int(matches[-1])
                    file_pct = min(1.0, us / 1e6 / duration)
                    overall = slice_start + int(file_pct * (slice_end - slice_start))
                    if overall > last_pct:
                        last_pct = overall
                        alive = pbar_set(handle, overall, f"Converting: {file_label}")
                        if not alive:
                            proc.kill()
                            cancelled = True
                            break
            except Exception:
                pass

        proc.wait()
        try:
            os.unlink(prog_path)
        except Exception:
            pass

        if cancelled:
            try:
                output_path.unlink()
            except Exception:
                pass
            pbar_close(handle)
            notify(
                "Audio Converter - Cancelled",
                f"Cancelled on file {idx + 1} of {total}",
                "dialog-cancel",
            )
            return

        if proc.returncode != 0:
            stderr_bytes = proc.stderr.read() if proc.stderr else b""
            errors.append(
                f"{input_path.name}:\n{stderr_bytes.decode(errors='replace')[:400]}"
            )
            try:
                output_path.unlink()
            except Exception:
                pass
        else:
            done += 1
            pbar_set(handle, slice_end, f"Done: {file_label}")

    pbar_close(handle)

    # ── Final notification ────────────────────────────────────────────────────
    if errors:
        preview = "\n\n".join(errors[:3])
        if len(errors) > 3:
            preview += f"\n\n…and {len(errors) - 3} more"
        kdialog(
            "--title",
            "Audio Converter - Errors",
            "--error",
            f"Converted {done} of {total} file(s).\n\nErrors:\n{preview}",
        )
        notify(
            "Audio Converter - Finished with errors",
            f"{done}/{total} converted, {len(errors)} failed.",
            "dialog-error",
        )
    elif done > 0:
        notify(
            "Audio Converter - Done",
            f"✔  {done} file{'s' if done != 1 else ''} → {fmt.upper()}{q_suffix}",
            "audio-x-generic",
        )


# ─── Configure dialog ─────────────────────────────────────────────────────────
# ─── Configure dialog ─────────────────────────────────────────────────────────
def run_configure():
    cfg = load_config()
    defaults = {fmt: data["options"][0][0] for fmt, data in FORMAT_DEFS.items()}

    # Only show formats that have more than 1 option?
    # Or show all "lossy" ones with choices?
    # Original logic: if opts != ["lossless"]
    configurable = [
        fmt
        for fmt, data in FORMAT_DEFS.items()
        if not (len(data["options"]) == 1 and data["options"][0][0] == "lossless")
    ]

    menu_args = [
        "--title",
        "Audio Converter - Configure",
        "--menu",
        "Select a format to configure:",
    ]
    for f in configurable:
        cur = cfg.get(f, defaults[f])
        label = FORMAT_DEFS[f]["label"]
        menu_args += [f, f"{label}   (currently: {cur})"]

    r = kdialog(*menu_args)
    if r.returncode != 0:
        return
    chosen_fmt = r.stdout.strip()
    if chosen_fmt not in configurable:
        return

    fmt_def = FORMAT_DEFS[chosen_fmt]
    options = fmt_def["options"]  # list of (value, desc)
    current = cfg.get(chosen_fmt, options[0][0])

    q_args = [
        "--title",
        f"Configure {fmt_def['label']}",
        "--menu",
        f"Output quality for {fmt_def['label']}:",
    ]
    for val, desc in options:
        marker = "  ✔" if val == current else ""
        q_args += [val, f"{val} - {desc}{marker}"]

    r2 = kdialog(*q_args)
    if r2.returncode != 0:
        return
    chosen_q = r2.stdout.strip()
    # Validate choice
    valid_vals = [o[0] for o in options]
    if chosen_q not in valid_vals:
        return

    cfg[chosen_fmt] = chosen_q
    save_config(cfg)
    update_desktop_names(cfg)

    notify(
        "Audio Converter - Settings saved",
        f"{fmt_def['label']} quality → {chosen_q}",
        "configure",
    )


# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Dolphin audio converter backend")
    parser.add_argument("--format", help="Output format")
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args()

    if args.configure:
        run_configure()
        return

    if not args.format:
        parser.print_help()
        sys.exit(1)

    fmt = args.format.lower()
    if fmt not in FORMAT_DEFS:
        supported = ", ".join(FORMAT_DEFS.keys())
        kdialog(
            "--error",
            f"Unknown format: '{fmt}'\nSupported: {supported}",
        )
        sys.exit(1)

    if not args.files:
        kdialog("--error", "No input files provided.")
        sys.exit(1)

    if not shutil.which("ffmpeg"):
        kdialog(
            "--error",
            "<b>ffmpeg not found.</b><br><br>"
            "Install via your package manager:<br>"
            "• <tt>sudo apt install ffmpeg</tt> - Debian / Ubuntu / Mint<br>"
            "• <tt>sudo dnf install ffmpeg</tt> - Fedora<br>"
            "• <tt>sudo pacman -S ffmpeg</tt> - Arch / Manjaro<br>"
            "• <tt>sudo zypper install ffmpeg</tt> - openSUSE",
        )
        sys.exit(1)

    cfg = load_config()
    # If not in config, use the first option as default
    default_q = FORMAT_DEFS[fmt]["options"][0][0]
    quality = cfg.get(fmt, default_q)
    convert_files(args.files, fmt, quality)


if __name__ == "__main__":
    main()
