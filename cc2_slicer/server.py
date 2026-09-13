#!/usr/bin/env python3
"""
CC2 Headless Slicer & Kiosk Microservice (Linux N100 Home Assistant Add-on).
Runs OrcaSlicer v2.4.2 CLI with xvfb-run, extracts thumbnails, serves print kiosk UI,
manages persistent print queue and history, and streams camera directly to/from Elegoo Centauri Carbon 2.
"""

from __future__ import annotations
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs
from PIL import Image

PORT = 8765
PRINTER_IP = os.getenv("PRINTER_IP", "192.168.1.100")
ACCESS_CODE = os.getenv("ACCESS_CODE", "")

# Load Home Assistant Add-on options if available
OPTIONS_FILE = Path("/data/options.json")
if OPTIONS_FILE.exists():
    try:
        with open(OPTIONS_FILE, "r", encoding="utf-8") as _f:
            _opts = json.load(_f)
            if _opts.get("printer_ip"):
                PRINTER_IP = _opts["printer_ip"]
            if _opts.get("access_code"):
                ACCESS_CODE = _opts["access_code"]
    except Exception as _e:
        print(f"[!] Warning reading /data/options.json: {_e}", flush=True)

ORCA_BIN = "/opt/orca/AppRun"
PROFILES_DIR = Path("/profiles")
MACHINE_PROFILE = PROFILES_DIR / "machine/Elegoo Centauri Carbon 2 0.4 nozzle.json"
PROCESS_PROFILE = PROFILES_DIR / "process/0.20mm Standard @Elegoo CC2 0.4 nozzle.json"
FILAMENT_PROFILE = PROFILES_DIR / "filament/Elegoo Rapid PLA+ @ECC2.json"
OUTPUT_DIR = Path("/tmp/cc2_output")
UPLOAD_DIR = Path("/tmp/uploads")

# Shared queue and history storage (prefers mounted /config, falls back to persistent /data)
if Path("/config").exists() and os.access("/config", os.W_OK):
    QUEUE_FILE = Path("/config/cc2_print_queue.json")
    HISTORY_FILE = Path("/config/cc2_print_history.json")
else:
    QUEUE_FILE = Path("/data/cc2_print_queue.json")
    HISTORY_FILE = Path("/data/cc2_print_history.json")


def patch_pycentauri_cc2() -> None:
    """Ensure pycentauri CC2Printer sends Calibration_switch, slot_map, and config to method 1020."""
    try:
        import pycentauri.cc2 as cc2_mod

        async def custom_start_print(
            self,
            filename: str,
            *,
            storage: str = "local",
            auto_leveling: bool = True,
            timelapse: bool = False,
            plate: str = "A",
            tray_id: int | None = None,
            canvas_id: int = 0,
            slot_map: list[dict[str, Any]] | None = None,
        ):
            self._require_control("start_print")
            if slot_map is None and tray_id is not None:
                # Map tools 0..3 to the chosen tray so any tool reference (T0, T1, etc.) uses this slot
                slot_map = [{"t": i, "canvas_id": int(canvas_id), "tray_id": int(tray_id)} for i in range(4)]

            config: dict[str, Any] = {
                "delay_video": bool(timelapse),
                "printer_check": bool(auto_leveling),
                "print_layout": str(plate),
                "bedlevel_force": False,
            }
            if slot_map is not None:
                config["slot_map"] = slot_map

            params: dict[str, Any] = {
                "filename": filename,
                "Filename": filename,
                "storage_media": storage,
                "Calibration_switch": 1 if auto_leveling else 0,
                "calibration_switch": 1 if auto_leveling else 0,
                "Tlp_Switch": 1 if timelapse else 0,
                "config": config,
            }
            if slot_map is not None:
                params["slot_map"] = slot_map

            print(f"[CC2] Method 1020 start_print: file={filename}, plate={plate}, slot_map={slot_map}, auto_leveling={auto_leveling}, Calibration_switch={params['Calibration_switch']}, printer_check={config['printer_check']}", flush=True)
            result = await self._cc2_request(1020, params, timeout=15.0)
            return self._wrap_result(1020, result)

        cc2_mod.CC2Printer.start_print = custom_start_print
        print("[+] Patched pycentauri.cc2.CC2Printer.start_print with slot_map & config support", flush=True)

        p = Path(cc2_mod.__file__)
        txt = p.read_text(encoding="utf-8")
        if '"Calibration_switch": 1 if auto_leveling else 0' not in txt:
            old_block = 'params: dict[str, Any] = {\n            "filename": filename,\n            "storage_media": storage,\n        }'
            new_block = 'params: dict[str, Any] = {\n            "filename": filename,\n            "Filename": filename,\n            "storage_media": storage,\n            "Calibration_switch": 1 if auto_leveling else 0,\n            "calibration_switch": 1 if auto_leveling else 0,\n            "Tlp_Switch": 1 if timelapse else 0,\n        }'
            if old_block in txt:
                p.write_text(txt.replace(old_block, new_block), encoding="utf-8")
                print("[+] Patched pycentauri/cc2.py file on disk", flush=True)
    except Exception as e:
        print(f"[!] Warning checking/patching pycentauri: {e}", flush=True)


patch_pycentauri_cc2()


def parse_time_from_string_or_filename(s: str) -> int:
    """Parse duration like '1h28m', '4h9m', '45m', '1h 30m 15s' into seconds."""
    if not s:
        return 0
    # Strip file extension so .3mf never triggers "3m" (which equals 180s!)
    s_clean = re.sub(r"\.[a-zA-Z0-9]+$", "", s)
    total = 0
    d_match = re.search(r"(\d+)\s*d(?![a-zA-Z])", s_clean, re.IGNORECASE)
    h_match = re.search(r"(\d+)\s*h(?![a-zA-Z])", s_clean, re.IGNORECASE)
    m_match = re.search(r"(\d+)\s*m(?![a-zA-Z])", s_clean, re.IGNORECASE)
    s_match = re.search(r"(\d+)\s*s(?![a-zA-Z])", s_clean, re.IGNORECASE)
    if d_match:
        total += int(d_match.group(1)) * 86400
    if h_match:
        total += int(h_match.group(1)) * 3600
    if m_match:
        total += int(m_match.group(1)) * 60
    if s_match:
        total += int(s_match.group(1))
    return total


