#!/bin/sh
# Full test run against a local Galene in Docker:
#   1. all WHIP clients on Debian trixie (GStreamer 1.26 + gst-plugins-rs 0.14)
#   2. the webrtcbin client and the voctomix sidecar on Ubuntu 22.04
#      (GStreamer 1.20, what voctomix's Dockerfile uses)
#   3. Galene restart while publishing (test/resilience.sh)
#
# Extra arguments go to pytest, e.g. test/run-tests.sh -k webrtcbin
#
# To test against a real server instead (no admin account needed):
#   GALENE_URL=https://galene.example.org GALENE_GROUP=test GALENE_TOKEN=... \
#   GALENE_ADMIN= docker compose run --rm --no-deps client python3 -m pytest -v test/
set -eu
cd "$(dirname "$0")/.."

docker compose --profile client build
docker compose up -d --wait galene

status=0
echo "### Debian trixie, GStreamer 1.26, all clients"
docker compose run --rm -T client python3 -m pytest -v -p no:cacheprovider test/ "$@" || status=1

echo "### Ubuntu 22.04, GStreamer 1.20, webrtcbin client and voctomix sidecar"
docker compose run --rm -T client-jammy python3 -m pytest -v -p no:cacheprovider test/ \
    -k "webrtcbin or sidecar" "$@" || status=1

echo "### Galene restart while publishing"
test/resilience.sh || status=1
SERVICE=client-jammy test/resilience.sh webrtcbin || status=1

exit $status
