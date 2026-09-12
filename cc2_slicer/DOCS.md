# Documentation: Elegoo CC2 Headless Slicer & Kiosk

## Overview
This add-on turns your Home Assistant instance into an autonomous 3D printing station for the **Elegoo Centauri Carbon 2 (CC2)**.

## Architecture
- **OrcaSlicer v2.4.2 Linux AppImage**: Runs inside the container with `xvfb-run` virtual display.
- **pycentauri CLI**: Communicates with the printer via LAN mode (supports passcode protection).
- **Print Kiosk UI**: Integrated responsive web dashboard running on port 8765 or through Home Assistant Ingress.
- **Direct Queue & History**: Persistent multi-job scheduling with automatic G-code duration parsing.

## Hardware & Performance Baseline
### What is an "N100"?
The Intel Processor N100 is a modern 4-core, 4-thread x86_64 low-power CPU (6W–15W TDP) commonly found in affordable mini PCs (Beelink, Minisforum, GMKtec, etc.) widely used as Home Assistant OS hosts.

### Performance Tiers
- **Intel Core i5 / i7 / AMD Ryzen**: ~2 – 4 seconds slice time
- **Intel N100 / N95 / N200 (Baseline)**: ~4 – 8 seconds slice time
- **Intel Celeron J4125 / N5105**: ~15 – 30 seconds slice time
- **Raspberry Pi / ARM**: Not supported (`amd64` only due to upstream OrcaSlicer AppImage)

### Minimum Requirements
- **Architecture**: `amd64` (x86_64)
- **Cores**: 4 CPU cores
- **RAM**: 4 GB minimum (8 GB+ recommended)
- **Free Disk**: 2.5 GB

## Kiosk Controls
- **Camera Live Stream HUD**: Overlay on camera feed providing real-time file name, elapsed print time, start time, and remaining time.
- **Confirmation Flow**: Opens on every print start to verify Plate A vs Plate B and select the matching Canvas AMS filament slot.
- **Queue**: Displays per-job estimated time and the total required time to clear the entire queue.
- **History**: Permanent record of completed, printing, and failed print jobs.
