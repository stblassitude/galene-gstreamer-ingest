# Galene as a WHIP server

Galene has accepted WHIP ([RFC 9725](https://www.rfc-editor.org/rfc/rfc9725))
ingest since 0.8, with ETags and ICE restarts since 1.0. This page covers
what a GStreamer client has to do to publish into a group. Everything here
was checked against the Galene 1.2.1 source (`webserver/whip.go`,
`group/group.go`, `token/`).

## Endpoint

```
POST https://HOST/group/GROUP/.whip
Content-Type: application/sdp
Authorization: Bearer TOKEN
```

- The group must exist. Subgroups work too: `/group/parent/child/.whip`.
- A successful POST returns `201 Created` with the SDP answer, a
  `Location` (the session resource, an opaque encrypted id), an `ETag`, and
  `Link: <...>; rel="ice-server"` headers for any STUN/TURN servers Galene
  is configured with.
- `DELETE` on the `Location` URL ends the publish immediately. Without the
  DELETE, Galene only notices after ICE times out (around 30 s), and
  listeners hear silence until then.
- `PATCH` with `application/trickle-ice-sdpfrag` (trickle ICE and ICE
  restarts) is supported, but not needed: Galene gathers all of its
  candidates before answering, so a non-trickle client works fine.
- `OPTIONS` on the session URL answers 200 while the session exists, and
  404 after a Galene restart or when an operator kicked the publisher.
  `whip.py` polls it to detect dead sessions.
- The offer must use **BUNDLE** (`a=group:BUNDLE`, webrtcbin
  `bundle-policy=max-bundle`). Without it, Galene (Pion) rejects the media
  section (`m=audio 0` in the answer) and no media flows.
- Errors (all tested): an **unknown token gives 404** (Galene's log says
  `not authorised: file does not exist`), an **expired token gives 401**,
  and a valid token without `present` gives 403. No token on a group where
  anonymous users may only listen also gives 403.
- The publisher shows up in the group as a user. Its name comes from the
  token's `username`; tokens without one appear as `whip`.

## Media

- Audio codec: **Opus**. Galene advertises
  `opus/48000/2 minptime=10;useinbandfec=1;stereo=1;sprop-stereo=1`, so
  stereo works end to end. Listeners' browsers decode stereo.
- A group's `"codecs"` list decides what Galene accepts (default
  `["vp8", "opus"]`). An audio-only group can use `["opus"]`.
- The stream is forwarded as-is (SFU). Galene does not transcode, so the
  Opus bitrate you send is what listeners receive. 64 kbit/s stereo is a
  good default for speech plus room, and 32 kbit/s is enough for mono
  speech.
- Galene's web UI treats the WHIP publisher like any other presenter, with
  label `camera`. Listeners need to be allowed to receive audio; observers
  can listen.

## Authentication

Galene checks a WHIP POST exactly like a user joining the group
([`GetPermission`](https://github.com/jech/galene/blob/master/group/group.go)),
with the username fixed to `whip` and the bearer value used as a **token**.
The client needs the `present` permission. That leaves three ways to
authorise a WHIP publisher:

### 1. Stateful token (recommended)

A random string stored on the server (`data/var/tokens.jsonl`), bound to one
group (or a group tree), with permissions, an optional username, and a
**mandatory expiry**. It can be revoked at any time.

You can create one in any of these ways:

- **Galene web UI**: as an operator in the group, type
  `/invite [username] [expiration]` in the chat, e.g.
  `/invite saal1-floor 30d` (units `s`, `min`, `h`, `d`, `mon`, `yr`;
  default 1 d). It prints an invitation link, and the part after `token=`
  is the token. The token gets `present` and `message`, copied from the
  operator's own permissions.
- **galenectl**:
  ```sh
  galenectl -server https://galene.example.org -admin-username admin \
      create-token -group test -user "Saal 1" -duration 720h
  ```
  The default duration is 24 h, which is too short for an event.
- **Admin HTTP API** (what [`tools/galene_token.py`](../tools/galene_token.py)
  does):
  ```sh
  curl -u admin:PASSWORD -X POST -i \
      -H 'Content-Type: application/json' \
      -d '{"permissions":["present"],"username":"Saal 1","expires":"2026-12-31T23:00:00Z"}' \
      https://galene.example.org/galene-api/v0/.groups/test/.tokens/
  # -> 201, header "Location: <token>"
  ```
  Note the path is `/galene-api/v0/.groups/GROUP/.tokens/`. The API
  document (`galene-api.md`) gives a different path under
  `.users/USERNAME/`, but the server and galenectl use this one.

  ```sh
  tools/galene_token.py --url https://galene.example.org --group test \
      --admin-user admin --admin-password-file pw.txt \
      create --username "Saal 1" --days 30
  ```

Things to get right:

- **Set a username in the token.** Without one, the publisher is named
  `whip`. If the group also defines a user called `whip`, the POST fails
  with "duplicate username".
- Use one token per group (or per stream), so a leaked token can be revoked
  without stopping everything. `includeSubgroups` / `-include-subgroups`
  covers a whole group tree, and `-group ''` covers the whole server.
- Tokens expire. Pick an expiry past the end of the event, and make sure
  the publisher logs a rejected token loudly (401 expired, 404 unknown; all clients in this repo do).

### 2. Cryptographic (JWT) token

If the group has `"authKeys"`, Galene also accepts JWTs signed with one of
those keys. The claims are `sub` (username), `aud` (the group URL,
`https://HOST/group/GROUP/`), `permissions` (must include `present`), `exp`,
and optionally `include-subgroups`. This suits an existing authorisation
server. For a fixed ingest box, stateful tokens are simpler.

### 3. No token (testing only)

Without an `Authorization` header, Galene authenticates the user `whip`
with an empty password. That only succeeds if the group has a matching
entry, e.g.

```json
"users": { "whip": { "password": { "type": "wildcard" }, "permissions": "present" } }
```

Then anyone who knows the group name can publish. Don't do this on a
public server.

## Group configuration example

`groups/Saal_1_Floor.json` for a listen-only interpretation channel:

```json
{
    "displayName": "Saal 1 Floor (Original Language)",
    "public": true,
    "codecs": ["opus"],
    "users": {
        "op": { "password": { "type": "bcrypt", "key": "..." }, "permissions": "op" }
    },
    "wildcard-user": { "password": { "type": "wildcard" }, "permissions": "observe" }
}
```

Anyone can join and listen (`observe`). Only token holders publish via
WHIP, and the operator can `/invite` new tokens.

## Network

- WebRTC media flows over UDP between the publisher and Galene's ICE
  candidates. If Galene runs with `-udp-range 8444` (single multiplexed
  port), only that UDP port needs to be open towards the server. With a
  range, the whole range must be open.
- The publisher can sit behind NAT without any STUN/TURN of its own,
  because it reaches out to Galene's public address and Galene learns the
  publisher's address from the incoming packets (peer-reflexive
  candidates). A STUN server on the client side does no harm, but an
  unreachable one slows ICE gathering.
- If outgoing UDP is blocked at the venue, use Galene's TURN server
  (`-turn`). Galene announces it in the `Link` headers of the WHIP
  response, but a client can only use those for a *later* attempt.
  simple-whip-client has `-f` (follow Link headers); for webrtcbin, set
  `turn-server` explicitly.
- Behind a reverse proxy (nginx), `/group/` must be proxied including
  `POST`/`PATCH`/`DELETE`, and the `Location`, `ETag` and `Link` response
  headers must pass through. Galene's own documentation covers the
  `proxyURL` setting.
