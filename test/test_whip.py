"""End-to-end tests: push a 440 Hz tone over WHIP, listen to it via Galene.

Runs inside the client container (see test/run-tests.sh):

    docker compose run --rm client pytest -v test/

Environment:
    GALENE_URL     server base URL (default http://galene:8443)
    GALENE_GROUP   group to publish to (default test)
    GALENE_ADMIN   "user:password" of a server admin; used to mint a token
                   and to read /galene-api/v0/.stats. Leave empty when
                   testing a server you have no admin account on.
    GALENE_TOKEN   WHIP token to use instead of minting one
    LISTEN_USER / LISTEN_PASSWORD   credentials the listener joins with
    WHIP_CLIENTS   comma-separated subset of the clients below
"""

import base64
import datetime
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = os.environ.get("GALENE_URL", "http://galene:8443").rstrip("/")
GROUP = os.environ.get("GALENE_GROUP", "test")
ADMIN = os.environ.get("GALENE_ADMIN", "")
WHIP_URL = "{}/group/{}/.whip".format(URL, urllib.parse.quote(GROUP))
PY = sys.executable

CLIENTS = {
    "webrtcbin-python": [PY, "examples/python/whip_push.py", "--client", "webrtcbin"],
    "whipclientsink-python": [PY, "examples/python/whip_push.py", "--client", "whipclientsink"],
    "whipsink-python": [PY, "examples/python/whip_push.py", "--client", "whipsink"],
    "whipclientsink-gst-launch": ["sh", "examples/gst-launch/whipclientsink.sh"],
    "whipsink-gst-launch": ["sh", "examples/gst-launch/whipsink.sh"],
    "simple-whip-client": ["sh", "examples/gst-launch/simple-whip-client.sh"],
}
selected = os.environ.get("WHIP_CLIENTS")
if selected:
    CLIENTS = {k: v for k, v in CLIENTS.items() if k in selected.split(",")}


def admin_request(method, path, body=None):
    user, _, pw = ADMIN.partition(":")
    h = {"Authorization": "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(URL + "/galene-api/v0/" + path, data=data, method=method, headers=h)
    return urllib.request.urlopen(req, timeout=10)


def group_clients(group=GROUP):
    """Number of clients in a group, from Galene's stats.

    Galene only reports track statistics for WebSocket clients; a WHIP
    publisher shows up as a bare {"id": ...}. So this counts clients, and
    whether audio actually flows is checked by the listener.
    """
    with admin_request("GET", ".stats") as r:
        stats = json.load(r)
    for g in stats:
        if g["name"] == group:
            return len(g.get("clients") or [])
    return 0


def wait_for(pred, timeout, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return pred()


def mint_token(group, username="whip-test"):
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
    body = {"permissions": ["present"], "username": username,
            "expires": expires.isoformat(timespec="seconds")}
    with admin_request("POST", ".groups/{}/.tokens/".format(group), body) as r:
        return r.headers["Location"]


@pytest.fixture(scope="session")
def token():
    if os.environ.get("GALENE_TOKEN"):
        return os.environ["GALENE_TOKEN"]
    if not ADMIN:
        pytest.skip("need GALENE_TOKEN or GALENE_ADMIN")
    return mint_token(GROUP)


@pytest.fixture(autouse=True)
def group_is_empty():
    """Each test starts with nothing published in the group."""
    if ADMIN:
        # 45s covers a publisher a previous test could not tear down
        # cleanly; Galene drops it once ICE consent times out.
        assert wait_for(lambda: not group_clients(), 45), "group is not empty"


class Publisher:
    def __init__(self, name, cmd, token=None):
        env = dict(os.environ, WHIP_URL=WHIP_URL)
        if token:
            env["WHIP_TOKEN"] = token
        self.name = name
        self.log_path = "/tmp/{}.log".format(name)
        self.log = open(self.log_path, "w")
        self.started = time.monotonic()
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=self.log,
                                     stderr=subprocess.STDOUT, start_new_session=True)

    def stop(self, sig=signal.SIGINT, timeout=10):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, sig)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
        self.log.close()
        return self.proc.returncode

    def output(self):
        if not self.log.closed:
            self.log.flush()
        with open(self.log_path) as f:
            return f.read()


def listen(duration=8, group=GROUP, freq=440):
    cmd = [PY, "test/galene_listener.py", "--url", URL, "--group", group,
           "--duration", str(duration), "--expect-freq", str(freq),
           "--username", os.environ.get("LISTEN_USER", "whip-test-listener"),
           "--password", os.environ.get("LISTEN_PASSWORD", "")]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=duration + 30)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise AssertionError("listener failed:\n" + p.stderr)


