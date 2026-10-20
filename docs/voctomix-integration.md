# Getting voctomix audio into Galene

voctomix builds its GStreamer pipeline as one big `gst-launch` style string.
Each output class (`AVRawOutput`, `ProgramOutputSink`, ...) contributes a
fragment, and `voctocore/lib/pipeline.py` runs all of them through
`Gst.parse_launch()` from **PyGObject** (`gi.repository.Gst`). After that,
each node's `attach(pipeline)` hooks up anything that needs Python code.
The Python examples in this repository use the same approach.

The mixed audio is available inside voctocore on the tee `audio-mix` (or
`audio-mix-blinded` when the blinder is enabled), with the caps from
`[mix] audiocaps`, by default
`audio/x-raw,format=S16LE,channels=2,layout=interleaved,rate=48000`. With
interpretation, the mix usually carries more channels, e.g. 0+1 for the
floor and 2+3 for the interpretation, each going to its own Galene group.

There are two ways to get that audio to Galene. Both use the same WHIP
client ([`examples/python/whip.py`](../examples/python/whip.py)), which
only needs webrtcbin. It works with the GStreamer 1.20 in voctomix's
Ubuntu 22.04 Dockerfile, which has no WHIP element of its own.

## Option A (recommended): sidecar process

[`examples/voctomix/voctomix_to_galene.py`](../examples/voctomix/voctomix_to_galene.py)
connects to one of voctocore's Matroska-over-TCP outputs and publishes one
WHIP stream per configured Galene group:

```
voctocore :15000 (blinded live) ──tcp──▶ matroskademux ─▶ audio ─▶ tee
        ├─▶ audiomixmatrix [0,1] ─▶ interaudiosink ┊ interaudiosrc ─▶ opus ─▶ webrtcbin ─▶ WHIP group A
        └─▶ audiomixmatrix [2,3] ─▶ interaudiosink ┊ interaudiosrc ─▶ opus ─▶ webrtcbin ─▶ WHIP group B
```

```sh
cp examples/voctomix/voctomix_to_galene.example.json /etc/galene-ingest/config.json
# put one token per group into the token files, see docs/galene-whip.md
python3 examples/voctomix/voctomix_to_galene.py /etc/galene-ingest/config.json
```

Why this is the recommended option:

- **No changes to voctomix.** voctocore already serves the stream. With
  `[blinder] enabled = true`, port 15000 carries the blinded mix (15000+n
  for further `[mix] livesources`), so pauses are blinded on Galene too.
  Port 11000 is the unblinded mix.
- **Failure isolation.** voctocore exits on *any* pipeline error
  (`Pipeline.on_error` calls `sys.exit(-1)`). A sidecar can crash,
  restart, or be upgraded mid-show without touching the mixer.
- **Every part reconnects on its own.** If voctocore restarts, the TCP
  source reconnects, and listeners hear silence in the meantime instead
  of losing the stream. If Galene or the network goes away, each WHIP
  stream retries with backoff.
- It can run on a different machine than voctocore.

Run it under systemd with `Restart=always` as a last line of defence:

```ini
[Unit]
Description=voctomix audio to Galene (WHIP)
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/galene-gstreamer-ingest/examples/voctomix/voctomix_to_galene.py /etc/galene-ingest/config.json
Restart=always
RestartSec=2
DynamicUser=yes

[Install]
WantedBy=multi-user.target
```

The test harness checks the sidecar end to end
(`test_voctomix_sidecar_routes_channels`). A fake voctocore output serves
4-channel audio over TCP, 440 Hz on channels 0+1 and 880 Hz on 2+3. The
test checks that the right tone arrives in each Galene group and that the
other tone does not leak into the floor group. It then restarts the fake
voctocore and checks the streams recover.

## Option B: inside voctocore

[`examples/voctomix/galene_whip_output.py`](../examples/voctomix/galene_whip_output.py)
is an `AVIONode` like `ProgramOutputSink`. Copy it together with `whip.py`
and `channels.py` to `voctocore/lib/` and add it in `Pipeline.__init__`
(the module docstring shows the snippet). It splits the work in two:

- Into voctocore's main pipeline string it adds only
  `audio-mix[-blinded]. ! queue leaky=downstream ! audioconvert ! audiomixmatrix ! interaudiosink`.
  None of these can fail because of the network.
- In `attach()` it starts a **separate** `Gst.Pipeline` in the same
  process: `interaudiosrc ! opusenc ! rtpopuspay ! webrtcbin`, run by
  `WhipPublisher`. Its errors go to its own bus, so voctocore's `on_error`
  never sees them. It uses voctocore's `Clock` and restarts with backoff.

This variant has been run inside a real voctocore: voctomix's Ubuntu 22.04
Docker image, a `kind = test` source, and the 1 kHz tone heard by a Galene
listener. After a Galene restart, voctocore kept running and audio was
back after 4 s. Two changes to voctomix's `Dockerfile` are needed:

```diff
         gstreamer1.0-libav \
+        gstreamer1.0-nice \
+        gir1.2-gst-plugins-bad-1.0 \
         gstreamer1.0-plugins-bad \
```

- `gstreamer1.0-nice` provides the ICE transport webrtcbin needs. Without
  it webrtcbin fails to start.
- `gir1.2-gst-plugins-bad-1.0` provides the `GstWebRTC` typelib for Python.

Don't put a WHIP *sink element* (whipclientsink/whipsink) directly into
voctocore's pipeline string. An unreachable server or an expired token
then posts a bus error, and voctocore shuts down mid-show.

## Channel mapping

`channels` in the config (or the `GaleneWhipOutput` argument) lists the
0-based mix channels for one Galene group: `[0, 1]` sends channels 0 and 1
as stereo, `[2]` sends channel 2 as mono. Opus over WebRTC carries one or
two channels, so pick at most two per group. Use a separate group per
language, so that listeners pick a language by picking a group (the same
pattern as the existing `Saal_Meltdown_0_Floor` / `Saal_Meltdown_1_DE_EN`
groups).

Two GStreamer details the examples take care of, which matter if you write
your own:

- `audiomixmatrix ... channel-mask=-1`: otherwise the output is
  "unpositioned" stereo, which `audioconvert` on 1.20 refuses to map to
  normal stereo.
- Identical, fully specified caps on both sides of the
  `interaudiosink`/`interaudiosrc` hop (`channels.inter_caps()`).
  interaudiosrc starts with caps of its own choosing, and if they differ
  from what arrives later it renegotiates mid-stream, which the
  webrtcbin branch does not survive.
