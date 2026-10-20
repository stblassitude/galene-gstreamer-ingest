"""voctocore output node that publishes mix audio to Galene over WHIP.

Drop-in sketch for voctomix (voctocore/lib/), modelled on ProgramOutputSink.
It adds a branch to voctocore's main pipeline string:

    audio-mix[-blinded]. ! queue ! audioconvert ! audiomixmatrix
        ! interaudiosink channel=whip-NAME

and, in attach(), starts a *separate* pipeline in the same process:

    interaudiosrc channel=whip-NAME ! opusenc ! rtpopuspay ! webrtcbin

driven by whip.WhipPublisher. The split matters: voctocore's Pipeline
calls sys.exit() on any bus error, and network trouble on the WHIP side
(server restart, token expiry, Wi-Fi drop) must not end the show. The
WHIP pipeline restarts with backoff while the main pipeline keeps going;
interaudiosrc emits silence while nothing is published.

Only GStreamer >= 1.20 with gst-plugins-bad (webrtcbin, interaudio*,
audiomixmatrix) and gstreamer1.0-nice are needed, which the voctomix
Ubuntu 22.04 image already has once gstreamer1.0-nice is added.

Wiring it into voctocore/lib/pipeline.py, next to the other outputs:

    from voctocore.lib.galene_whip_output import GaleneWhipOutput
    for name in Config.getGaleneOutputs():          # e.g. from [galene] section
        out = GaleneWhipOutput(
            name,
            endpoint=Config.get('galene.' + name, 'url'),
            token=Config.get('galene.' + name, 'token'),
            channels=[int(c) for c in Config.get('galene.' + name, 'channels').split(',')],
            audio_blinded=Config.getBlinderEnabled())
        self.bins.append(out)
        self.ports.append(Port('galene-' + name, out))

The Config accessors are illustrative; voctomix has no [galene] section.
"""

import logging

from gi.repository import Gst

from voctocore.lib.avnode import AVIONode
from voctocore.lib.clock import Clock
from voctocore.lib.config import Config

# Copy whip.py and channels.py next to this file in voctocore/lib/.
from voctocore.lib.whip import WhipPublisher, opus_whip_pipeline
from voctocore.lib.channels import audio_caps, inter_caps, select_channels


class GaleneWhipOutput(AVIONode):
    log: logging.Logger
    source: str
    bin: str

    def __init__(self, name: str, endpoint: str, token: str, channels: list[int],
                 audio_blinded: bool = False, bitrate: int = 64000):
        self.log = logging.getLogger("GaleneWhipOutput[{}]".format(name))
        self.source = name
        self.endpoint = endpoint
        self.token = token
        self.channels = channels
        self.bitrate = bitrate
        self.publisher = None

        in_channels = Config.getAudioChannels()
        caps = inter_caps(len(channels))
        self.inter_channel = "whip-{}".format(name)

        self.bin = """
            audio-mix{blinded}.
            ! queue
                max-size-time=3000000000
                leaky=downstream
                name=queue-galene-whip-{name}
            ! audioconvert
            ! {mixcaps}
            {select}
            ! audioconvert
            ! {caps}
            ! interaudiosink
                name=galene-whip-{name}
                channel={channel}
            """.format(
            blinded="-blinded" if audio_blinded else "",
            name=name,
            mixcaps=audio_caps(in_channels),
            select=select_channels(in_channels, channels),
            caps=caps,
            channel=self.inter_channel,
        )
        self.publish_description = opus_whip_pipeline(
            "interaudiosrc channel={} ! {}".format(self.inter_channel, caps),
            bitrate=bitrate, channels=min(len(channels), 2))

    def attach(self, pipeline: Gst.Pipeline):
        self.pipeline = pipeline
        self.publisher = WhipPublisher(self.publish_description, self.endpoint,
                                       self.token, name=self.source, clock=Clock)
        self.publisher.start()

    def port(self) -> str:
        return self.endpoint

    def num_connections(self) -> int:
        return 1 if self.publisher is not None and self.publisher.connected else 0

    def audio_channels(self) -> int:
        return len(self.channels)

    def video_channels(self) -> int:
        return 0

    def is_input(self) -> bool:
        return False

    def __str__(self) -> str:
        return "GaleneWhipOutput[{}] -> {}".format(self.source, self.endpoint)
