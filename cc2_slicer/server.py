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
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
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

        # Ensure upload.py supports UTF-8 filenames without crashing httpx ascii header validation
        try:
            import pycentauri.upload as up_mod
            p_up = Path(up_mod.__file__)
            t_up = p_up.read_text(encoding="utf-8")
            new_t_up = t_up.replace('X-File-Name: name.encode(utf-8),', '"X-File-Name": name.encode("utf-8"),')
            new_t_up = re.sub(r'"X-File-Name":\s*name,', '"X-File-Name": name.encode("utf-8"),', new_t_up)
            if new_t_up != t_up:
                p_up.write_text(new_t_up, encoding="utf-8")
                print("[+] Patched pycentauri/upload.py for UTF-8 filename support", flush=True)
        except Exception as e_up:
            print(f"[!] Warning patching pycentauri upload: {e_up}", flush=True)
    except Exception as e:
        print(f"[!] Warning checking/patching pycentauri: {e}", flush=True)


patch_pycentauri_cc2()


def sanitize_filename(name: str) -> str:
    """Normalize tricky punctuation like en-dash, em-dash, and quotes in filenames."""
    if not name:
        return ""
    name = name.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    name = name.replace("\u2018", "'").replace("\u2019", "'")
    name = name.replace("\u201c", '"').replace("\u201d", '"')
    return name.strip()


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


