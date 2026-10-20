#!/usr/bin/env python3
"""Push audio from a GStreamer pipeline to a Galene group over WHIP.

Uses PyGObject (``gi.repository.Gst``) and Gst.parse_launch(), the same way
voctomix builds its pipelines.

    whip_push.py --url https://galene.example.org/group/test/.whip --token TOKEN
    whip_push.py --client whipclientsink --source "pulsesrc device=..." ...

--client selects the WHIP implementation:

  webrtcbin       examples/python/whip.py on plain webrtcbin (default;
                  GStreamer >= 1.20, no gst-plugins-rs needed)
  whipclientsink  gst-plugins-rs webrtcsink with the WHIP signaller
  whipsink        gst-plugins-rs webrtchttp whipsink (deprecated upstream)

All three are restarted with backoff when the connection fails, so the
script can run unattended. SIGINT/SIGTERM tear the WHIP session down
cleanly (HTTP DELETE), so Galene drops the stream at once.
"""

import argparse
import logging
import os
import signal
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from whip import OPUS_PT, WhipPublisher, opus_whip_pipeline  # noqa: E402

DEFAULT_SOURCE = "audiotestsrc is-live=true wave=sine freq=440 volume=0.3"


def rs_pipeline(client, source, url, token, bitrate, channels):
    raw = ("{source} ! audioconvert ! audioresample"
           " ! audio/x-raw,rate=48000,channels={channels}").format(
        source=source, channels=channels)
    if client == "whipclientsink":
        # webrtcsink encodes by itself. Congestion control only adapts video,
        # and needs rtpgccbwe from gst-plugins-rs, so switch it off.
        sink = ("whipclientsink name=whip congestion-control=disabled"
                " signaller::whip-endpoint=\"{url}\"").format(url=url)
        if token:
            sink += " signaller::auth-token=\"{}\"".format(token)
        return "{} ! queue ! {}".format(raw, sink)
    if client == "whipsink":
        sink = "whipsink name=whip whip-endpoint=\"{}\"".format(url)
        if token:
            sink += " auth-token=\"{}\"".format(token)
        return ("{raw} ! opusenc bitrate={bitrate} frame-size=20 inband-fec=true"
                " ! rtpopuspay pt={pt}"
                " ! application/x-rtp,media=audio,encoding-name=OPUS,payload={pt}"
                " ! {sink}").format(raw=raw, bitrate=bitrate, pt=OPUS_PT, sink=sink)
    raise ValueError(client)


class RsPublisher:
    """Restart loop for the gst-plugins-rs sinks, which post bus errors.

    These sinks reach PLAYING before the WHIP POST has even been answered,
    so the backoff is only reset once a pipeline has stayed up for
    ``stable_after`` seconds.
    """

    def __init__(self, description, min_backoff=1.0, max_backoff=30.0, stable_after=10.0):
        self.description = description
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self.stable_after = stable_after
        self.backoff = min_backoff
        self.pipeline = None
        self.running = False
        self.retry = None
        self.stable_timer = None
        self.log = logging.getLogger("whip.rs")

    def start(self):
        self.running = True
        self._start()

    def _start(self):
        self.retry = None
        if not self.running:
            return False
        self.pipeline = Gst.parse_launch(self.description)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        bus.connect("message::state-changed", self._on_state)
        self.pipeline.set_state(Gst.State.PLAYING)
        return False

    def _on_state(self, _bus, message):
        if message.src is self.pipeline:
            _old, new, _pending = message.parse_state_changed()
            if new == Gst.State.PLAYING and self.stable_timer is None:
                self.log.info("pipeline playing")
                self.stable_timer = GLib.timeout_add(int(self.stable_after * 1000), self._stable)

    def _stable(self):
        self.stable_timer = None
        self.backoff = self.min_backoff
        return False

    def _on_error(self, _bus, message):
        err, debug = message.parse_error()
        # The HTTP status and server message are only in the debug string.
        detail = (debug or "").strip().splitlines()[-1:] or [""]
        self.log.warning("%s: %s (%s)", message.src.get_name(), err.message, detail[0][:160])
        self._teardown()
        if self.running and self.retry is None:
            self.log.info("retrying in %.1fs", self.backoff)
            self.retry = GLib.timeout_add(int(self.backoff * 1000), self._start)
            self.backoff = min(self.backoff * 2, self.max_backoff)

    def _teardown(self):
        if self.stable_timer is not None:
            GLib.source_remove(self.stable_timer)
            self.stable_timer = None
        if self.pipeline is not None:
            self.pipeline.get_bus().remove_signal_watch()
            # NULL makes the WHIP sinks send their DELETE.
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def stop(self):
        self.running = False
        if self.retry is not None:
            GLib.source_remove(self.retry)
        self._teardown()


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default=os.environ.get("WHIP_URL"),
                   help="WHIP endpoint, https://host/group/NAME/.whip (env WHIP_URL)")
    p.add_argument("--token", default=os.environ.get("WHIP_TOKEN"),
                   help="Galene token with the 'present' permission (env WHIP_TOKEN)")
    p.add_argument("--client", choices=["webrtcbin", "whipclientsink", "whipsink"],
                   default="webrtcbin")
    p.add_argument("--source", default=DEFAULT_SOURCE,
                   help="gst-launch description of a raw audio source")
    p.add_argument("--bitrate", type=int, default=64000, help="Opus bitrate in bit/s")
    p.add_argument("--channels", type=int, choices=[1, 2], default=2)
    p.add_argument("--stun-server", help="e.g. stun://stun.l.google.com:19302 (webrtcbin only)")
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    if not args.url:
        p.error("--url or WHIP_URL is required")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Gst.init(None)

    if args.client == "webrtcbin":
        publisher = WhipPublisher(
            opus_whip_pipeline(args.source, args.bitrate, args.channels),
            args.url, args.token, stun_server=args.stun_server)
    else:
        publisher = RsPublisher(rs_pipeline(args.client, args.source, args.url,
                                            args.token, args.bitrate, args.channels))

    loop = GLib.MainLoop()

    def shutdown(*_):
        logging.info("shutting down")
        publisher.stop()
        loop.quit()
        return GLib.SOURCE_REMOVE

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, shutdown)
    if args.duration:
        GLib.timeout_add(int(args.duration * 1000), shutdown)

    publisher.start()
    loop.run()


if __name__ == "__main__":
    main()
