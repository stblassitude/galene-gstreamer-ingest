#!/usr/bin/env python3
"""Create, list or revoke Galene stateful tokens for WHIP ingest.

Talks to Galene's admin API, the same requests ``galenectl create-token``
makes, so you need a server admin account (``"permissions": "admin"`` in
data/config.json) or an admin token.

    galene_token.py --url https://galene.example.org --group test \\
        --admin-user admin --admin-password-file pw.txt \\
        create --username "Saal 1" --days 30
    galene_token.py ... list
    galene_token.py ... revoke TOKEN

``create`` prints only the token, so it can be captured in a script.
"""

import argparse
import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def api(args, method, path, body=None, headers=None):
    url = "{}/galene-api/v0/.groups/{}/.tokens/{}".format(
        args.url.rstrip("/"), urllib.parse.quote(args.group, safe=""), path)
    h = dict(headers or {})
    if args.admin_token:
        h["Authorization"] = "Bearer " + args.admin_token
    else:
        cred = "{}:{}".format(args.admin_user, args.admin_password).encode()
        h["Authorization"] = "Basic " + base64.b64encode(cred).decode()
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        return urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        sys.exit("{} {}: HTTP {} {}".format(method, url, e.code,
                                           e.read().decode(errors="replace").strip()))


def cmd_create(args):
    expires = (datetime.datetime.now(datetime.timezone.utc)
               + datetime.timedelta(days=args.days))
    tok = {
        "permissions": args.permissions.split(","),
        "expires": expires.isoformat(timespec="seconds"),
    }
    if args.username:
        tok["username"] = args.username
    if args.include_subgroups:
        tok["includeSubgroups"] = True
    with api(args, "POST", "", tok) as resp:
        print(resp.headers["Location"])


def cmd_list(args):
    with api(args, "GET", "") as resp:
        names = json.load(resp)
    for name in names:
        with api(args, "GET", urllib.parse.quote(name, safe="")) as resp:
            print(name, json.dumps(json.load(resp)))


def cmd_revoke(args):
    path = urllib.parse.quote(args.token, safe="")
    with api(args, "GET", path) as resp:
        etag = resp.headers.get("ETag")
        tok = json.load(resp)
    tok["expires"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    headers = {"If-Match": etag} if etag else {}
    api(args, "PUT", path, tok, headers).close()
    print("revoked", args.token)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default=os.environ.get("GALENE_URL"), required="GALENE_URL" not in os.environ,
                   help="server base URL (env GALENE_URL)")
    p.add_argument("--group", default=os.environ.get("GALENE_GROUP"), required="GALENE_GROUP" not in os.environ,
                   help="group name (env GALENE_GROUP)")
    p.add_argument("--admin-user", default=os.environ.get("GALENE_ADMIN_USER"))
    p.add_argument("--admin-password-file",
                   help="file with the admin password (or env GALENE_ADMIN_PASSWORD)")
    p.add_argument("--admin-token", default=os.environ.get("GALENE_ADMIN_TOKEN"))
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create a token and print it")
    c.add_argument("--username", help="name the stream shows up under in Galene")
    c.add_argument("--permissions", default="present",
                   help="comma-separated, WHIP needs 'present' (default)")
    c.add_argument("--days", type=float, default=30, help="validity (default 30)")
    c.add_argument("--include-subgroups", action="store_true")
    c.set_defaults(func=cmd_create)

    sub.add_parser("list", help="list tokens of the group").set_defaults(func=cmd_list)

    r = sub.add_parser("revoke", help="expire a token now")
    r.add_argument("token")
    r.set_defaults(func=cmd_revoke)

    args = p.parse_args()
    args.admin_password = os.environ.get("GALENE_ADMIN_PASSWORD")
    if args.admin_password_file:
        with open(args.admin_password_file) as f:
            args.admin_password = f.read().strip()
    if not args.admin_token and not (args.admin_user and args.admin_password):
        p.error("need --admin-token, or --admin-user with a password")
    args.func(args)


if __name__ == "__main__":
    main()
