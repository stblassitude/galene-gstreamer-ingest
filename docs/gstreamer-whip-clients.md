# Choosing a GStreamer WHIP client

GStreamer has no WHIP client in its core modules (checked up to 1.28 in
Ubuntu 26.04). This repository compares four options, all tested against
Galene 1.2.1 with the harness in [`test/`](../test):

| | Where it lives | Needs |
|---|---|---|
| **webrtcbin + `whip.py`** | [`examples/python/whip.py`](../examples/python/whip.py), ~250 lines of Python in this repo | GStreamer ≥ 1.20, plugins-bad, gstreamer1.0-nice, PyGObject; all distro packages |
| **whipclientsink** | gst-plugins-rs, `rswebrtc` plugin (webrtcsink + WHIP signaller) | GStreamer ≥ 1.22; **not packaged** by Debian/Ubuntu, build from source |
| **whipsink** | gst-plugins-rs, `webrtchttp` plugin | same as above; upstream recommends whipclientsink instead |
| **simple-whip-client** | [meetecho/simple-whip-client](https://github.com/meetecho/simple-whip-client), standalone C program on webrtcbin | Debian/Ubuntu package `simple-whip-client` |

Homebrew's `gstreamer` formula (1.28) bundles gst-plugins-rs, so on macOS
whipclientsink and whipsink are available without building anything.

## Test results

Debian trixie (GStreamer 1.26.2, gst-plugins-rs 0.14.5) against a local
Galene 1.2.1, as run by [`test/run-tests.sh`](../test/run-tests.sh). Each
publisher sends a 440 Hz tone. A separate Galene listener
([`test/galene_listener.py`](../test/galene_listener.py)) joins over the
WebSocket protocol, decodes the Opus, and checks the frequency.

| | Audio heard by Galene listener | Clean teardown (DELETE) on Ctrl-C | Bad token | Audio back after Galene restart |
|---|---|---|---|---|
| **webrtcbin + `whip.py`** | ✅ after 0.3 s | ✅ | ✅ clear log line, retries with backoff | ✅ **~5 s** |
| whipclientsink | ✅ after 0.3 s | ✅ | ⚠️ generic bus error, the HTTP status is only in the debug string | ❌ never: keeps "playing", no error |
| whipsink | ✅ after 0.3 s | ❌ in `gst-launch -e`: EOS never completes, process must be killed, no DELETE (✅ via the Python wrapper, which goes straight to NULL) | ⚠️ as whipclientsink | ❌ as whipclientsink |
| simple-whip-client (Debian 0.0~git20240308) | ❌ **does not work with Galene** | – | ✅ exits | – |

The webrtcbin client gives the same results on **Ubuntu 22.04 /
GStreamer 1.20** (voctomix's Dockerfile base), including recovery after a
Galene restart. It has also been run inside a real voctocore, see
[voctomix-integration.md](voctomix-integration.md). The other three clients
can't run there: there is no gst-plugins-rs for 1.20, and no
simple-whip-client package in 22.04.

Why simple-whip-client fails: it creates webrtcbin with
`bundle-policy=none`, so its offer has no `a=group:BUNDLE`. Galene (Pion)
answers with the audio section rejected (`m=audio 0`). ICE completes, but
DTLS never starts and no media flows. There is no option to change this.
For other WHIP servers it's a fine quick-test tool.

## Recommendation

**Use webrtcbin with `examples/python/whip.py`.**

- It is the only option that notices a dead server or session on its own
  and reconnects, within seconds. The Rust sinks keep "playing" into the
  void after Galene restarts, so a long event would end up silent with
  nothing in the logs.
- It needs only distro packages, on every GStreamer from 1.20 to 1.28. No
  Rust toolchain or self-built plugins on the production box.
- webrtcbin is the most widely deployed WebRTC implementation in
  GStreamer. The WHIP part is small enough to read in a few minutes: POST
  the offer with all candidates (non-trickle), apply the answer, poll the
  session, DELETE on stop.
- It never posts bus errors on its own account. Failures go to a callback,
  so it can run next to a pipeline that must not die (voctocore).

Use **whipclientsink** if you want an element in a `gst-launch` line on a
machine where gst-plugins-rs is available (macOS/Homebrew, or a self-built
plugin), e.g. for a quick manual test. Don't rely on it unattended: it
does not notice a vanished server.

Avoid **whipsink** (deprecated upstream, hangs on EOS) and the packaged
**simple-whip-client** (incompatible with Galene, see above).

## What makes WHIP clients "tricky", and how this repo handles it

- **ICE gathering vs. POST timing.** WHIP wants the offer POSTed once. A
  non-trickle client must wait for gathering to complete, and an
  unreachable STUN server can stall that for a long time. `whip.py` waits
  for `ice-gathering-state=complete` but posts after 5 s regardless.
  Galene doesn't need client-side STUN (see [galene-whip.md](galene-whip.md#network)).
- **Teardown.** Without a DELETE, Galene keeps the dead publisher for
  around 30 s, and listeners hear silence. Send a DELETE on shutdown,
  i.e. handle SIGINT/SIGTERM. All Python examples do.
- **Error reporting.** Galene answers an unknown token with *404* (the
  token lookup fails with "file does not exist"), an expired token with
  401, and a token without `present` with 403. Log the status code; the
  body of the 404 is an HTML page.
- **Dead-peer detection.** Nothing in RTP notices a server restart. ICE
  consent freshness (RFC 7675) does, but only in newer stacks: GStreamer
  1.26 reports `connection-state=failed` after about 30 s, while 1.20
  (libnice 0.1.18) **never** does. `whip.py` therefore also checks the
  WHIP session itself: every 5 s it sends `OPTIONS` to the session URL.
  Galene answers 404 once it no longer knows the session, after a restart
  or when an operator kicks the publisher. Three network errors in a row
  count as a failure too. This gets audio back about 5 s after a Galene
  restart on any GStreamer version.
- **Stuck in "connecting".** If the server rejects the media or UDP is
  blocked, webrtcbin stays in `connecting` forever. `whip.py` gives up
  after 15 s and retries.
- **Signal handlers vs. webrtcbin locks.** Don't do blocking work (HTTP)
  or call back into webrtcbin from its signal handlers. `whip.py` does
  the POST on a helper thread.
- **GStreamer 1.20 + PyGObject quirks.** `get-transceivers` returns a
  `GArray` PyGObject can't iterate, and `GstValueList` fields (e.g.
  spectrum magnitudes) can't be read directly. The code works around
  both.
- **Galene stats.** `/galene-api/v0/.stats` reports tracks only for
  WebSocket clients. A WHIP publisher shows up as a bare `{"id": ...}`.
  To monitor a WHIP stream, listen to it (`test/galene_listener.py`) or
  watch the client's own logs.

## Upstream references

- whipclientsink: <https://gstreamer.freedesktop.org/documentation/rswebrtc/whipclientsink.html>
- whipsink (deprecation note): <https://gstreamer.freedesktop.org/documentation/webrtchttp/whipsink.html>
- Rename of whipwebrtcsink → whipclientsink, whipsink deprecation:
  <https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs/-/merge_requests/1282>
- simple-whip-client: <https://github.com/meetecho/simple-whip-client>
- WHIP: [RFC 9725](https://www.rfc-editor.org/rfc/rfc9725)
