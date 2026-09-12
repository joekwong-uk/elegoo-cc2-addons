# Elegoo CC2 Headless Slicer & Kiosk Add-on

Automated headless Orca/Elegoo 3MF & STL slicer microservice and mobile print kiosk for Home Assistant.

## Features
- **Mobile Print Kiosk**: High-performance dark-themed mobile kiosk UI accessible directly via Home Assistant Ingress or standalone dashboard iframe.
- **Chamber Camera Live HUD**: Live chamber camera feed with bottom status HUD overlay showing active job, elapsed time, started timestamp, and remaining print ETA.
- **Pre-Print Confirmation Modal**: 3D model thumbnail preview, Plate A/B selection, and dynamic Canvas AMS slot matching with PLA Pro enforcement.
- **Print Queue & History**: Multi-job queue with estimated duration per job and total queue clearance time, plus persistent history tracking.
- **Headless CLI Slicing**: Slices STL and 3MF files directly on your Home Assistant host using OrcaSlicer v2.4.2 without any GUI requirement.
- **Embedded Thumbnail Generation**: Extracts 3MF plate preview thumbnails and generates embedded G-code thumbnails compatible with the CC2 touch screen.
- **Direct CC2 Hardware Upload**: Integrated `pycentauri` engine transmits sliced jobs directly to the Elegoo Centauri Carbon 2 over local Wi-Fi / Ethernet.

## Installation
1. Add `https://github.com/joekwong-uk/elegoo-cc2-addons` to your Home Assistant Add-on Repositories.
2. Install **Elegoo CC2 Headless Slicer & Kiosk**.
3. Configure your printer IP and Access Code under the **Configuration** tab.
4. Click **Start**.

## Configuration
```yaml
printer_ip: "192.168.1.100"
access_code: "000000"
```
