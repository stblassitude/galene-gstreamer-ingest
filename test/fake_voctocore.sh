#!/bin/sh
# Stand-in for a voctocore output port: Matroska over TCP with raw video and
# 4 channels of S16LE audio, 440 Hz on channels 0+1 and 880 Hz on 2+3.
PORT=${1:-15000}
exec gst-launch-1.0 -q \
    audiointerleave name=il channel-positions-from-input=false \
    ! audioconvert ! audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=4,channel-mask=\(bitmask\)0x0 \
    ! queue ! mux. \
    audiotestsrc is-live=true wave=sine freq=440 volume=0.3 ! audio/x-raw,format=S16LE,rate=48000,channels=1 ! tee name=a \
    a. ! queue ! il.sink_0  a. ! queue ! il.sink_1 \
    audiotestsrc is-live=true wave=sine freq=880 volume=0.3 ! audio/x-raw,format=S16LE,rate=48000,channels=1 ! tee name=b \
    b. ! queue ! il.sink_2  b. ! queue ! il.sink_3 \
    videotestsrc is-live=true ! video/x-raw,format=I420,width=320,height=180,framerate=25/1 ! queue ! mux. \
    matroskamux name=mux streamable=true writing-app=fake-voctocore \
    ! tcpserversink host=0.0.0.0 port="$PORT" sync-method=next-keyframe
