# GStreamer client image with every WHIP client we evaluate:
#   - whipclientsink / whipsink  (gst-plugins-rs, built from source below)
#   - simple-whip-client         (Debian package, uses webrtcbin)
#   - webrtcbin + PyGObject      (for examples/python/whip_webrtcbin.py)
#
# Debian trixie ships GStreamer 1.26; gst-plugins-rs 0.14.x is the matching
# release series.

ARG DEBIAN_RELEASE=trixie

FROM debian:${DEBIAN_RELEASE} AS rsbuild
ARG GST_PLUGINS_RS_VERSION=0.14.5
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates git pkg-config build-essential cargo rustc \
        libssl-dev \
        libgstreamer1.0-dev \
        libgstreamer-plugins-base1.0-dev \
        libgstreamer-plugins-bad1.0-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${GST_PLUGINS_RS_VERSION}" \
        https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git /src
WORKDIR /src
RUN --mount=type=cache,target=/root/.cargo/registry \
    --mount=type=cache,target=/src/target \
    cargo build --release -p gst-plugin-webrtc -p gst-plugin-webrtchttp \
 && mkdir -p /out \
 && cp target/release/libgstrswebrtc.so target/release/libgstwebrtchttp.so /out/

FROM debian:${DEBIAN_RELEASE}
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl jq \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-nice \
        gir1.2-gstreamer-1.0 \
        gir1.2-gst-plugins-base-1.0 \
        gir1.2-gst-plugins-bad-1.0 \
        python3 python3-gi python3-websocket python3-pytest \
        simple-whip-client \
 && rm -rf /var/lib/apt/lists/*
COPY --from=rsbuild /out/ /usr/local/lib/gstreamer-1.0/
ENV GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0
# Build the plugin registry now instead of on every container start.
RUN gst-inspect-1.0 whipclientsink >/dev/null && gst-inspect-1.0 whipsink >/dev/null
WORKDIR /work
