#!/bin/sh
# Push audio to Galene with whipclientsink (gst-plugins-rs "rswebrtc" plugin,
# GStreamer >= 1.24). whipclientsink takes raw audio and does the Opus
# encoding itself.
#
#   WHIP_URL=https://galene.example.org/group/test/.whip WHIP_TOKEN=... ./whipclientsink.sh
#
# AUDIO_SRC replaces the test tone, e.g. AUDIO_SRC="pulsesrc device=..."
: "${WHIP_URL:?set WHIP_URL to https://HOST/group/GROUP/.whip}"
AUDIO_SRC=${AUDIO_SRC:-audiotestsrc is-live=true wave=sine freq=440 volume=0.3}

# congestion-control=disabled: GCC only adapts video and needs rtpgccbwe.
# shellcheck disable=SC2086
exec gst-launch-1.0 -e \
    $AUDIO_SRC \
    ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2 \
    ! queue \
    ! whipclientsink congestion-control=disabled \
        signaller::whip-endpoint="$WHIP_URL" \
        ${WHIP_TOKEN:+signaller::auth-token="$WHIP_TOKEN"}
