"""Minimal WHIP client built on webrtcbin.

Works with GStreamer >= 1.20 and only needs gst-plugins-bad (webrtcbin) and
gstreamer1.0-nice, so it runs on the GStreamer that distributions ship,
including Ubuntu 22.04 used by voctomix's Dockerfile. No gst-plugins-rs
needed.

Two layers:

WhipSession
    Drives one webrtcbin element through a WHIP publish: create offer,
    wait for ICE gathering, POST the offer, apply the answer, and DELETE
    the session on stop. Reports failures through a callback and never
    posts errors on the pipeline bus.

WhipPublisher
    Owns a separate Gst.Pipeline (``<source> ! opus ! rtp ! webrtcbin``)
    and restarts it with backoff whenever the session fails. Because the
    pipeline is separate, a failing WHIP connection cannot take down the
    pipeline that feeds it, e.g. voctocore.

The HTTP side uses only the standard library.
"""

import logging
import random
import threading
import urllib.error
import urllib.parse
import urllib.request

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402

log = logging.getLogger("whip")

OPUS_PT = 111


class WhipError(Exception):
    pass


class WhipSession:
    """One WHIP publish over an existing webrtcbin.

    Create it after the webrtcbin is linked (so its transceivers exist) and
    before the pipeline goes to PLAYING; webrtcbin then fires
    on-negotiation-needed and the session takes it from there.
    """

    def __init__(self, webrtcbin, endpoint, token=None, on_failure=None,
                 on_connected=None, http_timeout=10.0, gather_timeout=5.0,
                 connect_timeout=15.0, liveness_interval=5.0, liveness_failures=3):
        self.webrtc = webrtcbin
        self.endpoint = endpoint
        self.token = token
        self.on_failure = on_failure
        self.on_connected = on_connected
        self.http_timeout = http_timeout
        self.gather_timeout = gather_timeout
        self.connect_timeout = connect_timeout
        self.liveness_interval = liveness_interval
        self.liveness_failures = liveness_failures
        self._stop_event = threading.Event()

        self.resource_url = None
        self.etag = None
        self._lock = threading.Lock()
        self._posted = False
        self._failed = False
        self._stopped = False
        self._candidates = []
        self._gather_timer = None
        self._connect_timer = None
        self._connected = False
        self._handlers = [
            webrtcbin.connect("on-negotiation-needed", self._on_negotiation_needed),
            webrtcbin.connect("on-ice-candidate", self._on_ice_candidate),
            webrtcbin.connect("notify::ice-gathering-state", self._on_gathering_state),
            webrtcbin.connect("notify::connection-state", self._on_connection_state),
        ]

        # WHIP is publish-only: make every transceiver sendonly so the
        # offer does not ask the server for media back.
        # (get-transceivers returns a GArray PyGObject can't iterate on
        # GStreamer 1.20; each linked sink pad has one transceiver.)
        for idx in range(webrtcbin.numsinkpads):
            trans = webrtcbin.emit("get-transceiver", idx)
            trans.set_property(
                "direction", GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)

    # --- negotiation -----------------------------------------------------

    def _on_negotiation_needed(self, element):
        with self._lock:
            if self._posted or self._stopped:
                return
        log.debug("negotiation needed, creating offer")
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, None)
        element.emit("create-offer", None, promise)

    def _on_offer_created(self, promise, _data):
        reply = promise.get_reply()
        offer = reply.get_value("offer") if reply else None
        if offer is None:
            self._fail("webrtcbin did not create an offer")
            return
        self.webrtc.emit("set-local-description", offer, None)
        # Gathering normally completes in well under a second with host
        # candidates only; a STUN server that does not answer can stall it,
        # so post whatever we have after gather_timeout.
        self._gather_timer = threading.Timer(self.gather_timeout, self._post_offer,
                                             kwargs={"reason": "gathering timeout"})
        self._gather_timer.daemon = True
        self._gather_timer.start()

    def _on_ice_candidate(self, _element, mline, candidate):
        with self._lock:
            self._candidates.append((mline, candidate))

    def _on_gathering_state(self, element, _pspec):
        state = element.get_property("ice-gathering-state")
        log.debug("ICE gathering state: %s", state.value_nick)
        if state == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            # Don't call back into webrtcbin from its own signal emission.
            threading.Thread(target=self._post_offer, args=("gathering complete",),
                             daemon=True).start()

    def _offer_sdp(self):
        """Local description with all gathered candidates (non-trickle)."""
        desc = self.webrtc.get_property("local-description")
        text = desc.sdp.as_text()
        with self._lock:
            missing = [(m, c) for m, c in self._candidates
                       if "a=" + c not in text]
        if not missing:
            return text
        # Older webrtcbin versions do not always fold candidates into
        # local-description; add them to their m= sections ourselves.
        lines = text.rstrip("\r\n").split("\r\n")
        out, mline = [], -1
        for line in lines:
            if line.startswith("m="):
                if mline >= 0:
                    out.extend("a=" + c for m, c in missing if m == mline)
                mline += 1
            out.append(line)
        out.extend("a=" + c for m, c in missing if m == mline)
        return "\r\n".join(out) + "\r\n"

    def _post_offer(self, reason):
        with self._lock:
            if self._posted or self._stopped:
                return
            self._posted = True
        if self._gather_timer is not None:
            self._gather_timer.cancel()
        log.debug("posting offer (%s)", reason)
        # Runs on a timer or helper thread, never a streaming thread.
        self._do_post(self._offer_sdp())

    def _do_post(self, offer):
        req = urllib.request.Request(
            self.endpoint, data=offer.encode(), method="POST",
            headers=self._headers({"Content-Type": "application/sdp"}))
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as resp:
                if resp.status != 201:
                    raise WhipError("unexpected HTTP status %d" % resp.status)
                location = resp.headers.get("Location")
                self.etag = resp.headers.get("ETag")
                answer = resp.read().decode()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace").strip()
            if "<html" in body.lower():
                body = ""  # Galene's 404 page
            self._fail("WHIP POST failed: HTTP %d %s %s" % (e.code, e.reason, body[:160]))
            return
        except Exception as e:  # URLError, timeouts, WhipError
            self._fail("WHIP POST failed: %s" % e)
            return

        if location:
            self.resource_url = urllib.parse.urljoin(self.endpoint, location)
        log.info("WHIP session created: %s", self.resource_url)

        res, sdpmsg = GstSdp.SDPMessage.new_from_text(answer)
        if res != GstSdp.SDPResult.OK:
            self._fail("could not parse SDP answer")
            return
        desc = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
        self.webrtc.emit("set-remote-description", desc, None)
        # A session can get stuck in "connecting" (e.g. a media section the
        # server rejected, UDP blocked); webrtcbin never fails it by itself.
        self._connect_timer = threading.Timer(self.connect_timeout, self._check_connected)
        self._connect_timer.daemon = True
        self._connect_timer.start()

    def _check_connected(self):
        if not self._connected:
            self._fail("not connected %.0fs after the WHIP answer "
                       "(UDP blocked? see docs/galene-whip.md#network)" % self.connect_timeout)

    def _on_connection_state(self, element, _pspec):
        state = element.get_property("connection-state")
        log.info("peer connection state: %s", state.value_nick)
        if state == GstWebRTC.WebRTCPeerConnectionState.CONNECTED:
            if not self._connected and self.liveness_interval and self.resource_url:
                threading.Thread(target=self._liveness_loop, daemon=True).start()
            self._connected = True
            if self.on_connected:
                self.on_connected()
        elif state in (GstWebRTC.WebRTCPeerConnectionState.FAILED,
                       GstWebRTC.WebRTCPeerConnectionState.CLOSED):
            self._fail("peer connection %s" % state.value_nick)

    def _liveness_loop(self):
        """Poll the WHIP resource; fail when the server forgot the session.

        Before GStreamer 1.22 / libnice 0.1.19, webrtcbin has no ICE consent
        freshness and never notices a dead peer: after a Galene restart it
        would keep sending into the void forever. Galene answers OPTIONS on
        the session URL with 200 while the session exists and 404 after a
        restart or kick. Other statuses (e.g. 405 from servers without
        OPTIONS support) are ignored; only 404/410 and repeated network
        errors count.
        """
        errors = 0
        while not self._stop_event.wait(self.liveness_interval):
            req = urllib.request.Request(self.resource_url, method="OPTIONS",
                                         headers=self._headers())
            try:
                urllib.request.urlopen(req, timeout=self.http_timeout).close()
                errors = 0
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    self.resource_url = None  # nothing left to DELETE
                    self._fail("WHIP session gone on the server (HTTP %d)" % e.code)
                    return
                errors = 0
            except Exception as e:
                errors += 1
                log.debug("liveness check failed (%d): %s", errors, e)
                if errors >= self.liveness_failures:
                    self._fail("server unreachable: %s" % e)
                    return

    # --- teardown ----------------------------------------------------------

    def _headers(self, extra=None):
        h = dict(extra or {})
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        return h

    def _fail(self, reason):
        with self._lock:
            if self._failed or self._stopped:
                return
            self._failed = True
        log.warning("%s", reason)
        if self.on_failure:
            self.on_failure(reason)

    def stop(self):
        """Disconnect from webrtcbin and DELETE the WHIP resource."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        self._stop_event.set()
        for timer in (self._gather_timer, self._connect_timer):
            if timer is not None:
                timer.cancel()
        for h in self._handlers:
            self.webrtc.disconnect(h)
        if not self.resource_url:
            return
        req = urllib.request.Request(self.resource_url, method="DELETE",
                                     headers=self._headers())
        try:
            urllib.request.urlopen(req, timeout=self.http_timeout).close()
            log.info("WHIP session deleted")
        except Exception as e:
            # The server times the session out on its own when ICE fails.
            log.warning("WHIP DELETE failed: %s", e)


def opus_whip_pipeline(source, bitrate=64000, channels=2):
    """gst-launch description: raw audio from ``source`` to a webrtcbin."""
    return (
        "{source} ! audioconvert ! audioresample"
        " ! audio/x-raw,rate=48000,channels={channels}"
        " ! opusenc bitrate={bitrate} frame-size=20 inband-fec=true"
        " ! rtpopuspay pt={pt}"
        " ! application/x-rtp,media=audio,encoding-name=OPUS,payload={pt}"
        " ! webrtcbin name=whip bundle-policy=max-bundle"
    ).format(source=source, channels=channels, bitrate=bitrate, pt=OPUS_PT)


class WhipPublisher:
    """Keeps a WHIP publish up, rebuilding its pipeline on failure.

    ``pipeline_description`` must contain a webrtcbin named ``whip``; see
    opus_whip_pipeline(). Needs a running GLib main loop.
    """

    def __init__(self, pipeline_description, endpoint, token=None, name="whip",
                 clock=None, stun_server=None, min_backoff=1.0, max_backoff=30.0):
        self.description = pipeline_description
        self.endpoint = endpoint
        self.token = token
        self.name = name
        self.clock = clock
        self.stun_server = stun_server
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self.log = logging.getLogger("whip.%s" % name)

        self.pipeline = None
        self.session = None
        self._backoff = min_backoff
        self._retry_source = None
        self._running = False
        self.connected = False
        self.attempts = 0

    def start(self):
        self._running = True
        self._start_pipeline()

    def stop(self):
        self._running = False
        if self._retry_source is not None:
            GLib.source_remove(self._retry_source)
            self._retry_source = None
        self._teardown()

    def _start_pipeline(self):
        self._retry_source = None
        if not self._running:
            return False
        self.attempts += 1
        self.log.info("connecting to %s (attempt %d)", self.endpoint, self.attempts)
        pipeline = Gst.parse_launch(self.description)
        pipeline.set_name("whip-%s" % self.name)
        if self.clock is not None:
            pipeline.use_clock(self.clock)
        webrtc = pipeline.get_by_name("whip")
        if self.stun_server:
            webrtc.set_property("stun-server", self.stun_server)

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)

        self.pipeline = pipeline
        self.session = WhipSession(
            webrtc, self.endpoint, self.token,
            on_failure=lambda reason: GLib.idle_add(self._on_failure, reason),
            on_connected=lambda: GLib.idle_add(self._on_connected))
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            GLib.idle_add(self._on_failure, "pipeline failed to start")
        return False

    def _on_connected(self):
        self.connected = True
        self._backoff = self.min_backoff
        self.log.info("publishing")
        return False

    def _on_bus_error(self, _bus, message):
        err, debug = message.parse_error()
        self.log.debug("%s", debug)
        self._on_failure("%s: %s" % (message.src.get_name(), err.message))

    def _on_failure(self, reason):
        if self.pipeline is None:
            return False
        self.log.warning("publish failed: %s", reason)
        self._teardown()
        if self._running and self._retry_source is None:
            delay = self._backoff * random.uniform(0.8, 1.2)
            self._backoff = min(self._backoff * 2, self.max_backoff)
            self.log.info("retrying in %.1fs", delay)
            self._retry_source = GLib.timeout_add(int(delay * 1000), self._start_pipeline)
        return False

    def _teardown(self):
        self.connected = False
        session, pipeline = self.session, self.pipeline
        self.session = self.pipeline = None
        if session is not None:
            session.stop()
        if pipeline is not None:
            pipeline.get_bus().remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)
