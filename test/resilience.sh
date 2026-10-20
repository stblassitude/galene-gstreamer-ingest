#!/bin/sh
# Restart Galene while each Python client is publishing and measure how long
# until a listener hears the tone again. Runs on the host (needs docker).
#
#   test/resilience.sh [client...]      default: webrtcbin whipclientsink whipsink
#   SERVICE=client-jammy test/resilience.sh webrtcbin    (GStreamer 1.20)
#
# Exits non-zero only if webrtcbin does not recover. The gst-plugins-rs
# sinks are known not to (they keep "playing" without posting an error);
# they are run to keep an eye on that, see docs/gstreamer-whip-clients.md.
set -eu
cd "$(dirname "$0")/.."
CLIENTS=${*:-webrtcbin whipclientsink whipsink}
DC="docker compose"
SERVICE=${SERVICE:-client}
RUN="$DC run --rm -T -e GST_DEBUG=1 $SERVICE"

listen_until_heard() {  # $1 = timeout in seconds; prints seconds until heard, or FAIL
    start=$(date +%s)
    while [ $(( $(date +%s) - start )) -lt "$1" ]; do
        if $RUN python3 test/galene_listener.py --url http://galene:8443 --group test \
                --duration 4 >/dev/null 2>&1; then
            echo $(( $(date +%s) - start ))
            return 0
        fi
    done
    echo FAIL
    return 1
}

$DC up -d --wait galene >/dev/null
status=0
for client in $CLIENTS; do
    token=$($RUN env GALENE_ADMIN_PASSWORD=admin python3 tools/galene_token.py \
        --url http://galene:8443 --group test --admin-user admin \
        create --username "resilience-$client" --days 1)
    name="whip-resilience-$client"
    docker rm -f "$name" >/dev/null 2>&1 || true
    $DC run -d --name "$name" -e GST_DEBUG=1 "$SERVICE" python3 examples/python/whip_push.py \
        --client "$client" --url http://galene:8443/group/test/.whip --token "$token" >/dev/null

    before=$(listen_until_heard 30) || true
    $DC restart galene >/dev/null
    $DC up -d --wait galene >/dev/null
    if after=$(listen_until_heard 90); then
        echo "$SERVICE/$client: heard after start ${before}s; after Galene restart ${after}s"
    elif [ "$client" = webrtcbin ]; then
        echo "$SERVICE/$client: heard after start ${before}s; after Galene restart: NOT RECOVERED"
        status=1
    else
        echo "$SERVICE/$client: heard after start ${before}s; after Galene restart: not recovered (known)"
    fi
    docker logs "$name" 2>&1 | grep -E "WARNING|retrying|publishing|playing" | tail -6 | sed 's/^/    /'
    docker stop -t 10 "$name" >/dev/null
    docker rm "$name" >/dev/null 2>&1 || true
done
exit $status
