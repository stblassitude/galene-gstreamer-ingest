# Same base as voctomix's Dockerfile (Ubuntu 22.04, GStreamer 1.20).
# Only distro packages: tests that the webrtcbin WHIP client works there.
FROM ubuntu:22.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-nice \
        gir1.2-gstreamer-1.0 \
        gir1.2-gst-plugins-base-1.0 \
        gir1.2-gst-plugins-bad-1.0 \
        python3 python3-gi python3-websocket python3-pytest \
 && rm -rf /var/lib/apt/lists/*
# Build the plugin registry now instead of on every container start.
RUN gst-inspect-1.0 >/dev/null 2>&1
WORKDIR /work
