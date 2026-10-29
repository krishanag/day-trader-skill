---
name: trader-kite
description: >-
  Zerodha Login. Signs you in to Zerodha Kite Connect once a day and keeps
  the session every other skill in this pack reads data through. Opens the
  Kite login link in your browser (you type your password and TOTP there,
  never in the chat), catches the redirect on 127.0.0.1, exchanges the
  request token for an access token with your API secret and saves the
  session to trader/kite_session.json, mode 600, good until 06:00 IST the
  next day. Also reports whether today's session is still good and which
  exchanges your account has. Use when the user says "log in to Zerodha",
  "connect Kite", "kite login", "am I logged in", "token expired", "set up
  Zerodha", or whenever any other trader skill says it needs a login.
argument-hint: "[login | status] [--paste]"
---

# trader-kite

Every data tool in this pack reads Zerodha Kite Connect: candles, quotes and
the instrument list. Kite hands out an access token that dies at 06:00 IST
the next morning, so the first thing each trading day is this skill. It does
not place orders and it does not read your password: you type that on
Zerodha's own page in your browser.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/login.py"               # open the login link, catch the redirect, save the session
python3 "${CLAUDE_SKILL_DIR}/login.py" --paste       # paste the redirected URL instead of catching it
python3 "${CLAUDE_SKILL_DIR}/login.py" --no-browser  # only print the link (headless machines)
python3 "${CLAUDE_SKILL_DIR}/login.py" status        # is today's session still good?
```

Run the tools from the user's project folder: `trader/` and `.env` are
relative to it.

## What it reads and writes

| file | what |
| --- | --- |
| `.env` in the project folder, or the environment | `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_REDIRECT_URL` (the redirect URL registered on the Kite app, `http://127.0.0.1:5000/callback` for a local catch) |
| `trader/kite_session.json` | written by `login`: `user_id`, `user_name`, `access_token`, `created_at` (ISO, +05:30). Mode 600. Every other tool reads it through `kite.Kite.connect()` |
| `trader/cache/` | instrument lists and the F&O (CAS) list, one file per day |

## Setting up, once

1. Create a Kite Connect app at https://developers.kite.trade (the Connect
   plan, Rs 500 a month, is what gives historical candles and quotes; the free
   Personal plan only places orders). Set its redirect URL to
   `http://127.0.0.1:5000/callback`.
2. Put the three values in `.env` in the project folder:

   ```
   KITE_API_KEY=...
   KITE_API_SECRET=...
   KITE_REDIRECT_URL=http://127.0.0.1:5000/callback
   ```

   `chmod 600 .env`. The pack's `.gitignore` keeps `.env` and `trader/` out
   of git. Never paste a key or a secret into the chat.
3. `python3 trader-kite/login.py`. Your browser opens Zerodha's login; sign
   in with your user ID, password and TOTP. Zerodha sends the browser back to
   127.0.0.1:5000, the script catches the one-time `request_token`, swaps it
   for the access token and saves the session.

## Each trading day

Before 09:15 IST: `python3 trader-kite/login.py`. The session lasts until
06:00 IST the next day, so one login covers the whole session. If a tool
prints "not logged in to Kite" or "the Kite session ... expired", run it
again. If another app of yours uses the same Kite API key, logging in here
may end that app's session for the day: Kite allows one access token per
app at a time.

## How Claude runs it

1. Run `status` first. If it prints the user's name and exchanges, say so
   and stop; nothing else is needed.
2. If it says login is needed, run `login`. Tell the user the browser is
   open and that they sign in there, not in the chat. The script waits up to
   five minutes for the redirect.
3. If the redirect cannot be caught (the port is busy, a remote machine, a
   different redirect URL), run `login --paste` and ask the user to paste the
   full URL from the address bar after Zerodha redirects them. The URL holds
   a one-time token that is useless after the exchange, so pasting it is
   safe; the password is never pasted.
4. Report the saved session in one line: the user name, the user ID and that
   it is good until 06:00 IST tomorrow.

## What it never does

- It never asks for, reads or stores the Zerodha password or TOTP.
- It never places, modifies or cancels an order. Orders go through
  `/trader-broker`, through Zerodha's Kite MCP server, only after the user
  types yes, and Claude's own "Allow once / Deny" card asks again.
- It does not keep the API secret anywhere but `.env` or the environment.

## Fine print

This pack is software that reads market data and does arithmetic. It is not
financial advice. Most people who day trade lose money. Kite Connect is
Zerodha's official API and its data is the same as Kite's, but it can lag or
rate-limit you; the price that fills is your broker's.
