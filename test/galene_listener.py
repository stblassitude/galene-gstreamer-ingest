#!/usr/bin/env python3
"""Join a Galene group as a listener and check what audio arrives.

Speaks Galene's WebSocket protocol (galene-protocol.md): it joins the
group, requests audio, answers the server's offers with webrtcbin, decodes
the Opus and runs a spectrum analysis. It prints a JSON summary and exits
0 if a tone at --expect-freq was heard, non-zero otherwise.

    galene_listener.py --url http://galene:8443 --group test --duration 8

Needs python3-websocket (websocket-client) and PyGObject with GstWebRTC.
"""

import argparse
import json
import logging
import re
import ssl
import sys
import threading
import urllib.parse
import urllib.request
import uuid

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402
import websocket  # noqa: E402

log = logging.getLogger("listener")

BANDS = 1024  # 23.4 Hz per band at 48 kHz


class Listener:
    def __init__(self, args):
        self.args = args
        self.id = uuid.uuid4().hex
        self.loop = GLib.MainLoop()
        self.pipeline = Gst.Pipeline.new("listener")
        self.ws = None
        self.lock = threading.Lock()
        self.streams = {}          # stream id -> webrtcbin
        self.offers = 0
        self.joined = None
        self.error = None
        self.spectra = 0
        self.peak_hits = 0
        self.max_rms_db = -200.0
        self.first_audio = None
        self.start_time = None

    # --- websocket ---------------------------------------------------------

    def send(self, msg):
        with self.lock:
            self.ws.send(json.dumps(msg))

    def endpoint(self):
        status_url = "{}/group/{}/.status".format(self.args.url.rstrip("/"),
                                                  urllib.parse.quote(self.args.group))
        ctx = None if not self.args.insecure else ssl._create_unverified_context()
        with urllib.request.urlopen(status_url, timeout=10, context=ctx) as r:
            ep = json.load(r)["endpoint"]
        # Behind a reverse proxy the advertised endpoint may not be reachable
        # from inside the test network; keep the path, use our host.
        base = urllib.parse.urlparse(self.args.url)
        epu = urllib.parse.urlparse(ep)
        scheme = "wss" if base.scheme == "https" else "ws"
        return urllib.parse.urlunparse((scheme, base.netloc, epu.path, "", "", ""))

    def run_ws(self):
        url = self.endpoint()
        log.info("connecting to %s", url)
        sslopt = {"cert_reqs": ssl.CERT_NONE} if self.args.insecure else None
        self.ws = websocket.create_connection(url, timeout=None, sslopt=sslopt)
        self.send({"type": "handshake", "version": ["2"], "id": self.id})
        join = {"type": "join", "kind": "join", "group": self.args.group}
        if self.args.token:
            join["token"] = self.args.token
        else:
            join["username"] = self.args.username
            join["password"] = self.args.password
        self.send(join)
        while True:
            try:
                raw = self.ws.recv()
            except Exception:
                break
            if not raw:
                break
            GLib.idle_add(self.on_message, json.loads(raw))

    def on_message(self, m):
        t = m.get("type")
        if t == "handshake":
            pass
        elif t == "ping":
            self.send({"type": "pong"})
        elif t == "joined":
            if m.get("kind") == "fail":
                self.finish("join failed: %s" % m.get("value"))
            elif m.get("kind") == "join":
                self.joined = m.get("username")
                log.info("joined as %r, permissions %s", self.joined, m.get("permissions"))
                self.send({"type": "request", "request": {"": ["audio"]}})
        elif t == "offer":
            self.on_offer(m)
        elif t == "ice":
            webrtc = self.streams.get(m["id"])
            c = m.get("candidate") or {}
            if webrtc is not None and c.get("candidate"):
                webrtc.emit("add-ice-candidate", c.get("sdpMLineIndex", 0), c["candidate"])
        elif t == "close":
            log.info("stream %s closed by server", m.get("id"))
        elif t == "usermessage" and m.get("kind") in ("error", "warning"):
            log.warning("server says: %s", m.get("value"))
        return False

    # --- media -------------------------------------------------------------

    def on_offer(self, m):
        sid = m["id"]
        self.offers += 1
        log.info("offer for stream %s from %r (label %s)", sid, m.get("username"), m.get("label"))
        webrtc = self.streams.get(sid)
        if webrtc is None:
            webrtc = Gst.ElementFactory.make("webrtcbin", None)
            webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
            webrtc.connect("pad-added", self.on_pad_added)
            webrtc.connect("on-ice-candidate", lambda _w, idx, cand: self.send(
                {"type": "ice", "id": sid,
                 "candidate": {"candidate": cand, "sdpMLineIndex": idx}}))
            self.pipeline.add(webrtc)
            webrtc.sync_state_with_parent()
            self.streams[sid] = webrtc
        _res, sdp = GstSdp.SDPMessage.new_from_text(m["sdp"])
        offer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, sdp)

        def on_answer(promise, _):
            reply = promise.get_reply()
            answer = reply.get_value("answer") if reply and reply.has_field("answer") else None
            if answer is None:
                self.finish("create-answer failed: %s" % (reply.to_string() if reply else None))
                return
            webrtc.emit("set-local-description", answer, None)
            self.send({"type": "answer", "id": sid, "sdp": answer.sdp.as_text()})

        def on_remote_set(_promise, _):
            webrtc.emit("create-answer", None, Gst.Promise.new_with_change_func(on_answer, None))

        webrtc.emit("set-remote-description", offer,
                    Gst.Promise.new_with_change_func(on_remote_set, None))

    def on_pad_added(self, _webrtc, pad):
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        s = caps.get_structure(0)
        if s.get_string("media") != "audio":
            fakesink = Gst.ElementFactory.make("fakesink", None)
            self.pipeline.add(fakesink)
            fakesink.sync_state_with_parent()
            pad.link(fakesink.get_static_pad("sink"))
            return
        log.info("audio track: %s", caps.to_string())
        b = Gst.parse_bin_from_description(
            "rtpopusdepay ! opusdec ! audioconvert ! audioresample"
            " ! audio/x-raw,rate=48000,channels=1"
            " ! level interval=200000000 post-messages=true"
            " ! spectrum bands={} interval=200000000 threshold=-90 post-messages=true"
            " ! fakesink sync=false".format(BANDS), True)
        self.pipeline.add(b)
        b.sync_state_with_parent()
        pad.link(b.get_static_pad("sink"))

    def on_element(self, _bus, message):
        s = message.get_structure()
        if s is None:
            return
        if s.get_name() == "level":
            rms = s.get_value("rms")
            if rms:
                self.max_rms_db = max(self.max_rms_db, max(rms))
        elif s.get_name() == "spectrum":
            # Older PyGObject can't convert GstValueList; parse the text form.
            m = re.search(r"magnitude=\(float\)\{([^}]*)\}", s.to_string())
            if not m:
                return
            mags = [float(v) for v in m.group(1).split(",")]
            peak = max(range(len(mags)), key=lambda i: mags[i])
            freq = (peak + 0.5) * 24000.0 / BANDS
            self.spectra += 1
            if mags[peak] > -60 and abs(freq - self.args.expect_freq) < 2 * 24000.0 / BANDS:
                self.peak_hits += 1
                if self.first_audio is None:
                    self.first_audio = GLib.get_monotonic_time() / 1e6 - self.start_time

    # --- lifecycle ---------------------------------------------------------

    def finish(self, error=None):
        if error and not self.error:
            self.error = error
        self.loop.quit()
        return False

    def run(self):
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::element", self.on_element)
        bus.connect("message::error", lambda _b, m: self.finish(m.parse_error()[0].message))
        self.pipeline.set_state(Gst.State.PLAYING)
        self.start_time = GLib.get_monotonic_time() / 1e6
        threading.Thread(target=self.run_ws, daemon=True).start()
        GLib.timeout_add(int(self.args.duration * 1000), self.finish)
        self.loop.run()
        self.pipeline.set_state(Gst.State.NULL)
        try:
            self.ws.close()
        except Exception:
            pass
        # A spectrum is posted every 200 ms; require the tone in most of them.
        heard = self.spectra > 0 and self.peak_hits >= max(3, self.spectra // 2)
        return {
            "joined_as": self.joined,
            "offers": self.offers,
            "spectra": self.spectra,
            "tone_hits": self.peak_hits,
            "max_rms_db": round(self.max_rms_db, 1),
            "first_tone_after_s": None if self.first_audio is None else round(self.first_audio, 2),
            "heard_tone": heard,
            "error": self.error,
        }


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", required=True, help="server base URL")
    p.add_argument("--group", required=True)
    p.add_argument("--username", default="whip-test-listener")
    p.add_argument("--password", default="")
    p.add_argument("--token")
    p.add_argument("--duration", type=float, default=8)
    p.add_argument("--expect-freq", type=float, default=440)
    p.add_argument("--insecure", action="store_true", help="skip TLS verification")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    Gst.init(None)
    result = Listener(args).run()
    print(json.dumps(result))
    sys.exit(0 if result["heard_tone"] else 1)


if __name__ == "__main__":
    main()