def extract_thumbnail(mf_path: Path, plate_idx: int = 1, thumb_file: str | None = None) -> bytes | None:
    try:
        with zipfile.ZipFile(mf_path) as z:
            namelist = set(z.namelist())
            candidates = []
            if thumb_file:
                candidates.extend([thumb_file, f"Metadata/{thumb_file}"])
            candidates.extend([
                f"Metadata/plate_{plate_idx}.png",
                f"Metadata/plate_{plate_idx}_small.png",
                f"Metadata/plate_{plate_idx}.jpg",
                f"Metadata/plate_{plate_idx}.jpeg",
                f"Metadata/plate_{plate_idx}.webp",
            ])
            if plate_idx == 1:
                candidates.extend([
                    "Metadata/plate_1.png",
                    "Metadata/top_1.png",
                    "Auxiliaries/.thumbnails/thumbnail_middle.png",
                    "Auxiliaries/.thumbnails/thumbnail_3mf.png",
                    "Auxiliaries/.thumbnails/thumbnail_small.png",
                ])
            for cand in candidates:
                if cand in namelist:
                    data = z.read(cand)
                    img = Image.open(io.BytesIO(data))
                    img.thumbnail((300, 300), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    return buf.getvalue()
            # If still not found and plate_idx > 1, allow general fallback
            fallback_candidates = [
                "Metadata/plate_1.png",
                "Metadata/top_1.png",
                "Auxiliaries/.thumbnails/thumbnail_middle.png",
                "Auxiliaries/.thumbnails/thumbnail_3mf.png",
                "Auxiliaries/.thumbnails/thumbnail_small.png",
            ]
            for cand in fallback_candidates:
                if cand in namelist:
                    data = z.read(cand)
                    img = Image.open(io.BytesIO(data))
                    img.thumbnail((300, 300), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    return buf.getvalue()
            # Try any picture in Auxiliaries/Model Pictures/
            for name in namelist:
                if name.startswith("Auxiliaries/Model Pictures/") and name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    data = z.read(name)
                    img = Image.open(io.BytesIO(data))
                    img.thumbnail((300, 300), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    return buf.getvalue()
    except Exception as e:
        print(f"[!] Thumbnail extraction error: {e}", flush=True)
    return None


def extract_3mf_title(archive_path: Path) -> str | None:
    """Extract model title from 3MF project metadata or auxiliaries."""
    try:
        with zipfile.ZipFile(archive_path, "r") as z:
            # 1. Inspect Metadata/model_settings.config (Bambu / OrcaSlicer project)
            if "Metadata/model_settings.config" in z.namelist():
                xml_data = z.read("Metadata/model_settings.config")
                root = ET.fromstring(xml_data)
                for obj in root.findall(".//object"):
                    for meta in obj.findall("metadata"):
                        if meta.get("key") == "name" and meta.get("value"):
                            val = meta.get("value").strip()
                            val = re.sub(r"\.(stl|3mf|smf|gcode)$", "", val, flags=re.IGNORECASE).strip()
                            if val and not val.lower().startswith("plate_") and val.lower() not in ("default", "model"):
                                return val
                for part in root.findall(".//part"):
                    for meta in part.findall("metadata"):
                        if meta.get("key") == "name" and meta.get("value"):
                            val = meta.get("value").strip()
                            val = re.sub(r"\.(stl|3mf|smf|gcode)$", "", val, flags=re.IGNORECASE).strip()
                            if val and not val.lower().startswith("plate_") and val.lower() not in ("default", "model"):
                                return val
            # 2. Inspect Auxiliaries/Model Pictures/*.webp
            for name in z.namelist():
                if name.startswith("Auxiliaries/Model Pictures/") and not name.endswith("/"):
                    cand = name.split("/")[-1]
                    cand = re.sub(r"\.(webp|png|jpg|jpeg)$", "", cand, flags=re.IGNORECASE).strip()
                    if cand and not cand.isdigit() and len(cand) > 2:
                        return cand
    except Exception as e:
        print(f"[!] Warning extracting 3MF title: {e}", flush=True)
    return None


def get_3mf_plates(archive_path: Path) -> list[dict[str, Any]]:
    """Extract all plate configurations and names from a 3MF project file."""
    plates = []
    try:
        with zipfile.ZipFile(archive_path, "r") as z:
            if "Metadata/model_settings.config" in z.namelist():
                xml_data = z.read("Metadata/model_settings.config")
                root = ET.fromstring(xml_data)
                for pl in root.findall(".//plate"):
                    pid = None
                    pname = ""
                    pthumb = ""
                    for m in pl.findall("metadata"):
                        k = m.get("key")
                        v = m.get("value", "")
                        if k == "plater_id":
                            pid = int(v) if v.isdigit() else v
                        elif k == "plater_name":
                            pname = v.strip()
                        elif k == "thumbnail_file":
                            pthumb = v.strip()
                    if pid is not None:
                        plates.append({"id": pid, "name": pname, "thumbnail": pthumb})
    except Exception as e:
        print(f"[!] Warning reading 3MF plates: {e}", flush=True)
    return plates


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
    if file_path.suffix.lower() not in (".3mf", ".smf"):
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
    base_name = sanitize_filename(re.sub(r"\.(stl|3mf|smf)$", "", filename, flags=re.IGNORECASE))

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
        "--allow-newer-file",
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

    gcode_files = sorted(
        list(tmp_slice_dir.glob("*.gcode")),
        key=lambda p: int(re.search(r"plate_(\d+)", p.name).group(1)) if re.search(r"plate_(\d+)", p.name) else 0
    )
    if not gcode_files:
        raise RuntimeError(f"No .gcode generated in {tmp_slice_dir}")

    plates_meta = get_3mf_plates(input_path) if input_path.suffix.lower() in (".3mf", ".smf") else []
    is_multi_plate = len(gcode_files) > 1

    plates_result = []
    for idx, gcode_file in enumerate(gcode_files):
        m_idx = re.search(r"plate_(\d+)", gcode_file.name)
        plate_num = int(m_idx.group(1)) if m_idx else (idx + 1)

        pm = next((p for p in plates_meta if p.get("id") == plate_num), {})
        pname = sanitize_filename(pm.get("name", "").strip())

        if is_multi_plate:
            final_gcode_name = f"{base_name}_plate_{plate_num}.gcode"
            if pname and pname.lower() != base_name.lower():
                disp_name = f"{base_name} - {pname}"
            else:
                disp_name = f"{base_name} - Plate {plate_num}"
        else:
            final_gcode_name = f"{base_name}.gcode"
            disp_name = base_name

        final_gcode = OUTPUT_DIR / final_gcode_name
        if final_gcode.exists():
            try:
                final_gcode.unlink()
            except Exception:
                pass
        gcode_file.rename(final_gcode)

        # Parse estimated print time
        est_seconds = parse_estimated_time_from_gcode(final_gcode) or parse_time_from_string_or_filename(final_gcode_name) or 3600

        # Extract thumbnail if 3MF
        thumb_png = None
        if input_path.suffix.lower() in (".3mf", ".smf"):
            thumb_png = extract_thumbnail(input_path, plate_idx=plate_num, thumb_file=pm.get("thumbnail"))
            if thumb_png:
                inject_gcode_thumbnails(final_gcode, thumb_png)
                # Cache thumbnail for this plate
                save_cached_thumbnail(final_gcode.stem, thumb_png)
                if is_multi_plate:
                    save_cached_thumbnail(f"{base_name}_plate_{plate_num}", thumb_png)
                else:
                    save_cached_thumbnail(base_name, thumb_png)

        # Upload to CC2 printer via pycentauri CLI (non-fatal if printer is printing/busy)
        up_cmd = [
            "centauri", "upload", str(final_gcode),
            "--host", PRINTER_IP,
            "--access-code", ACCESS_CODE,
            "--enable-control",
        ]
        print(f"[*] Uploading {final_gcode.name} to CC2 at {PRINTER_IP}...", flush=True)
        up_proc = subprocess.run(up_cmd, capture_output=True, text=True, timeout=120)
        upload_ok = (up_proc.returncode == 0)
        if not upload_ok:
            print(f"[!] Note: CC2 file upload postponed for {final_gcode.name} (printer busy): {up_proc.stderr}", flush=True)

        plates_result.append({
            "plate_id": plate_num,
            "plate_name": pname,
            "display_name": disp_name,
            "gcode": final_gcode.name,
            "filename": final_gcode.name,
            "slice_time": slice_time,
            "slice_time_seconds": slice_time,
            "estimated_seconds": est_seconds,
            "estimated_formatted": format_duration(est_seconds),
            "size_bytes": final_gcode.stat().st_size,
            "uploaded": upload_ok,
            "thumbnail_b64": base64.b64encode(thumb_png).decode("ascii") if thumb_png else None,
        })

    first = plates_result[0]
    result = {
        "status": "success",
        "is_multi_plate": is_multi_plate,
        "plates": plates_result,
        "gcode": first["gcode"],
        "filename": first["filename"],
        "slice_time": slice_time,
        "slice_time_seconds": slice_time,
        "estimated_seconds": first["estimated_seconds"],
        "estimated_formatted": first["estimated_formatted"],
        "size_bytes": first["size_bytes"],
        "uploaded": first["uploaded"],
        "printer_ip": PRINTER_IP,
    }
    if first.get("thumbnail_b64"):
        result["thumbnail_b64"] = first["thumbnail_b64"]
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


COLOR_NAMES_MAP = {
    "Black": (0, 0, 0),
    "White": (255, 255, 255),
    "Grey": (128, 128, 128),
    "Silver": (192, 192, 192),
    "Red": (220, 20, 60),
    "Coral / Pink": (249, 93, 119),
    "Orange": (255, 140, 0),
    "Yellow": (255, 242, 66),
    "Green": (34, 139, 34),
    "Lime": (50, 205, 50),
    "Blue": (30, 144, 255),
    "Navy": (0, 0, 128),
    "Purple": (128, 0, 128),
    "Cyan": (0, 206, 209),
    "Brown": (139, 69, 19),
}


def approx_color_name(hex_code: str | None) -> str:
    """Find the closest descriptive color name from a HEX string."""
    if not hex_code or not str(hex_code).startswith("#") or len(str(hex_code)) < 7:
        return ""
    try:
        h = str(hex_code).lstrip("#")
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
        best_name = "Color"
        min_dist = float("inf")
        for name, (cr, cg, cb) in COLOR_NAMES_MAP.items():
            dist = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
            if dist < min_dist:
                min_dist = dist
                best_name = name
        return best_name
    except Exception:
        return "Color"


_ams_cache: dict[str, Any] = {"ts": 0.0, "details": {}}


def get_ams_details(force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """
    Fetch comprehensive AMS (Canvas) slot details including filament name, type, brand, and HEX color.
    Uses Home Assistant Supervisor REST API with a 3-second cache.
    """
    global _ams_cache
    now = time.time()
    if not force_refresh and (now - _ams_cache.get("ts", 0)) < 3.0 and _ams_cache.get("details"):
        return _ams_cache["details"]

    token = os.getenv("SUPERVISOR_TOKEN")
    details: dict[str, dict[str, Any]] = {}

    if token:
        try:
            req = urllib.request.Request(
                "http://supervisor/core/api/states",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                all_states = {s["entity_id"]: s for s in json.loads(resp.read().decode())}

            active_tray = all_states.get("sensor.cc2_active_tray_id", {}).get("state")
            active_color = all_states.get("sensor.cc2_active_filament_color", {}).get("state")

            for i, slot_name in enumerate(["a1", "a2", "a3", "a4"]):
                slot_key = slot_name.upper()
                name_state = all_states.get(f"sensor.cc2_{slot_name}_name", {}).get("state")
                color_state = all_states.get(f"sensor.cc2_{slot_name}_color", {}).get("state")
                attr_data = all_states.get(f"sensor.cc2_{slot_name}_attributes", {}).get("attributes", {})

                is_empty = name_state in ["unknown", "unavailable", None, "Empty", "empty"]
                name = "Empty" if is_empty else str(name_state).strip()
                color = str(color_state).strip() if color_state and str(color_state).startswith("#") else "#808080"
                c_name = approx_color_name(color) if not is_empty else ""

                is_active = False
                if not is_empty:
                    if active_color and color and str(active_color).upper() == color.upper():
                        is_active = True
                    elif active_tray is not None and str(active_tray).isdigit() and int(active_tray) == i:
                        is_active = True

                details[slot_key] = {
                    "slot": slot_key,
                    "tray_id": i,
                    "canvas_id": 0,
                    "name": name,
                    "type": attr_data.get("type") or (name.split()[0] if not is_empty else "None"),
                    "color": color,
                    "color_name": c_name,
                    "brand": attr_data.get("brand", "ELEGOO") if not is_empty else "",
                    "temp_range": attr_data.get("nozzle_temp_range", ""),
                    "is_active": is_active,
                    "is_loaded": not is_empty,
                }

            if any(v["is_loaded"] for v in details.values()):
                _ams_cache = {"ts": now, "details": details}
                return details
        except Exception as e:
            print(f"[!] Warning reading AMS details from supervisor: {e}", flush=True)

    # Fallback defaults if supervisor is unreachable
    defaults = {
        "A1": {"name": "PLA", "color": "#FFF242", "color_name": "Yellow"},
        "A2": {"name": "PLA", "color": "#F95D77", "color_name": "Coral / Pink"},
        "A3": {"name": "PLA PRO", "color": "#898989", "color_name": "Grey"},
        "A4": {"name": "RAPID PLA+", "color": "#000000", "color_name": "Black"},
    }
    fallback_details = {}
    for i, (k, d) in enumerate(defaults.items()):
        fallback_details[k] = {
            "slot": k,
            "tray_id": i,
            "canvas_id": 0,
            "name": d["name"],
            "type": "PLA",
            "color": d["color"],
            "color_name": d["color_name"],
            "brand": "ELEGOO",
            "temp_range": "190-230°C",
            "is_active": (i == 1),
            "is_loaded": True,
        }
    return fallback_details


def get_ams_slots() -> dict[str, str]:
    """Returns slot name map e.g. {'A1': 'PLA', 'A2': 'PLA'} for backwards compatibility."""
    details = get_ams_details()
    return {k: v["name"] for k, v in details.items()}


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


def add_job_to_queue(
    filename: str,
    est_seconds: int | None = None,
    filament: str = "PLA",
    plate: str = "A",
    slot: str | None = None,
    tray_id: int | None = None,
    slice_time_seconds: float | None = None,
    display_name: str | None = None,
    auto_level: bool = True,
    filesize: str = "",
) -> dict[str, Any]:
    """Helper to construct, enrich, and append a print job to the persistent queue."""
    queue = load_queue()
    est = est_seconds
    if not est or est in (180, 3600, 5400):
        cand = OUTPUT_DIR / filename
        if cand.exists():
            gcode_est = parse_estimated_time_from_gcode(cand)
            if gcode_est > 0:
                est = gcode_est
    if not est:
        est = parse_time_from_string_or_filename(filename) or 3600

    base_name = re.sub(r"\.(stl|3mf|smf|gcode)$", "", filename, flags=re.IGNORECASE)
    thumb_path = Path(f"/config/www/cc2_kiosk/thumbnails/{base_name}.png")
    if not thumb_path.exists():
        tdata = fetch_thumbnail_from_printer(filename)
        if tdata:
            save_cached_thumbnail(base_name, tdata)

    # Resolve slot & tray_id if not explicitly provided
    if tray_id is None:
        tray_id, _cid = resolve_tray_and_canvas(slot=slot, filament=filament)
        if not slot:
            slot = f"A{tray_id + 1}"

    job = {
        "id": f"job_{int(time.time() * 1000)}",
        "filename": filename,
        "display_name": display_name or filename,
        "filament": filament,
        "slot": slot,
        "tray_id": tray_id,
        "plate": plate,
        "auto_level": auto_level,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "slice_time_seconds": slice_time_seconds,
        "estimated_seconds": est,
        "estimated_formatted": format_duration(est),
        "filesize": filesize,
        "status": "queued",
    }
    queue.append(job)
    save_queue(queue)
    return job


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

    # Ensure file is uploaded to CC2 printer storage if present in local cc2_output
    local_path = OUTPUT_DIR / filename
    if local_path.exists():
        up_cmd = [
            "centauri", "upload", str(local_path),
            "--host", PRINTER_IP,
            "--access-code", ACCESS_CODE,
            "--enable-control",
        ]
        try:
            print(f"[*] Ensuring {filename} is uploaded to CC2 before start...", flush=True)
            up_proc = subprocess.run(up_cmd, capture_output=True, text=True, timeout=120)
            if up_proc.returncode != 0:
                print(f"[!] Warning uploading before print: {up_proc.stderr}", flush=True)
        except Exception as _ue:
            print(f"[!] Upload before print exception: {_ue}", flush=True)

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
            ams_details = get_ams_details()
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

                # Enrich slot color information
                item_slot = (item.get("slot") or "").strip().upper()
                slot_info = ams_details.get(item_slot)
                if not slot_info:
                    matched_slot = item_copy["filament_status"].get("slot")
                    slot_info = ams_details.get(matched_slot) if matched_slot else None
                if slot_info:
                    item_copy["slot_color"] = slot_info.get("color", "#808080")
                    item_copy["slot_color_name"] = slot_info.get("color_name", "")
                    item_copy["slot_brand"] = slot_info.get("brand", "")
                else:
                    item_copy["slot_color"] = "#808080"
                    item_copy["slot_color_name"] = ""
                    item_copy["slot_brand"] = ""

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
                "ams_details": ams_details,
                "count": len(enhanced),
                "total_eta_seconds": total_eta,
                "total_eta_formatted": format_duration(total_eta),
            }).encode())
            return

        # 3b. AMS Slots & Colors API
        if clean in ("/api/slots", "/slots", "/api/ams", "/ams"):
            details = get_ams_details(force_refresh=True)
            active_slot = next((k for k, v in details.items() if v.get("is_active")), "A2")
            active_tray = details.get(active_slot, {}).get("tray_id", 1)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "success",
                "active_slot": active_slot,
                "active_tray_id": active_tray,
                "slots": details,
                "slots_list": list(details.values()),
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
            if not is_active and active_hist:
                if p_status.upper() in ("IDLE", "COMPLETE", "COMPLETED", "STANDBY", "FINISH", "FINISHED", "STOPPED"):
                    active_hist["status"] = "completed"
                    active_hist["completed_at"] = time.time()
                    if active_hist.get("started_at"):
                        active_hist["duration_seconds"] = int(active_hist["completed_at"] - active_hist["started_at"])
                        active_hist["duration_formatted"] = format_duration(active_hist["duration_seconds"])
                    save_history(history)
                    active_hist = None

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
                    if not data:
                        m_p = re.search(r"^(.*)_plate_(\d+)$", base_name, re.IGNORECASE)
                        if m_p:
                            parent_mf = UPLOAD_DIR / f"{m_p.group(1)}.3mf"
                            if parent_mf.exists() and parent_mf.is_file():
                                data = extract_thumbnail(parent_mf, plate_idx=int(m_p.group(2)))
                                if data:
                                    save_cached_thumbnail(base_name, data)

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
            parsed_url = urlparse(self.path)
            qparams = parse_qs(parsed_url.query)

            raw_auto_queue = qparams.get("auto_queue", [""])[0] or self.headers.get("X-Auto-Queue", "")
            auto_queue = raw_auto_queue.lower() in ("1", "true", "yes", "on")

            param_filament = qparams.get("filament", [""])[0] or self.headers.get("X-Filament", "")
            param_plate = qparams.get("plate", [""])[0] or self.headers.get("X-Plate", "")
            param_slot = qparams.get("slot", [""])[0] or self.headers.get("X-Slot", "")
            raw_auto_level = qparams.get("auto_level", [""])[0] or self.headers.get("X-Auto-Level", "")
            auto_level = False if raw_auto_level.lower() in ("0", "false", "no") else True

            filename = (
                qparams.get("filename", [""])[0]
                or self.headers.get("X-Filename", "")
                or ""
            )
            cd = self.headers.get("Content-Disposition", "")
            if not filename and cd:
                m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
                if m:
                    filename = m.group(1).strip()

            content_type = self.headers.get("Content-Type", "")
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

            file_data = None

            if content_type.startswith("multipart/form-data"):
                boundary_match = re.search(r'boundary=([^;]+)', content_type)
                if boundary_match:
                    boundary = boundary_match.group(1).strip().strip('"').encode()
                    parts = body.split(b"--" + boundary)
                    for part in parts:
                        if b"Content-Disposition:" in part:
                            header_end = part.find(b"\r\n\r\n")
                            if header_end != -1:
                                headers_raw = part[:header_end].decode("utf-8", errors="replace")
                                part_body = part[header_end + 4 :].rstrip(b"\r\n")

                                m_fn = re.search(r'filename="([^"]+)"', headers_raw)
                                if m_fn:
                                    if not filename or filename == "model.stl":
                                        filename = m_fn.group(1)
                                    file_data = part_body
                                else:
                                    m_name = re.search(r'name="([^"]+)"', headers_raw)
                                    if m_name:
                                        f_name = m_name.group(1)
                                        f_val = part_body.decode("utf-8", errors="replace").strip()
                                        if f_name == "auto_queue" and f_val.lower() in ("1", "true", "yes", "on"):
                                            auto_queue = True
                                        elif f_name == "filament" and f_val and not param_filament:
                                            param_filament = f_val
                                        elif f_name == "plate" and f_val and not param_plate:
                                            param_plate = f_val
                                        elif f_name == "slot" and f_val and not param_slot:
                                            param_slot = f_val
                                        elif f_name == "auto_level" and f_val.lower() in ("0", "false", "no"):
                                            auto_level = False
            else:
                # Raw binary upload
                file_data = body

            if not file_data:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "No file uploaded or file is empty"}).encode())
                return

            # Format detection via magic bytes
            is_3mf = file_data.startswith(b"PK\x03\x04")
            is_gcode = (
                file_data.startswith(b";")
                or b"\nM104" in file_data[:2000]
                or b"\nG1 " in file_data[:2000]
                or b"\nG0 " in file_data[:2000]
                or b"; generated by" in file_data[:2000]
                or b"; G-Code generated" in file_data[:2000]
            )
            is_stl = not is_3mf and not is_gcode

            upload_dir = Path("/tmp/uploads")
            upload_dir.mkdir(parents=True, exist_ok=True)

            # Determine clean filename and display_name
            raw_fn = sanitize_filename(Path(filename).name.strip()) if filename else ""
            is_generic = not raw_fn or raw_fn.lower() in (
                "model.stl", "model.3mf", "model.gcode", "model",
                "shortcut input.stl", "shortcut input.3mf", "shortcut input",
                "file.stl", "file.3mf", "file", "upload.stl", "upload.3mf"
            )

            display_name = raw_fn

            if is_3mf:
                temp_inspect = upload_dir / f"tmp_inspect_{int(time.time()*1000)}.3mf"
                temp_inspect.write_bytes(file_data)
                extracted_title = extract_3mf_title(temp_inspect)
                temp_inspect.unlink(missing_ok=True)

                if is_generic and extracted_title:
                    clean_title = sanitize_filename(extracted_title)
                    filename = f"{clean_title}.3mf"
                    display_name = clean_title
                elif is_generic:
                    filename = f"model_{int(time.time())}.3mf"
                    display_name = "3D Model"
                else:
                    clean_stem = sanitize_filename(re.sub(r"\.(stl|3mf|smf|gcode)$", "", raw_fn, flags=re.IGNORECASE).strip())
                    filename = f"{clean_stem}.3mf"
                    display_name = clean_stem

            elif is_gcode:
                if is_generic:
                    m_gc = re.search(rb';\s*(?:model|filename|title|source)\s*:\s*([^\r\n]+)', file_data[:4096], re.IGNORECASE)
                    if m_gc:
                        g_title = m_gc.group(1).decode("utf-8", errors="ignore").strip()
                        g_title = re.sub(r"\.(stl|3mf|smf|gcode)$", "", g_title, flags=re.IGNORECASE).strip()
                        filename = f"{g_title}.gcode" if g_title else f"model_{int(time.time())}.gcode"
                        display_name = g_title or "G-code Job"
                    else:
                        filename = f"model_{int(time.time())}.gcode"
                        display_name = "G-code Job"
                else:
                    clean_stem = sanitize_filename(re.sub(r"\.(stl|3mf|smf|gcode)$", "", raw_fn, flags=re.IGNORECASE).strip())
                    filename = f"{clean_stem}.gcode"
                    display_name = clean_stem

            else:
                # STL
                if is_generic:
                    if file_data.startswith(b"solid "):
                        hdr_name = file_data[:80].decode("utf-8", errors="ignore").split("\n")[0][6:].strip()
                        if hdr_name and hdr_name.lower() not in ("default", "model", "ascii", "solid", "openscad_model"):
                            clean_hdr = sanitize_filename(hdr_name)
                            filename = f"{clean_hdr}.stl"
                            display_name = clean_hdr
                        else:
                            filename = f"model_{int(time.time())}.stl"
                            display_name = "3D Model"
                    else:
                        filename = f"model_{int(time.time())}.stl"
                        display_name = "3D Model"
                else:
                    clean_stem = sanitize_filename(re.sub(r"\.(stl|3mf|smf|gcode)$", "", raw_fn, flags=re.IGNORECASE).strip())
                    filename = f"{clean_stem}.stl"
                    display_name = clean_stem

            saved_path = upload_dir / filename
            with open(saved_path, "wb") as f:
                f.write(file_data)

            # If G-code: process immediately (fast, ~0.2s)
            if saved_path.suffix.lower() == ".gcode":
                try:
                    res = process_gcode_upload(saved_path)
                    if auto_queue:
                        gcode_fname = res.get("gcode") or res.get("filename") or filename
                        est_sec = res.get("estimated_seconds") or 3600
                        job = add_job_to_queue(
                            filename=gcode_fname,
                            est_seconds=est_sec,
                            filament=param_filament or "PLA",
                            plate=param_plate or "A",
                            slot=param_slot or None,
                            auto_level=auto_level,
                            slice_time_seconds=res.get("slice_time") or res.get("slice_time_seconds"),
                            display_name=display_name,
                            filesize=f"{round(len(file_data) / (1024*1024), 2)} MB",
                        )
                        res["queued"] = True
                        res["job"] = job
                        res["message"] = f"File {display_name} processed and added to print queue successfully!"

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(res).encode())
                    return
                except Exception as e:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode())
                    return

            # If 3MF or STL:
            if auto_queue:
                base_name = re.sub(r"\.(stl|3mf|smf)$", "", filename, flags=re.IGNORECASE)

                plates_meta = get_3mf_plates(saved_path) if is_3mf else []
                is_multi_plate = len(plates_meta) > 1

                resolved_tray, _ = resolve_tray_and_canvas(slot=param_slot, filament=param_filament)
                resolved_slot = param_slot or f"A{resolved_tray + 1}"

                # Pre-extract thumbnail(s) if 3MF
                if is_3mf:
                    try:
                        if is_multi_plate:
                            for pm in plates_meta:
                                pid = pm["id"]
                                tdata = extract_thumbnail(saved_path, plate_idx=pid, thumb_file=pm.get("thumbnail"))
                                if tdata:
                                    save_cached_thumbnail(f"{base_name}_plate_{pid}", tdata)
                        else:
                            thumb_data = extract_thumbnail(saved_path)
                            if thumb_data:
                                save_cached_thumbnail(base_name, thumb_data)
                    except Exception as e:
                        print(f"[!] Warning caching thumbnail for {filename}: {e}", flush=True)

                initial_jobs = []
                now_ms = int(time.time() * 1000)

                if is_multi_plate:
                    for pm in plates_meta:
                        pid = pm["id"]
                        pname = pm.get("name", "").strip()
                        if pname and pname.lower() != base_name.lower():
                            pdisp = f"{display_name} - {pname}"
                        else:
                            pdisp = f"{display_name} - Plate {pid}"
                        p_job_id = f"job_{now_ms}_{pid}"
                        p_job = {
                            "id": p_job_id,
                            "plate_id": pid,
                            "filename": f"{base_name}_plate_{pid}.gcode",
                            "display_name": pdisp,
                            "filament": param_filament or "PLA",
                            "slot": resolved_slot,
                            "tray_id": resolved_tray,
                            "plate": param_plate or "A",
                            "auto_level": auto_level,
                            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "slice_time_seconds": None,
                            "estimated_seconds": parse_time_from_string_or_filename(filename) or 3600,
                            "estimated_formatted": "Slicing on N100...",
                            "filesize": f"{round(len(file_data) / (1024*1024), 2)} MB",
                            "status": "slicing",
                        }
                        initial_jobs.append(p_job)
                else:
                    job_id = f"job_{now_ms}"
                    initial_job = {
                        "id": job_id,
                        "filename": f"{base_name}.gcode",
                        "display_name": display_name,
                        "filament": param_filament or "PLA",
                        "slot": resolved_slot,
                        "tray_id": resolved_tray,
                        "plate": param_plate or "A",
                        "auto_level": auto_level,
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "slice_time_seconds": None,
                        "estimated_seconds": parse_time_from_string_or_filename(filename) or 3600,
                        "estimated_formatted": "Slicing on N100...",
                        "filesize": f"{round(len(file_data) / (1024*1024), 2)} MB",
                        "status": "slicing",
                    }
                    initial_jobs.append(initial_job)

                queue = load_queue()
                queue.extend(initial_jobs)
                save_queue(queue)

                expected_jids = [j["id"] for j in initial_jobs]

                def _bg_slice_task(fpath: Path, expected_ids: list[str], bname: str, clean_disp: str):
                    print(f"[*] Background slicing started for {fpath.name} (jobs: {expected_ids})...", flush=True)
                    try:
                        s_res = slice_and_upload(fpath)
                        print(f"[+] Background slicing succeeded for {fpath.name}", flush=True)
                        q = load_queue()
                        plates_res = s_res.get("plates", [])
                        if s_res.get("is_multi_plate") and plates_res:
                            for pl in plates_res:
                                pid = pl.get("plate_id")
                                pl_gcode = pl.get("gcode") or f"{bname}_plate_{pid}.gcode"
                                pl_disp = pl.get("display_name") or f"{clean_disp} - Plate {pid}"
                                pl_est = pl.get("estimated_seconds") or 3600
                                pl_slice = pl.get("slice_time_seconds") or s_res.get("slice_time")

                                matched = False
                                for it in q:
                                    if (it.get("id") in expected_ids and it.get("plate_id") == pid) or it.get("filename") == pl_gcode:
                                        it["filename"] = pl_gcode
                                        it["display_name"] = pl_disp
                                        it["status"] = "queued"
                                        it["slice_time_seconds"] = pl_slice
                                        it["estimated_seconds"] = pl_est
                                        it["estimated_formatted"] = format_duration(pl_est)
                                        matched = True
                                        break
                                if not matched:
                                    # Fallback: check if an initial placeholder with status 'slicing' matches
                                    for it in q:
                                        if it.get("id") in expected_ids and it.get("status") == "slicing":
                                            it["plate_id"] = pid
                                            it["filename"] = pl_gcode
                                            it["display_name"] = pl_disp
                                            it["status"] = "queued"
                                            it["slice_time_seconds"] = pl_slice
                                            it["estimated_seconds"] = pl_est
                                            it["estimated_formatted"] = format_duration(pl_est)
                                            matched = True
                                            break
                                    if not matched:
                                        # Append extra plate job
                                        q.append({
                                            "id": f"job_{int(time.time() * 1000)}_{pid}",
                                            "plate_id": pid,
                                            "filename": pl_gcode,
                                            "display_name": pl_disp,
                                            "filament": param_filament or "PLA",
                                            "slot": resolved_slot,
                                            "tray_id": resolved_tray,
                                            "plate": param_plate or "A",
                                            "auto_level": auto_level,
                                            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                            "slice_time_seconds": pl_slice,
                                            "estimated_seconds": pl_est,
                                            "estimated_formatted": format_duration(pl_est),
                                            "filesize": f"{round(fpath.stat().st_size / (1024*1024), 2)} MB",
                                            "status": "queued",
                                        })
                        else:
                            gcode_fn = s_res.get("gcode") or f"{bname}.gcode"
                            est = s_res.get("estimated_seconds") or parse_time_from_string_or_filename(fpath.name) or 3600
                            for it in q:
                                if it.get("id") in expected_ids or it.get("filename") == gcode_fn:
                                    it["filename"] = gcode_fn
                                    it["status"] = "queued"
                                    it["slice_time_seconds"] = s_res.get("slice_time")
                                    it["estimated_seconds"] = est
                                    it["estimated_formatted"] = format_duration(est)
                                    break
                        save_queue(q)
                    except Exception as ex:
                        print(f"[!] Background slicing failed for {fpath.name}: {ex}", flush=True)
                        q = load_queue()
                        for it in q:
                            if it.get("id") in expected_ids:
                                it["status"] = "error"
                                it["estimated_formatted"] = "Slicing failed"
                                it["error"] = str(ex)
                        save_queue(q)

                threading.Thread(
                    target=_bg_slice_task,
                    args=(saved_path, expected_jids, base_name, display_name),
                    daemon=True,
                ).start()

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "success",
                    "queued": True,
                    "is_multi_plate": is_multi_plate,
                    "message": f"'{display_name}' ({len(initial_jobs)} plate{'s' if len(initial_jobs) > 1 else ''}) received! Slicing in background on N100 and queued to CC2.",
                    "job": initial_jobs[0],
                    "jobs": initial_jobs,
                    "filename": filename,
                    "display_name": display_name,
                }).encode())
                return
            else:
                try:
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
                raw_auto_level = data.get("auto_level")
                auto_level = True if raw_auto_level is None else (False if str(raw_auto_level).lower() in ("false", "0") else bool(raw_auto_level))

                job = add_job_to_queue(
                    filename=fname,
                    est_seconds=est,
                    filament=filament,
                    plate=plate,
                    slot=slot,
                    tray_id=tray_id,
                    slice_time_seconds=data.get("slice_time_seconds"),
                    display_name=data.get("display_name", fname),
                    auto_level=auto_level,
                    filesize=data.get("filesize", ""),
                )
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

                if target_job.get("status") == "slicing":
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Job is still slicing on N100. Please wait until slicing completes."}).encode())
                    return

                if target_job.get("status") == "error":
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": f"Job slicing failed: {target_job.get('error', 'unknown error')}"}).encode())
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
                if target_job.get("status") == "slicing":
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Job is still slicing on N100. Please wait until slicing completes."}).encode())
                    return
                if target_job.get("status") == "error":
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": f"Job slicing failed: {target_job.get('error', 'unknown error')}"}).encode())
                    return
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
            elif target_job:
                # Re-add job to queue so it is not lost on start failure
                q_cur = load_queue()
                if not any(j.get("id") == target_job.get("id") for j in q_cur):
                    q_cur.insert(0, target_job)
                    save_queue(q_cur)

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
