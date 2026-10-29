#!/usr/bin/env python3
"""Daily Zerodha login for the pack (standard library only).

  python3 trader-kite/login.py            open the login link, catch the redirect, save the token
  python3 trader-kite/login.py --paste    paste the redirected URL instead of catching it
  python3 trader-kite/login.py status     is today's session still good?

The login link goes to kite.zerodha.com; you sign in there, in your browser,
never in the chat. Zerodha then redirects to your app's redirect URL with a
one-time request_token. If that URL is on this machine (127.0.0.1 or
localhost) this script listens on its port for a few minutes and catches it;
otherwise, or with --paste, copy the address bar after the redirect and paste it.
The token is exchanged for an access token (sha256 checksum with your API
secret) and saved to trader/kite_session.json, mode 600. It lasts until
06:00 IST tomorrow.
"""

import argparse
import http.server
import os
import sys
import urllib.parse
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kite  # noqa: E402

WAIT_SECONDS = 300


def request_token_from(url_or_token):
    """A redirected URL (or a bare token) -> request_token, or '' if absent / failed."""
    text = url_or_token.strip()
    if "request_token=" not in text:
        return text if text and "/" not in text and "=" not in text else ""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
    if query.get("status", ["success"])[0] != "success":
        return ""
    return query.get("request_token", [""])[0]


def catch_redirect(redirect_url, timeout=WAIT_SECONDS):
    """Listen on the redirect URL's local port; return the request_token or ''."""
    parts = urllib.parse.urlparse(redirect_url)
    if parts.hostname not in ("127.0.0.1", "localhost") or not parts.port:
        return ""
    found = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            found["token"] = request_token_from("http://x" + self.path)
            ok = bool(found["token"])
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("Logged in. You can close this tab." if ok else
                              "Login failed or was cancelled.").encode())

        def log_message(self, *args):
            pass

    try:
        server = http.server.HTTPServer((parts.hostname, parts.port), Handler)
    except OSError as e:
        print("note: port %d is busy (%s); paste the redirected URL instead." % (parts.port, e),
              file=sys.stderr)
        return ""
    server.timeout = timeout
    with server:
        server.handle_request()
    return found.get("token", "")


def exchange(creds, request_token, now):
    if not creds["api_secret"]:
        raise kite.KiteError("KITE_API_SECRET is not set")
    data = kite.http("POST", "/session/token", creds["api_key"], data={
        "api_key": creds["api_key"], "request_token": request_token,
        "checksum": kite.checksum(creds["api_key"], request_token, creds["api_secret"])})
    return kite.save_session(data, now)


def cmd_login(args):
    creds = kite.credentials()
    url = kite.LOGIN % creds["api_key"]
    print("Log in to Zerodha in your browser:\n  %s\n" % url)
    if not args.no_browser:
        webbrowser.open(url)
    token = "" if args.paste else catch_redirect(creds["redirect_url"])
    if not token:
        token = request_token_from(input("Paste the URL you were redirected to: "))
    if not token:
        print("No request_token found: the login failed or was cancelled.", file=sys.stderr)
        return 1
    rec = exchange(creds, token, kite.now_ist())
    print("Logged in as %s (%s). Session saved to %s, good until 06:00 IST tomorrow."
          % (rec.get("user_name") or "?", rec.get("user_id") or "?", kite.SESSION_FILE))
    return 0


def cmd_status(_args):
    k = kite.Kite.connect()
    prof = k.profile()
    print("Logged in as %s (%s); exchanges: %s" % (
        prof.get("user_name"), prof.get("user_id"), ", ".join(prof.get("exchanges", []))))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Zerodha Kite login for the pack.")
    ap.add_argument("command", nargs="?", default="login", choices=["login", "status"])
    ap.add_argument("--paste", action="store_true", help="paste the redirected URL")
    ap.add_argument("--no-browser", action="store_true", help="only print the login link")
    args = ap.parse_args(argv)
    try:
        return cmd_status(args) if args.command == "status" else cmd_login(args)
    except kite.KiteError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
