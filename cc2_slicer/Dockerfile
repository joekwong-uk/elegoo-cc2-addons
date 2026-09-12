FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends     wget     ca-certificates     libopengl0     libglu1-mesa     libgl1-mesa-dri     libgomp1     libgtk-3-0     libwebkit2gtk-4.1-0     libsecret-1-0     xvfb     python3     python3-pip     python3-pil     && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages pycentauri

# Download OrcaSlicer AppImage and extract
RUN mkdir -p /opt/orca &&     wget -q https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.4.2/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage -O /tmp/orca.AppImage &&     chmod +x /tmp/orca.AppImage &&     cd /tmp && ./orca.AppImage --appimage-extract &&     mv /tmp/squashfs-root /opt/orca/app &&     rm /tmp/orca.AppImage &&     ln -s /opt/orca/app/AppRun /opt/orca/AppRun

WORKDIR /app
COPY server.py /app/server.py
COPY kiosk /app/kiosk
COPY profiles /profiles

EXPOSE 8765

CMD ["python3", "/app/server.py"]
