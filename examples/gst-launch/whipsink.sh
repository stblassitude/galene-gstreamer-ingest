#!/bin/sh
# Push audio to Galene with whipsink (gst-plugins-rs "webrtchttp" plugin,
# GStreamer >= 1.22). whipsink expects RTP, so you encode and payload
# yourself. Upstream recommends whipclientsink instead.
#
#   WHIP_URL=https://galene.example.org/group/test/.whip WHIP_TOKEN=... ./whipsink.sh
: "${WHIP_URL:?set WHIP_URL to https://HOST/group/GROUP/.whip}"
AUDIO_SRC=${AUDIO_SRC:-audiotestsrc is-live=true wave=sine freq=440 volume=0.3}

# shellcheck disable=SC2086
exec gst-launch-1.0 -e \
    $AUDIO_SRC \
    ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2 \
    ! opusenc bitrate=64000 frame-size=20 inband-fec=true \
    ! rtpopuspay pt=111 \
    ! application/x-rtp,media=audio,encoding-name=OPUS,payload=111 \
    ! whipsink whip-endpoint="$WHIP_URL" \
        ${WHIP_TOKEN:+auth-token="$WHIP_TOKEN"}
