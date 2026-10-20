# galene-gstreamer-ingest

Push audio from GStreamer into a [Galene](https://github.com/jech/galene)
group over WHIP, e.g. a voctomix mix with simultaneous interpretation,
one Galene group per language. Contents:

- **Documentation**: how Galene's WHIP endpoint and tokens work, which
  GStreamer WHIP client to use and why, and how to hook it into voctomix.
- **Examples**: plain `gst-launch-1.0` pipelines, Python/PyGObject (the
  way voctomix builds pipelines), and two voctomix integrations.
- **Test harness**: a local Galene in Docker, every client variant, and a
  listener that joins the Galene group and checks it actually hears the
  tone that was sent.

## TL;DR

```sh
# 1. a token with "present" permission for the group (admin account needed;
#    or type "/invite whip-ingest 30d" in the group as operator)
tools/galene_token.py --url https://c3listen.lassitu.de --group test \
    --admin-user admin --admin-password-file pw.txt create --username "whip-test" --days 30

# 2. push a test tone (GStreamer >= 1.20, PyGObject, plugins-bad, gstreamer1.0-nice)
examples/python/whip_push.py \
    --url https://c3listen.lassitu.de/group/test/.whip --token TOKEN

# 3. or the same with a gst-launch one-liner (needs gst-plugins-rs, e.g. Homebrew)
WHIP_URL=https://c3listen.lassitu.de/group/test/.whip WHIP_TOKEN=TOKEN \
    examples/gst-launch/whipclientsink.sh
```

Then open <https://c3listen.lassitu.de/group/test/> and listen.

**Recommended client: webrtcbin + [`examples/python/whip.py`](examples/python/whip.py).**
It works with distro GStreamer 1.20–1.28, including voctomix's Ubuntu 22.04
image. It is the only tested client that notices when Galene goes away and
reconnects on its own. See [docs/gstreamer-whip-clients.md](docs/gstreamer-whip-clients.md)
for the comparison and test results.

**Recommended voctomix integration: the sidecar**
[`examples/voctomix/voctomix_to_galene.py`](examples/voctomix/voctomix_to_galene.py).
It reads voctocore's blinded TCP output and publishes selected channels to
one Galene group each, without touching voctocore. See
[docs/voctomix-integration.md](docs/voctomix-integration.md).

## Documentation

| | |
|---|---|
| [docs/galene-whip.md](docs/galene-whip.md) | Galene's WHIP endpoint, **authentication** (stateful tokens via `/invite`, galenectl or the API; JWT; anonymous), group config, network/TURN |
| [docs/gstreamer-whip-clients.md](docs/gstreamer-whip-clients.md) | webrtcbin vs. whipclientsink vs. whipsink vs. simple-whip-client: test results, recommendation, pitfalls |
| [docs/voctomix-integration.md](docs/voctomix-integration.md) | Sidecar vs. in-core node, channel mapping, required Dockerfile changes |

## Examples

| File | |
|---|---|
| [examples/gst-launch/whipclientsink.sh](examples/gst-launch/whipclientsink.sh) | `gst-launch-1.0` with whipclientsink (gst-plugins-rs) |
| [examples/gst-launch/whipsink.sh](examples/gst-launch/whipsink.sh) | `gst-launch-1.0` with whipsink (gst-plugins-rs, deprecated upstream) |
| [examples/gst-launch/simple-whip-client.sh](examples/gst-launch/simple-whip-client.sh) | Meetecho's `whip-client` CLI (Debian/Ubuntu package). **Does not work with Galene**, kept for reference |
| [examples/python/whip.py](examples/python/whip.py) | WHIP client library on webrtcbin: `WhipSession`, `WhipPublisher` (reconnecting) |
| [examples/python/whip_push.py](examples/python/whip_push.py) | CLI: any `--source`, any of the three in-pipeline clients via `Gst.parse_launch` |
| [examples/voctomix/voctomix_to_galene.py](examples/voctomix/voctomix_to_galene.py) | Sidecar: voctocore TCP output → per-group channel selection → WHIP |
| [examples/voctomix/galene_whip_output.py](examples/voctomix/galene_whip_output.py) | voctocore `AVIONode` publishing `audio-mix` from inside voctocore |
| [tools/galene_token.py](tools/galene_token.py) | Create/list/revoke Galene tokens through the admin API |

All examples take raw audio from any source. Replace the test tone with,
e.g., `AUDIO_SRC="pulsesrc device=..."` or `--source "alsasrc device=hw:1"`.

## Test harness

Requires Docker. The first build compiles gst-plugins-rs and takes a
while (about 20 min, needs a few GB of RAM in the Docker VM).

```sh
test/run-tests.sh                  # everything: all clients, GStreamer 1.20 + 1.26, Galene restart
docker compose up -d --wait galene
docker compose run --rm client python3 -m pytest -v test/ -k webrtcbin
test/resilience.sh webrtcbin       # restart Galene while publishing
```

| | |
|---|---|
| `docker/galene/` | Galene 1.2.1 built from source; groups `test` and `test2`; admin `admin:admin`; plain HTTP on :8443 |
| `docker/client/` | Debian trixie, GStreamer 1.26, gst-plugins-rs 0.14.5 (whipclientsink, whipsink), simple-whip-client |
| `docker/client-jammy/` | Ubuntu 22.04, GStreamer 1.20, distro packages only (= voctomix's base image) |
| [test/test_whip.py](test/test_whip.py) | per client: publish → listener hears 440 Hz → Ctrl-C → stream gone at once; bad token rejected; voctomix sidecar routes channels to the right groups and survives a voctocore restart |
| [test/galene_listener.py](test/galene_listener.py) | joins a Galene group over the WebSocket protocol, decodes the audio, checks the tone frequency |
| [test/resilience.sh](test/resilience.sh) | restarts Galene mid-stream, measures time until audio returns |
| [test/fake_voctocore.sh](test/fake_voctocore.sh) | stand-in for voctocore's Matroska TCP output (4 channels, 440/880 Hz) |

### Against the real server

No admin account is needed, only a token for the group. The listener
joins anonymously, which works on groups with a wildcard `observe` user,
like `test` on c3listen.lassitu.de.

```sh
GALENE_URL=https://c3listen.lassitu.de GALENE_GROUP=test GALENE_TOKEN=TOKEN GALENE_ADMIN= \
    docker compose run --rm --no-deps client python3 -m pytest -v test/ -k "teardown"
```

Without an admin account, the checks that need Galene's stats API (group
membership, clean teardown) are skipped. Audio is still verified by the
listener.
