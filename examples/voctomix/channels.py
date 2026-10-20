"""Channel selection helpers shared by the voctomix examples."""


def audio_caps(channels, rate=48000, fmt="S16LE"):
    """Raw audio caps as voctomix uses them (see [mix] audiocaps).

    No channel-mask: voctomix doesn't set one, and Matroska transport adds
    a default one (e.g. 0x33 for 4 channels), so pinning it breaks linking.
    """
    return "audio/x-raw,format={},layout=interleaved,rate={},channels={}".format(
        fmt, rate, channels)


def inter_caps(channels):
    """Fixed caps for the interaudiosink -> interaudiosrc hop (1 or 2 ch).

    Put the same caps on both sides. interaudiosrc starts producing silence
    with caps it picks on its own; if they differ from what interaudiosink
    later delivers, it renegotiates mid-stream, which webrtcbin does not
    survive.
    """
    if channels == 1:
        return "audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=1"
    if channels == 2:
        return ("audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2,"
                "channel-mask=(bitmask)0x3")
    raise ValueError("Opus over WebRTC carries 1 or 2 channels, not {}".format(channels))


def select_channels(in_channels, channels):
    """gst-launch fragment picking ``channels`` (0-based) out of the input.

    [0, 1] of a 4-channel mix gives stereo from channels 0+1, [2] gives
    mono from channel 2.
    """
    for c in channels:
        if not 0 <= c < in_channels:
            raise ValueError("channel {} not in 0..{}".format(c, in_channels - 1))
    if list(channels) == list(range(in_channels)):
        return ""
    # audiomixmatrix: one row per output channel, one column per input.
    # channel-mask=-1 gives normal mono/stereo positions on the output
    # instead of "unpositioned", which audioconvert won't map to stereo.
    matrix = [[1.0 if col == c else 0.0 for col in range(in_channels)] for c in channels]
    return ('! audiomixmatrix in-channels={} out-channels={} channel-mask=-1 matrix="{}" '.format(
        in_channels, len(channels), str(matrix).replace("[", "<").replace("]", ">")))
