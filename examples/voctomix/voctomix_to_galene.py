#!/usr/bin/env python3
"""Sidecar: publish voctocore audio channels to Galene groups over WHIP.

Connects to one of voctocore's Matroska-over-TCP outputs (11000 = mix,
15000 = first blinded live output), takes the audio track, and publishes
selected channels to one Galene group each, e.g. the floor language to one
group and the interpretation to another.

    voctomix_to_galene.py config.json

config.json (see voctomix_to_galene.example.json):

    {
      "source": {"host": "localhost", "port": 15000, "channels": 4},
      "streams": [
        {"name": "floor",
         "url": "https://galene.example.org/group/floor/.whip",
         "token_file": "/etc/galene-ingest/floor.token",
         "channels": [0, 1]},
        {"name": "translation",
         "url": "https://galene.example.org/group/translation/.whip",
         "token_file": "/etc/galene-ingest/translation.token",
         "channels": [2, 3]}
      ]
    }

Every part reconnects on its own: the TCP source when voctocore restarts,
each WHIP publish when Galene or the network goes away. While the source
is down, listeners get silence instead of a dropped stream.
"""

import json
import logging
import os
import signal
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "python"))
from channels import audio_caps, inter_caps, select_channels  # noqa: E402
from whip import WhipPublisher, opus_whip_pipeline  # noqa: E402

log = logging.getLogger("voctomix-to-galene")


class TcpSource:
    """tcpclientsrc ! matroskademux, fanned out to interaudiosinks."""

    def __init__(self, host, port, in_channels, streams, retry=2.0):
        self.retry = retry
        self.pipeline = None
        self.retry_source = None
        branches = "".join(
            """
            t. ! queue max-size-time=1000000000 leaky=downstream
               {select} ! audioconvert ! {caps} ! interaudiosink channel=whip-{name}
            """.format(select=select_channels(in_channels, s["channels"]),
                       caps=inter_caps(len(s["channels"])), name=s["name"])
            for s in streams)
        self.description = """
            tcpclientsrc host={host} port={port}
            ! matroskademux name=demux
            demux.video_0 ! queue ! fakesink sync=false async=false
            demux.audio_0 ! queue ! audioconvert ! {caps} ! tee name=t
            {branches}
            """.format(host=host, port=port, caps=audio_caps(in_channels), branches=branches)

    def start(self):
        self.retry_source = None
        log.info("connecting to voctocore output")
        self.pipeline = Gst.parse_launch(self.description)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self.on_down)
        bus.connect("message::eos", self.on_down)
        self.pipeline.set_state(Gst.State.PLAYING)
        return False

    def on_down(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            log.warning("source: %s", message.parse_error()[0].message)
        else:
            log.warning("source: end of stream (voctocore went away?)")
        self.stop()
        self.retry_source = GLib.timeout_add(int(self.retry * 1000), self.start)

    def stop(self):
        if self.retry_source is not None:
            GLib.source_remove(self.retry_source)
            self.retry_source = None
        if self.pipeline is not None:
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None


def read_token(stream):
    if stream.get("token_file"):
        with open(stream["token_file"]) as f:
            return f.read().strip()
    return stream.get("token")


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: {} config.json".format(sys.argv[0]))
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Gst.init(None)
    clock = Gst.SystemClock.obtain()

    src = cfg["source"]
    streams = cfg["streams"]
    source = TcpSource(src.get("host", "localhost"), src.get("port", 15000),
                       src.get("channels", 2), streams)
    publishers = []
    for s in streams:
        n = len(s["channels"])
        desc = opus_whip_pipeline(
            "interaudiosrc channel=whip-{} ! {}".format(s["name"], inter_caps(n)),
            bitrate=s.get("bitrate", 64000), channels=min(n, 2))
        publishers.append(WhipPublisher(desc, s["url"], read_token(s), name=s["name"],
                                        clock=clock, stun_server=s.get("stun_server")))

    loop = GLib.MainLoop()

    def shutdown(*_):
        log.info("shutting down")
        for p in publishers:
            p.stop()
        source.stop()
        loop.quit()
        return GLib.SOURCE_REMOVE

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, shutdown)

    source.start()
    for p in publishers:
        p.start()
    loop.run()


if __name__ == "__main__":
    main()
