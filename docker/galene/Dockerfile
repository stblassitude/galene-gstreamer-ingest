# Local Galene server for the test harness, built from a release tag.
FROM golang:1.25-trixie AS build
ARG GALENE_VERSION=galene-1.2.1
RUN git clone --depth 1 --branch "${GALENE_VERSION}" https://github.com/jech/galene /src
WORKDIR /src
RUN CGO_ENABLED=0 go build -ldflags='-s -w' -o /out/galene . \
 && CGO_ENABLED=0 go build -ldflags='-s -w' -o /out/galenectl ./galenectl

FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/galene /out/galenectl /usr/local/bin/
COPY --from=build /src/static /opt/galene/static
COPY config.json /opt/galene/data/config.json
COPY groups/ /opt/galene/groups/
WORKDIR /opt/galene
EXPOSE 8443/tcp 8444/udp
# Plain HTTP (no TLS) and a single multiplexed UDP port; built-in TURN off.
ENTRYPOINT ["galene", "-insecure", "-http", ":8443", "-udp-range", "8444", "-turn", ""]