KNOWN_FAILURES = {
    # gst-plugins-rs 0.14: whipsink never finishes EOS, so "gst-launch-1.0 -e"
    # hangs on Ctrl-C, has to be killed, and never sends the DELETE. The
    # Python wrapper avoids it by going straight to NULL.
    "whipsink-gst-launch": "whipsink hangs on EOS, no DELETE",
    # simple-whip-client (Debian 0.0~git20240308) offers without
    # a=group:BUNDLE; Galene (pion) rejects the m= section (port 0 in the
    # answer) and the peer connection never connects.
    "simple-whip-client": "offer without BUNDLE, rejected by Galene",
}


@pytest.mark.parametrize("client", [
    pytest.param(c, marks=pytest.mark.xfail(strict=True, reason=KNOWN_FAILURES[c]))
    if c in KNOWN_FAILURES else c
    for c in CLIENTS])
def test_publish_listen_teardown(client, token):
    pub = Publisher(client, CLIENTS[client], token)
    try:
        if ADMIN:
            assert wait_for(group_clients, 20), "publisher never joined:\n" + pub.output()
            print("\n{}: joined the group after {:.1f}s".format(
                client, time.monotonic() - pub.started))
        else:
            time.sleep(5)
        assert pub.proc.poll() is None, "publisher exited:\n" + pub.output()

        result = listen()
        print("{}: listener {}".format(client, result))
        assert result["heard_tone"], "listener did not hear the tone: {}\n{}".format(
            result, pub.output())
    finally:
        rc = pub.stop(signal.SIGINT)

    if ADMIN:
        # A clean shutdown DELETEs the WHIP resource; Galene drops the stream
        # at once instead of waiting for ICE to time out (~30s).
        t0 = time.monotonic()
        gone = wait_for(lambda: not group_clients(), 5, interval=0.2)
        assert gone, "stream still published 5s after SIGINT (no DELETE?), rc={}\n{}".format(
            rc, pub.output())
        print("{}: stream removed {:.1f}s after SIGINT, rc={}".format(
            client, time.monotonic() - t0, rc))


@pytest.mark.parametrize("client", list(CLIENTS))
def test_bad_token_is_rejected(client):
    pub = Publisher(client + "-badtoken", CLIENTS[client], "not-a-valid-token")
    try:
        time.sleep(6)
        alive = pub.proc.poll() is None
        if ADMIN:
            assert not group_clients(), "publisher was accepted with a bad token"
    finally:
        pub.stop()
    out = pub.output()
    print("\n{}: alive after 6s={} (python clients retry, CLIs exit)".format(client, alive))
    # Galene answers an unknown token with 404 ("not authorised: file does
    # not exist" in its log), an expired or wrong-group one with 403.
    assert any(s in out for s in ("401", "403", "404", "Forbidden", "Not Found")), \
        "rejection not visible in output:\n" + out


@pytest.mark.skipif(not ADMIN, reason="needs GALENE_ADMIN to mint tokens for two groups")
def test_voctomix_sidecar_routes_channels(tmp_path):
    """voctomix_to_galene.py: channels 0+1 -> test, 2+3 -> test2."""
    group2 = os.environ.get("GALENE_GROUP2", "test2")
    cfg = {
        "source": {"host": "127.0.0.1", "port": 15000, "channels": 4},
        "streams": [
            {"name": "floor", "url": WHIP_URL, "token": mint_token(GROUP, "floor"),
             "channels": [0, 1]},
            {"name": "translation", "url": "{}/group/{}/.whip".format(URL, group2),
             "token": mint_token(group2, "translation"), "channels": [2, 3]},
        ],
    }
    path = tmp_path / "sidecar.json"
    path.write_text(json.dumps(cfg))
    fake = Publisher("fake-voctocore", ["sh", "test/fake_voctocore.sh", "15000"])
    sidecar = Publisher("sidecar", [PY, "examples/voctomix/voctomix_to_galene.py", str(path)])
    try:
        assert wait_for(lambda: group_clients() and group_clients(group2), 20), sidecar.output()
        floor = listen(group=GROUP, freq=440)
        translation = listen(group=group2, freq=880)
        print("\nfloor {}\ntranslation {}".format(floor, translation))
        assert floor["heard_tone"], sidecar.output()
        assert translation["heard_tone"], sidecar.output()
        # The routing must not leak: 880 Hz is not in the floor group.
        assert not listen(duration=4, group=GROUP, freq=880)["heard_tone"]

        # voctocore restarts: the sidecar reconnects and the streams stay up.
        fake.stop()
        time.sleep(3)
        assert sidecar.proc.poll() is None, sidecar.output()
        fake = Publisher("fake-voctocore", ["sh", "test/fake_voctocore.sh", "15000"])
        assert listen(group=GROUP, freq=440)["heard_tone"], sidecar.output()
    finally:
        sidecar.stop()
        fake.stop()
    assert wait_for(lambda: not group_clients() and not group_clients(group2), 5), \
        sidecar.output()