def format_duration(seconds: int | float | None) -> str:
    """Format seconds into 'Xh Ym' or 'Xm Ys'."""
    if not seconds or seconds <= 0:
        return "N/A"
    sec = int(seconds)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def format_duration_hms(seconds: int | float | None) -> str:
    """Format seconds into HH:MM:SS."""
    if not seconds or seconds <= 0:
        return "00:00:00"
    sec = int(seconds)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def extract_thumbnail(mf_path: Path, plate_idx: int = 1) -> bytes | None:
    try:
        with zipfile.ZipFile(mf_path) as z:
            candidates = [
                f"Metadata/plate_{plate_idx}.png",
                f"Metadata/plate_{plate_idx}_small.png",
                f"Metadata/plate_{plate_idx + 1}.png",
                "Metadata/plate_1.png",
                "Metadata/top_1.png",
            ]
            for cand in candidates:
                if cand in z.namelist():
                    data = z.read(cand)
                    img = Image.open(io.BytesIO(data))
                    img.thumbnail((300, 300), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    return buf.getvalue()
    except Exception as e:
        print(f"[!] Thumbnail extraction error: {e}", flush=True)
    return None


def create_thumbnail_block(img: Image.Image, size: tuple[int, int]) -> list[str]:
    """Generate G-code thumbnail comment lines for a specific resolution."""
    thumb = img.copy()
    thumb.thumbnail(size, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, format="PNG")
    data = buf.getvalue()
    b64 = base64.b64encode(data).decode("ascii")
    lines = [b64[i : i + 78] for i in range(0, len(b64), 78)]
    return [
        f"; thumbnail begin {size[0]}x{size[1]} {len(data)}",
        *[f"; {l}" for l in lines],
        "; thumbnail end",
    ]


def extract_thumbnail_from_gcode(gcode_path: Path) -> bytes | None:
    """Extract embedded thumbnail PNG bytes from G-code comments."""
    try:
        if not gcode_path.exists() or not gcode_path.is_file():
            return None
        with open(gcode_path, "r", encoding="utf-8", errors="replace") as f:
            lines = [f.readline() for _ in range(600)]
        in_thumb = False
        b64_lines: list[str] = []
        found_blocks: dict[str, bytes] = {}
        curr_res = ""
        for line in lines:
            line_s = line.strip()
            if line_s.startswith("; thumbnail begin"):
                parts = line_s.split()
                curr_res = parts[3] if len(parts) > 3 else "thumb"
                in_thumb = True
                b64_lines = []
            elif line_s.startswith("; thumbnail end"):
                in_thumb = False
                if b64_lines:
                    try:
                        found_blocks[curr_res] = base64.b64decode("".join(b64_lines))
                    except Exception:
                        pass
            elif in_thumb:
                cleaned = line_s.lstrip(";").strip()
                b64_lines.append(cleaned)

        if "300x300" in found_blocks:
            return found_blocks["300x300"]
        if "320x320" in found_blocks:
            return found_blocks["320x320"]
        if "144x144" in found_blocks:
            return found_blocks["144x144"]
        if found_blocks:
            return next(iter(found_blocks.values()))
    except Exception as e:
        print(f"[!] Error extracting thumbnail from gcode: {e}", flush=True)
    return None


def inject_gcode_thumbnails(gcode_path: Path, png_data: bytes) -> None:
    """
    Inject both 144x144 (required by Elegoo CC2 printer touch screen)
    and 300x300 (used by Kiosk Web UI and Home Assistant) into the G-code header.
    """
    try:
        img = Image.open(io.BytesIO(png_data))
        thumb_block = [
            *create_thumbnail_block(img, (144, 144)),
            *create_thumbnail_block(img, (300, 300)),
            "",
        ]
        with open(gcode_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Strip any existing leading thumbnail blocks to avoid duplicates
        lines = content.splitlines()
        idx_end = -1
        in_thumb = False
        for i, line in enumerate(lines[:600]):
            if "; thumbnail begin" in line:
                in_thumb = True
            elif "; thumbnail end" in line:
                in_thumb = False
                idx_end = i
            elif not in_thumb and line.strip() and not line.startswith("; thumbnail"):
                break

        remaining = lines[idx_end + 1 :] if idx_end != -1 else lines
        new_content = "\n".join(thumb_block) + "\n" + "\n".join(remaining)
        with open(gcode_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        print(f"[!] Failed to inject thumbnail: {e}", flush=True)


def parse_estimated_time_from_gcode(gcode_path: Path) -> int:
    """
    Parse actual sliced print duration from G-code comments.
    OrcaSlicer outputs '; estimated printing time (normal mode) = Xh Ym Zs'
    at the very end of the file.
    """
    try:
        size = gcode_path.stat().st_size
        read_size = min(size, 262144)  # Read up to last 256KB
        with open(gcode_path, "rb") as f:
            if size > read_size:
                f.seek(size - read_size)
            chunk = f.read().decode("utf-8", errors="ignore")

        for line in reversed(chunk.splitlines()):
            line_l = line.lower()
            if "estimated printing time" in line_l and "first layer" not in line_l:
                m = re.search(r"=\s*(.+)", line)
                if m:
                    parsed = parse_time_from_string_or_filename(m.group(1).strip())
                    if parsed > 0:
                        return parsed
    except Exception as e:
        print(f"[!] Error parsing gcode estimated time: {e}", flush=True)
    return 0


def sanitize_3mf_for_slicing(file_path: Path) -> Path:
    """
    Sanitizes embedded configs in 3MF archives that cause OrcaSlicer CLI validation errors (exit code 238).
    Specifically, Bambu Studio and OrcaSlicer GUI export -1 for 'auto' parameters:
      - raft_first_layer_expansion: -1 -> 0
      - tree_support_wall_count: -1 -> 1
      - prime_tower_lift_height: -1 -> 0
    which fail OrcaSlicer CLI schema validation [0, ...] with exit code 238 (-18).
    """
    if file_path.suffix.lower() != ".3mf":
        return file_path

    sanitized_path = file_path.with_name(f"clean_{file_path.name}")
    try:
        fixes = {
            "raft_first_layer_expansion": "0",
            "tree_support_wall_count": "1",
            "prime_tower_lift_height": "0",
        }
        with zipfile.ZipFile(file_path, "r") as zin, zipfile.ZipFile(sanitized_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                buf = zin.read(item.filename)
                if "config" in item.filename.lower() or item.filename.endswith((".json", ".config")):
                    try:
                        text = buf.decode("utf-8", errors="ignore")
                        data = json.loads(text)
                        modified = False
                        for k, v in fixes.items():
                            if k in data and str(data[k]) in ("-1", "-1.0", -1):
                                data[k] = v
                                modified = True
                        if modified:
                            buf = json.dumps(data, indent=4).encode("utf-8")
                    except Exception:
                        pass
                zout.writestr(item, buf)
        print(f"[+] Sanitized 3MF project settings for CLI compatibility: {file_path.name}", flush=True)
        return sanitized_path
    except Exception as e:
        print(f"[!] Warning: Failed to sanitize 3MF {file_path.name}: {e}", flush=True)
        return file_path


def slice_and_upload(input_path: Path) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    filename = input_path.name
    base_name = re.sub(r"\.(stl|3mf)$", "", filename, flags=re.IGNORECASE)

    # Sanitize 3MF to avoid OrcaSlicer CLI validation errors (code 238)
    slice_input = sanitize_3mf_for_slicing(input_path)

    tmp_slice_dir = Path("/tmp/orca_out")
    tmp_slice_dir.mkdir(parents=True, exist_ok=True)
    for old in tmp_slice_dir.glob("*"):
        try:
            if old.is_file():
                old.unlink()
        except Exception:
            pass

    cmd = [
        "xvfb-run", "-a",
        ORCA_BIN,
        "--slice", "0",
        "--load-settings", str(MACHINE_PROFILE),
        "--load-settings", str(PROCESS_PROFILE),
        "--load-settings", str(FILAMENT_PROFILE),
        "--outputdir", str(tmp_slice_dir),
        str(slice_input),
    ]

    print(f"[*] Slicing {filename} via OrcaSlicer CLI...", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    slice_time = round(time.time() - t0, 1)

    # Clean up temporary sanitized file
    if slice_input != input_path:
        try:
            slice_input.unlink(missing_ok=True)
        except Exception:
            pass

    if proc.returncode != 0:
        err_msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"OrcaSlicer exited code {proc.returncode}: {err_msg[-500:]}")

    gcode_files = list(tmp_slice_dir.glob("*.gcode"))
    if not gcode_files:
        raise RuntimeError(f"No .gcode generated in {tmp_slice_dir}")

    gcode_path = gcode_files[0]
    final_gcode = OUTPUT_DIR / f"{base_name}.gcode"
    gcode_path.rename(final_gcode)

    # Parse estimated print time
    est_seconds = parse_estimated_time_from_gcode(final_gcode) or parse_time_from_string_or_filename(filename) or 3600

    # Extract thumbnail if 3MF
    thumb_png = None
    if input_path.suffix.lower() == ".3mf":
        thumb_png = extract_thumbnail(input_path)
        if thumb_png:
            inject_gcode_thumbnails(final_gcode, thumb_png)
            # Cache thumbnail
            for thumb_dest in [
                Path("/tmp/uploads/last_thumbnail.png"),
                Path("/tmp/orca_out/last_thumbnail.png"),
                Path(f"/config/www/cc2_kiosk/thumbnails/{base_name}.png") if Path("/config/www/cc2_kiosk").exists() else None,
                Path("/config/www/cc2_kiosk/thumbnails/last_thumbnail.png") if Path("/config/www/cc2_kiosk").exists() else None,
            ]:
                if thumb_dest:
                    try:
                        thumb_dest.parent.mkdir(parents=True, exist_ok=True)
                        thumb_dest.write_bytes(thumb_png)
                    except Exception:
                        pass

    # Upload to CC2 printer via pycentauri CLI
    up_cmd = [
        "centauri", "upload", str(final_gcode),
        "--host", PRINTER_IP,
        "--access-code", ACCESS_CODE,
        "--enable-control",
    ]
    print(f"[*] Uploading to CC2 at {PRINTER_IP}...", flush=True)
    up_proc = subprocess.run(up_cmd, capture_output=True, text=True, timeout=120)

    if up_proc.returncode != 0:
        raise RuntimeError(f"centauri upload failed: {up_proc.stderr}")

    result = {
        "status": "success",
        "gcode": final_gcode.name,
        "filename": final_gcode.name,
        "slice_time": slice_time,
        "slice_time_seconds": slice_time,
        "estimated_seconds": est_seconds,
        "estimated_formatted": format_duration(est_seconds),
        "size_bytes": final_gcode.stat().st_size,
        "uploaded": True,
        "printer_ip": PRINTER_IP,
    }
    if thumb_png:
        result["thumbnail_b64"] = base64.b64encode(thumb_png).decode("ascii")
    return result


def process_gcode_upload(input_path: Path) -> dict[str, Any]:
    """
    Process an already-sliced .gcode file:
    - Save to OUTPUT_DIR without running OrcaSlicer
    - Extract and inject dual 144x144 + 300x300 thumbnails if available
    - Parse estimated duration from G-code comments
    - Upload directly to CC2 printer storage
    """
    t0 = time.time()
    filename = input_path.name
    base_name = re.sub(r"\.gcode$", "", filename, flags=re.IGNORECASE)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    final_gcode = OUTPUT_DIR / filename

    if input_path.resolve() != final_gcode.resolve():
        shutil.copy2(input_path, final_gcode)

    # Parse estimated print time
    est_seconds = parse_estimated_time_from_gcode(final_gcode) or parse_time_from_string_or_filename(filename) or 3600

    # Check for thumbnails in the G-code
    thumb_png = extract_thumbnail_from_gcode(final_gcode)
    if thumb_png:
        # Re-inject to guarantee both 144x144 (printer LCD) and 300x300 (web) exist
        inject_gcode_thumbnails(final_gcode, thumb_png)
        # Cache thumbnail
        for thumb_dest in [
            Path("/tmp/uploads/last_thumbnail.png"),
            Path("/tmp/orca_out/last_thumbnail.png"),
            Path(f"/config/www/cc2_kiosk/thumbnails/{base_name}.png") if Path("/config/www/cc2_kiosk").exists() else None,
            Path("/config/www/cc2_kiosk/thumbnails/last_thumbnail.png") if Path("/config/www/cc2_kiosk").exists() else None,
        ]:
            if thumb_dest:
                try:
                    thumb_dest.parent.mkdir(parents=True, exist_ok=True)
                    thumb_dest.write_bytes(thumb_png)
                except Exception:
                    pass

    # Upload to CC2 printer via pycentauri CLI
    up_cmd = [
        "centauri", "upload", str(final_gcode),
        "--host", PRINTER_IP,
        "--access-code", ACCESS_CODE,
        "--enable-control",
    ]
    print(f"[*] Uploading pre-sliced G-code to CC2 at {PRINTER_IP}...", flush=True)
    up_proc = subprocess.run(up_cmd, capture_output=True, text=True, timeout=120)

    upload_ok = (up_proc.returncode == 0)
    if not upload_ok:
        print(f"[!] Warning: upload to printer reported: {up_proc.stderr}", flush=True)

    elapsed = round(time.time() - t0, 1)
    result = {
        "status": "success",
        "is_gcode": True,
        "gcode": final_gcode.name,
        "filename": final_gcode.name,
        "slice_time": elapsed,
        "slice_time_seconds": elapsed,
        "estimated_seconds": est_seconds,
        "estimated_formatted": format_duration(est_seconds),
        "size_bytes": final_gcode.stat().st_size,
        "uploaded": upload_ok,
        "printer_ip": PRINTER_IP,
    }
    if thumb_png:
        result["thumbnail_b64"] = base64.b64encode(thumb_png).decode("ascii")
    return result


def load_queue() -> list[dict[str, Any]]:
    if QUEUE_FILE.exists():
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] Error reading queue: {e}", flush=True)
    return []


def save_queue(queue: list[dict[str, Any]]) -> None:
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(queue, f, indent=2)
    except Exception as e:
        print(f"[!] Error saving queue: {e}", flush=True)


def load_history() -> list[dict[str, Any]]:
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] Error reading history: {e}", flush=True)
    return []


def save_history(history: list[dict[str, Any]]) -> None:
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[:100], f, indent=2)
    except Exception as e:
        print(f"[!] Error saving history: {e}", flush=True)


def get_ams_slots() -> dict[str, str]:
    token = os.getenv("SUPERVISOR_TOKEN")
    if token:
        try:
            slots = {}
            for slot in ["a1", "a2", "a3", "a4"]:
                req = urllib.request.Request(
                    f"http://supervisor/core/api/states/sensor.cc2_{slot}_name",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    data = json.loads(resp.read().decode())
                    val = data.get("state")
                    slots[slot.upper()] = val if val not in ["unknown", "unavailable", None] else "Empty"
            if any(v != "Empty" for v in slots.values()):
                return slots
        except Exception:
            pass
    return {"A1": "PLA", "A2": "PLA", "A3": "PLA PRO", "A4": "RAPID PLA+"}


def match_filament(required_filament: str, ams_slots: dict[str, str]) -> dict[str, Any]:
    req = (required_filament or "PLA").strip().upper()
    for slot, loaded in ams_slots.items():
        loaded_clean = loaded.strip().upper()
        if loaded_clean in ["EMPTY", "UNKNOWN"]:
            continue
        if req == loaded_clean:
            return {"match": True, "slot": slot, "loaded": loaded, "required": required_filament}
    for slot, loaded in ams_slots.items():
        loaded_clean = loaded.strip().upper()
        if loaded_clean in ["EMPTY", "UNKNOWN"]:
            continue
        if (req in loaded_clean) or (loaded_clean in req):
            return {"match": True, "slot": slot, "loaded": loaded, "required": required_filament}
    return {"match": False, "slot": None, "loaded_slots": ams_slots, "required": required_filament}


def resolve_tray_and_canvas(slot: str | None = None, filament: str | None = None) -> tuple[int, int]:
    """
    Map slot name ('A1', 'A2', 'A3', 'A4', 'B1'...) or filament name to (tray_id, canvas_id).
    Defaults to (0, 0).
    """
    if slot:
        s = str(slot).strip().upper()
        if s.startswith("A"):
            try:
                num = int(s[1:])
                return (max(0, min(3, num - 1)), 0)
            except ValueError:
                pass
        elif s.startswith("B"):
            try:
                num = int(s[1:])
                return (max(0, min(3, num - 1)), 1)
            except ValueError:
                pass
        elif s.isdigit():
            num = int(s)
            return (max(0, min(3, num - 1)), 0)

    # Fall back to matching filament against loaded AMS slots
    ams = get_ams_slots()
    matched = match_filament(filament or "PLA", ams)
    if matched.get("match") and matched.get("slot"):
        return resolve_tray_and_canvas(slot=matched["slot"])

    return (0, 0)


def save_cached_thumbnail(base_name: str, data: bytes) -> None:
    """Save thumbnail PNG into all relevant caching locations."""
    if not data:
        return
    for dest in [
        Path(f"/config/www/cc2_kiosk/thumbnails/{base_name}.png") if Path("/config/www/cc2_kiosk").exists() else None,
        Path(f"/tmp/cc2_output/{base_name}.png"),
        Path(f"/tmp/uploads/{base_name}.png"),
        Path(f"/tmp/orca_out/{base_name}.png"),
    ]:
        if dest:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            except Exception:
                pass


def fetch_thumbnail_from_printer(filename: str) -> bytes | None:
    """Fetch file thumbnail directly from CC2 printer via SDCP method 1045."""
    try:
        import asyncio
        from pycentauri.cc2 import CC2Printer

        async def _get():
            async with await CC2Printer.connect(PRINTER_IP, access_code=ACCESS_CODE, connect_timeout=4.0) as p:
                res = await p._cc2_request(1045, {"storage_media": "local", "file_name": filename}, timeout=4.0)
                if isinstance(res, dict) and res.get("error_code") == 0 and res.get("thumbnail"):
                    return base64.b64decode(res["thumbnail"])
            return None

        return asyncio.run(_get())
    except Exception as e:
        print(f"[!] Warning fetching thumbnail for {filename} from CC2: {e}", flush=True)
        return None


def get_cc2_files_direct() -> list[dict[str, Any]]:
    """List files directly from CC2 printer via SDCP method 1044 with exact print times."""
    import asyncio
    from pycentauri.cc2 import CC2Printer

    async def _list():
        async with await CC2Printer.connect(PRINTER_IP, access_code=ACCESS_CODE, connect_timeout=5.0) as p:
            res = await p._cc2_request(1044, {"storage_media": "local", "dir": "/", "limit": 60, "offset": 0}, timeout=5.0)
            files = []
            if isinstance(res, dict) and res.get("error_code") == 0:
                for f in res.get("file_list", []):
                    fname = f.get("filename", "")
                    if fname.endswith(".gcode"):
                        sec = f.get("print_time", 0)
                        size_bytes = f.get("size", 0)
                        size_mb = f"{round(size_bytes / (1024*1024), 1)} MB" if size_bytes else ""
                        color_map = f.get("color_map", [])
                        filament = color_map[0].get("name", "PLA") if color_map else "PLA"
                        files.append({
                            "filename": fname,
                            "size": size_mb,
                            "size_bytes": size_bytes,
                            "layers": f.get("layer", 0),
                            "print_time_seconds": sec,
                            "print_time_formatted": format_duration(sec),
                            "filament": filament,
                        })
            return files

    try:
        return asyncio.run(_list())
    except Exception as e:
        print(f"[!] Direct CC2 list_files failed: {e}, falling back to CLI", flush=True)
        cmd = ["centauri", "files", "--host", PRINTER_IP, "--access-code", ACCESS_CODE]
        res = subprocess.run(cmd, capture_output=True, text=True)
        files = []
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line or "file(s) on" in line:
                    continue
                parts = re.split(r'\s{2,}', line)
                if len(parts) >= 1 and parts[0].endswith(".gcode"):
                    fname = parts[0]
                    size = parts[1] if len(parts) > 1 else ""
                    layers = parts[2] if len(parts) > 2 else ""
                    files.append({"filename": fname, "size": size, "layers": layers})
        return files


def execute_cc2_start_print(
    filename: str,
    *,
    plate: str = "A",
    slot: str | None = None,
    tray_id: int | None = None,
    canvas_id: int = 0,
    filament: str | None = None,
    auto_leveling: bool = True,
    timelapse: bool = False,
) -> tuple[bool, str]:
    """
    Start a print on Elegoo CC2 ensuring exact plate, slot, and tray mapping are sent.
    Returns (success: bool, message: str).
    """
    if tray_id is None:
        tray_id, canvas_id = resolve_tray_and_canvas(slot=slot, filament=filament)

    print(f"[CC2] execute_cc2_start_print: filename={filename}, plate={plate}, slot={slot}, tray_id={tray_id}, canvas_id={canvas_id}, auto_leveling={auto_leveling}, timelapse={timelapse}", flush=True)

    import asyncio
    from pycentauri.cc2 import CC2Printer

    async def _run():
        async with await CC2Printer.connect(
            PRINTER_IP,
            access_code=ACCESS_CODE,
            enable_control=True,
            connect_timeout=10.0,
        ) as printer:
            res = await printer.start_print(
                filename,
                storage="local",
                auto_leveling=auto_leveling,
                timelapse=timelapse,
                plate=plate,
                tray_id=tray_id,
                canvas_id=canvas_id,
            )
            return res.inner if hasattr(res, "inner") else res

    try:
        res = asyncio.run(_run())
        err = res.get("error_code") if isinstance(res, dict) else 0
        if err == 0:
            return True, f"Print started successfully for {filename} (Plate {plate}, Slot A{tray_id+1})"
        else:
            return False, f"CC2 returned error_code {err}: {res}"
    except Exception as e:
        print(f"[!] Pycentauri start_print failed: {e}. Trying CLI fallback...", flush=True)
        cmd = [
            "centauri", "print", "start", filename,
            "--host", PRINTER_IP, "--access-code", ACCESS_CODE,
            "--enable-control"
        ]
        if auto_leveling:
            cmd.append("--auto-level")
        else:
            cmd.append("--no-auto-level")
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return True, f"Print started via CLI fallback: {res.stdout}"
        return False, f"Print start failed: {res.stderr or res.stdout or str(e)}"


class SlicerHTTPHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def _normalize_path(self) -> str:
        p = urlparse(self.path).path
        if p.startswith("/api/cc2_slicer"):
            p = p[len("/api/cc2_slicer"):]
            if not p.startswith("/"):
                p = "/" + p
        return p

    def do_GET(self):
        clean = self._normalize_path()

        # 1. Kiosk Web UI
        if clean in ("/", "/index.html", "/kiosk", "/kiosk/index.html"):
            for cand in [Path("/app/kiosk/index.html"), Path("/config/www/cc2_kiosk/index.html")]:
                if cand.exists():
                    try:
                        with open(cand, "rb") as f:
                            html = f.read()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(html)))
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        self.wfile.write(html)
                        return
                    except Exception as e:
                        print(f"[!] Error serving kiosk: {e}", flush=True)

        # 2. Status API
        if clean in ("/api/status", "/status"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ready",
                "engine": "OrcaSlicer-Linux-N100",
                "printer_ip": PRINTER_IP
            }).encode())
            return

        # 3. Queue API
        if clean in ("/api/queue", "/queue"):
            queue = load_queue()
            ams = get_ams_slots()
            total_eta = 0
            enhanced = []
            queue_dirty = False
            for idx, item in enumerate(queue):
                item_copy = dict(item)
                req_fil = item.get("filament", "PLA")
                item_copy["filament_status"] = match_filament(req_fil, ams)
                item_copy["position"] = idx + 1
                est = item.get("estimated_seconds")
                # Auto-repair duration from G-code if available or if previously miscalculated as 180s
                if not est or est == 180 or est == 3600:
                    cand = OUTPUT_DIR / item.get("filename", "")
                    if cand.exists():
                        gcode_est = parse_estimated_time_from_gcode(cand)
                        if gcode_est > 0:
                            est = gcode_est
                            item["estimated_seconds"] = est
                            item["estimated_formatted"] = format_duration(est)
                            queue_dirty = True
                if not est:
                    est = parse_time_from_string_or_filename(item.get("filename", "")) or 3600
                item_copy["estimated_seconds"] = est
                item_copy["estimated_formatted"] = format_duration(est)
                if item.get("status") != "printing":
                    total_eta += est
                enhanced.append(item_copy)

            if queue_dirty:
                save_queue(queue)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "queue": enhanced,
                "items": enhanced,
                "ams_slots": ams,
                "count": len(enhanced),
                "total_eta_seconds": total_eta,
                "total_eta_formatted": format_duration(total_eta),
            }).encode())
            return

        # 4. History API
        if clean in ("/api/history", "/history"):
            history = load_history()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "success",
                "history": history,
                "items": history,
                "count": len(history),
            }).encode())
            return

        # 5. Print Status API
        if clean in ("/api/print/status", "/print/status", "/api/status/print"):
            token = os.getenv("SUPERVISOR_TOKEN")
            p_status = "IDLE"
            fname = ""
            pct = 0.0
            elapsed = 0
            remain = 0
            started_at = 0

            if token:
                try:
                    for ent, target in [("sensor.cc2_print_status", "status"), ("sensor.cc2_file_name", "fname"), ("sensor.cc2_percent_complete", "pct"), ("sensor.cc2_current_print_time", "elapsed"), ("sensor.cc2_remaining_print_time", "remain")]:
                        req = urllib.request.Request(f"http://supervisor/core/api/states/{ent}", headers={"Authorization": f"Bearer {token}"})
                        with urllib.request.urlopen(req, timeout=1.0) as resp:
                            data = json.loads(resp.read().decode())
                            val = data.get("state")
                            if val not in ["unknown", "unavailable", None, ""]:
                                if target == "status": p_status = str(val).upper()
                                elif target == "fname": fname = str(val)
                                elif target == "pct": pct = float(val)
                                elif target == "elapsed": elapsed = int(float(val))
                                elif target == "remain": remain = int(float(val))
                except Exception:
                    pass

            is_active = p_status in ("RUNNING", "PRINTING", "PAUSE", "PAUSED", "WORKING", "RESUME", "BUSY")
            history = load_history()
            active_hist = next((h for h in history if h.get("status") == "printing"), None)
            if not fname and active_hist:
                fname = active_hist.get("filename", "")
            if not started_at and active_hist:
                started_at = active_hist.get("started_at", 0)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "active": is_active,
                "status": p_status,
                "filename": fname,
                "started_at": started_at,
                "started_at_formatted": time.strftime("%H:%M:%S", time.localtime(started_at)) if started_at else "",
                "elapsed_seconds": elapsed,
                "elapsed_formatted": format_duration_hms(elapsed),
                "remaining_seconds": remain,
                "remaining_formatted": format_duration(remain),
                "percent": pct,
                "plate": (active_hist or {}).get("plate", "A"),
                "filament": (active_hist or {}).get("filament", "PLA"),
            }).encode())
            return

        # 6. Thumbnail API
        if clean in ("/api/thumbnail", "/thumbnail"):
            parsed_url = urlparse(self.path)
            query_params = parse_qs(parsed_url.query)
            requested_file = query_params.get("file", [""])[0]

            data = None
            if requested_file:
                base_name = re.sub(r"\.(stl|3mf|gcode)$", "", requested_file, flags=re.IGNORECASE)
                # Check persistent and temporary disk cache
                candidates = [
                    Path(f"/config/www/cc2_kiosk/thumbnails/{base_name}.png"),
                    Path(f"/tmp/cc2_output/{base_name}.png"),
                    Path(f"/tmp/uploads/{base_name}.png"),
                    Path(f"/tmp/orca_out/{base_name}.png"),
                ]
                for cand in candidates:
                    if cand.exists() and cand.is_file():
                        try:
                            data = cand.read_bytes()
                            break
                        except Exception:
                            pass

                # If not cached as PNG, try extracting from sliced G-code
                if not data:
                    for gc in [
                        OUTPUT_DIR / f"{base_name}.gcode",
                        OUTPUT_DIR / requested_file,
                        UPLOAD_DIR / f"{base_name}.gcode",
                    ]:
                        data = extract_thumbnail_from_gcode(gc)
                        if data:
                            save_cached_thumbnail(base_name, data)
                            break

                # If still not found, try extracting from .3mf project file
                if not data:
                    for mf in [
                        UPLOAD_DIR / f"{base_name}.3mf",
                        UPLOAD_DIR / requested_file,
                    ]:
                        if mf.exists() and mf.is_file():
                            data = extract_thumbnail(mf)
                            if data:
                                save_cached_thumbnail(base_name, data)
                                break

                # If still not found, fetch live from CC2 printer storage via SDCP method 1045
                if not data:
                    printer_thumb = fetch_thumbnail_from_printer(requested_file)
                    if printer_thumb:
                        data = printer_thumb
                        save_cached_thumbnail(base_name, data)

            # Fallback to last_thumbnail ONLY if NO specific file was requested!
            if not data and not requested_file:
                for cand in [
                    Path("/config/www/cc2_kiosk/thumbnails/last_thumbnail.png"),
                    Path("/tmp/orca_out/last_thumbnail.png"),
                    Path("/tmp/uploads/last_thumbnail.png"),
                ]:
                    if cand.exists() and cand.is_file():
                        try:
                            data = cand.read_bytes()
                            break
                        except Exception:
                            pass

            if data:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=300")
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "No thumbnail available"}).encode())
            return

        # 7. Printer Files API
        if clean in ("/api/files", "/files"):
            files = get_cc2_files_direct()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"files": files}).encode())
            return

        # 8. Live MJPEG Camera Stream
        if clean in ("/api/camera", "/camera", "/api/camera.mjpg", "/camera.mjpg"):
            try:
                stream_url = f"http://{PRINTER_IP}:8080/?action=stream"
                if ACCESS_CODE:
                    stream_url += f"&access_code={ACCESS_CODE}"
                req = urllib.request.Request(stream_url)
                with urllib.request.urlopen(req, timeout=5) as stream:
                    self.send_response(200)
                    self.send_header("Content-Type", stream.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=frame"))
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.send_header("Pragma", "no-cache")
                    self.end_headers()
                    while True:
                        chunk = stream.read(4096)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                try:
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode())
                except Exception:
                    pass
            return

        # 9. Still Camera Snapshot
        if clean in ("/api/snapshot", "/snapshot"):
            try:
                snap_url = f"http://{PRINTER_IP}:8080/?action=stream"
                if ACCESS_CODE:
                    snap_url += f"&access_code={ACCESS_CODE}"
                req = urllib.request.Request(snap_url)
                with urllib.request.urlopen(req, timeout=5) as stream:
                    buf = bytearray()
                    while True:
                        chunk = stream.read(4096)
                        if not chunk:
                            break
                        buf.extend(chunk)
                        start = buf.find(b"\xff\xd8")
                        if start != -1:
                            end = buf.find(b"\xff\xd9", start)
                            if end != -1:
                                jpeg = bytes(buf[start : end + 2])
                                self.send_response(200)
                                self.send_header("Content-Type", "image/jpeg")
                                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                                self.end_headers()
                                self.wfile.write(jpeg)
                                return
            except Exception as e:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        clean = self._normalize_path()

        # 1. Slice and Upload
        if clean in ("/api/upload", "/upload", "/api/slice", "/slice"):
            content_type = self.headers.get("Content-Type", "")
            content_length = int(self.headers.get("Content-Length", 0))

            if not content_type.startswith("multipart/form-data"):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Expected multipart/form-data"}).encode())
                return

            boundary = content_type.split("boundary=")[1].strip().encode()
            body = self.rfile.read(content_length)

            parts = body.split(b"--" + boundary)
            file_data = None
            filename = "model.stl"

            for part in parts:
                if b"Content-Disposition:" in part and b"filename=" in part:
                    header_end = part.find(b"\r\n\r\n")
                    if header_end != -1:
                        headers_raw = part[:header_end].decode("utf-8", errors="replace")
                        m = re.search(r'filename="([^"]+)"', headers_raw)
                        if m:
                            filename = m.group(1)
                        file_data = part[header_end + 4 :].rstrip(b"\r\n")
                        break

            if not file_data:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "No file uploaded"}).encode())
                return

            upload_dir = Path("/tmp/uploads")
            upload_dir.mkdir(parents=True, exist_ok=True)
            saved_path = upload_dir / filename
            with open(saved_path, "wb") as f:
                f.write(file_data)

            try:
                if saved_path.suffix.lower() == ".gcode":
                    res = process_gcode_upload(saved_path)
                else:
                    res = slice_and_upload(saved_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(res).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        # 2. Queue Operations
        if clean in ("/api/queue", "/queue"):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode())
            except Exception:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
                return

            action = data.get("action")
            queue = load_queue()

            if action == "add":
                fname = data.get("filename")
                est = data.get("estimated_seconds")
                filament = data.get("filament", "PLA")
                plate = data.get("plate", "A")
                slot = data.get("slot")
                tray_id = data.get("tray_id")

                if not est or est in (180, 3600, 5400):
                    cand = OUTPUT_DIR / fname
                    if cand.exists():
                        gcode_est = parse_estimated_time_from_gcode(cand)
                        if gcode_est > 0:
                            est = gcode_est
                if not est:
                    est = parse_time_from_string_or_filename(fname) or 3600

                base_name = re.sub(r"\.(stl|3mf|gcode)$", "", fname, flags=re.IGNORECASE)
                thumb_path = Path(f"/config/www/cc2_kiosk/thumbnails/{base_name}.png")
                if not thumb_path.exists():
                    tdata = fetch_thumbnail_from_printer(fname)
                    if tdata:
                        save_cached_thumbnail(base_name, tdata)

                # Resolve slot & tray_id if not explicitly provided
                if tray_id is None:
                    tray_id, _cid = resolve_tray_and_canvas(slot=slot, filament=filament)
                    if not slot:
                        slot = f"A{tray_id + 1}"

                job = {
                    "id": f"job_{int(time.time() * 1000)}",
                    "filename": fname,
                    "display_name": data.get("display_name", fname),
                    "filament": filament,
                    "slot": slot,
                    "tray_id": tray_id,
                    "plate": plate,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "slice_time_seconds": data.get("slice_time_seconds"),
                    "estimated_seconds": est,
                    "estimated_formatted": format_duration(est),
                    "filesize": data.get("filesize", ""),
                    "status": "queued",
                }
                queue.append(job)
                save_queue(queue)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success", "job": job}).encode())
                return

            elif action in ("delete", "remove"):
                job_id = data.get("id")
                queue = [j for j in queue if j.get("id") != job_id]
                save_queue(queue)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success", "deleted": job_id}).encode())
                return

            elif action == "reorder":
                order = data.get("order", data.get("order_ids", []))
                if isinstance(order, list):
                    job_map = {j["id"]: j for j in queue if "id" in j}
                    reordered = [job_map[jid] for jid in order if jid in job_map]
                    for j in queue:
                        if j.get("id") not in order:
                            reordered.append(j)
                    save_queue(reordered)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "queue": reordered}).encode())
                    return

            elif action == "clear":
                save_queue([])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success", "queue": []}).encode())
                return

            elif action in ("start", "start_next"):
                job_id = data.get("id")
                target_job = None
                if job_id:
                    for j in queue:
                        if j.get("id") == job_id:
                            target_job = j
                            break
                elif queue:
                    target_job = queue[0]

                if not target_job:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "No job found to start"}).encode())
                    return

                target_file = target_job.get("filename")
                target_auto_level = target_job.get("auto_level", True)
                target_plate = target_job.get("plate", "A")
                target_slot = target_job.get("slot")
                target_tray_id = target_job.get("tray_id")
                target_filament = target_job.get("filament", "PLA")

                ok, msg = execute_cc2_start_print(
                    target_file,
                    plate=target_plate,
                    slot=target_slot,
                    tray_id=target_tray_id,
                    filament=target_filament,
                    auto_leveling=target_auto_level,
                )
                if ok:
                    # Move to history
                    queue = [j for j in queue if j.get("id") != target_job.get("id")]
                    save_queue(queue)

                    history = load_history()
                    for h in history:
                        if h.get("status") == "printing":
                            h["status"] = "completed"
                            h["completed_at"] = time.time()
                            if h.get("started_at"):
                                h["duration_seconds"] = int(h["completed_at"] - h["started_at"])
                                h["duration_formatted"] = format_duration(h["duration_seconds"])

                    est_sec = target_job.get("estimated_seconds", 0) or parse_time_from_string_or_filename(target_file)
                    history_entry = {
                        "id": target_job.get("id"),
                        "filename": target_file,
                        "display_name": target_job.get("display_name", target_file),
                        "filament": target_filament,
                        "slot": target_slot or (f"A{target_tray_id + 1}" if target_tray_id is not None else "A1"),
                        "tray_id": target_tray_id,
                        "plate": target_plate,
                        "auto_level": target_auto_level,
                        "started_at": time.time(),
                        "started_at_formatted": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "completed_at": None,
                        "estimated_seconds": est_sec,
                        "estimated_formatted": format_duration(est_sec),
                        "status": "printing",
                    }
                    history.insert(0, history_entry)
                    save_history(history)

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "started": target_job.get("id"), "job": history_entry, "message": msg}).encode())
                    return
                else:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "error": msg}).encode())
                    return

            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Unknown action '{action}'"}).encode())
            return

        # 3. Print Confirm Start
        if clean in ("/api/print/confirm_start", "/print/confirm_start", "/api/confirm_start"):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode())
            except Exception:
                data = {}

            filename = data.get("filename")
            plate = data.get("plate_type") or data.get("plate") or "A"
            filament = data.get("filament", "PLA")
            slot = data.get("slot")
            tray_id = data.get("tray_id")
            raw_auto_level = data.get("auto_level")
            if raw_auto_level is not None:
                auto_level = False if str(raw_auto_level).lower() in ("false", "0") else bool(raw_auto_level)
            else:
                auto_level = True
            queue_id = data.get("queue_id")

            queue = load_queue()
            target_job = None
            if queue_id:
                for j in queue:
                    if j.get("id") == queue_id:
                        target_job = j
                        break
            elif filename:
                for j in queue:
                    if j.get("filename") == filename:
                        target_job = j
                        break

            if target_job:
                filename = target_job.get("filename")
                if "auto_level" in target_job and raw_auto_level is None:
                    job_al = target_job.get("auto_level")
                    auto_level = False if str(job_al).lower() in ("false", "0") else bool(job_al)
                if not slot and target_job.get("slot"):
                    slot = target_job.get("slot")
                if tray_id is None and target_job.get("tray_id") is not None:
                    tray_id = target_job.get("tray_id")
                queue = [j for j in queue if j.get("id") != target_job.get("id")]
                save_queue(queue)

            if not filename:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing filename"}).encode())
                return

            ok, msg = execute_cc2_start_print(
                filename,
                plate=plate,
                slot=slot,
                tray_id=tray_id,
                filament=filament,
                auto_leveling=auto_level,
            )

            history = load_history()
            for h in history:
                if h.get("status") == "printing":
                    h["status"] = "completed"
                    h["completed_at"] = time.time()
                    if h.get("started_at"):
                        h["duration_seconds"] = int(h["completed_at"] - h["started_at"])
                        h["duration_formatted"] = format_duration(h["duration_seconds"])

            est_sec = (target_job.get("estimated_seconds") if target_job else 0) or parse_time_from_string_or_filename(filename)
            history_entry = {
                "id": (target_job.get("id") if target_job else f"job_{int(time.time() * 1000)}"),
                "filename": filename,
                "display_name": (target_job.get("display_name") if target_job else filename),
                "filament": filament,
                "slot": slot or (f"A{tray_id + 1}" if tray_id is not None else "A1"),
                "tray_id": tray_id,
                "plate": plate,
                "auto_level": auto_level,
                "started_at": time.time(),
                "started_at_formatted": time.strftime("%Y-%m-%d %H:%M:%S"),
                "completed_at": None,
                "estimated_seconds": est_sec,
                "estimated_formatted": format_duration(est_sec),
                "status": "printing" if ok else "failed",
            }
            if ok:
                history.insert(0, history_entry)
                save_history(history)

            self.send_response(200 if ok else 500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok" if ok else "error",
                "message": msg,
                "job": history_entry
            }).encode())
            return

        # 4. Print Start
        if clean in ("/api/print/start", "/print/start", "/api/print", "/print"):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode()) if body else {}
                filename = data.get("filename")
                if not filename:
                    raise ValueError("Missing filename")
                plate = data.get("plate_type") or data.get("plate") or "A"
                filament = data.get("filament")
                slot = data.get("slot")
                tray_id = data.get("tray_id")
                raw_auto_level = data.get("auto_level")
                if raw_auto_level is not None:
                    auto_level = False if str(raw_auto_level).lower() in ("false", "0") else bool(raw_auto_level)
                else:
                    auto_level = True
                timelapse = bool(data.get("timelapse", False))

                ok, msg = execute_cc2_start_print(
                    filename,
                    plate=plate,
                    slot=slot,
                    tray_id=tray_id,
                    filament=filament,
                    auto_leveling=auto_level,
                    timelapse=timelapse,
                )
                if ok:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "filename": filename, "message": msg}).encode())
                else:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "error": msg}).encode())
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        # 5. Stop Print
        if clean in ("/api/print/stop", "/print/stop", "/api/stop", "/stop"):
            cmd = [
                "centauri", "print", "stop",
                "--host", PRINTER_IP, "--access-code", ACCESS_CODE
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success" if res.returncode == 0 else "error", "output": res.stdout}).encode())
            return

        self.send_response(404)
        self.end_headers()


def run():
    server_address = ("", PORT)
    httpd = HTTPServer(server_address, SlicerHTTPHandler)
    print(f"[*] CC2 Slicer & Kiosk Microservice running on port {PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    run()
