#!/bin/sh
# Push audio to Galene with Meetecho's simple-whip-client
# (https://github.com/meetecho/simple-whip-client, Debian/Ubuntu package
# "simple-whip-client"). It uses webrtcbin and takes the audio part of the
# pipeline as a string, which must end in Opus RTP caps.
#
#   WHIP_URL=https://galene.example.org/group/test/.whip WHIP_TOKEN=... ./simple-whip-client.sh
#
# DOES NOT WORK WITH GALENE (tested with Debian 0.0~git20240308): the
# client offers without BUNDLE, Galene rejects the media section, and no
# audio flows. Kept as a reference and as a test case documenting this.
: "${WHIP_URL:?set WHIP_URL to https://HOST/group/GROUP/.whip}"
AUDIO_SRC=${AUDIO_SRC:-audiotestsrc is-live=true wave=sine freq=440 volume=0.3}

exec whip-client -u "$WHIP_URL" ${WHIP_TOKEN:+-t "$WHIP_TOKEN"} \
    -A "$AUDIO_SRC ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2
        ! opusenc bitrate=64000 frame-size=20 inband-fec=true
        ! rtpopuspay pt=111 ssrc=1 ! queue
        ! application/x-rtp,media=audio,encoding-name=OPUS,payload=111"
