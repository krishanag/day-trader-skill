---
name: trader-broker
description: >-
  Order Executor. Sends approved orders to Zerodha through the Kite MCP
  server, paper by default, never without your yes. Shows each order as a
  plain ticket (symbol, side, quantity, product MIS, the cover order's
  stop-loss trigger, the max loss in rupees at the stop, the target the
  alerts watch, the flat-by time) plus the exact JSON for the Kite MCP
  place_order tool, records your yes word for word, then Claude's own Allow
  once / Deny card asks again. Paper mode records a simulated fill at Kite's
  last price into the local ledger and never calls the broker. Builds the
  exit ticket that closes a day trade by your flat-by (the exit-cover-order
  call, or a MIS sell plus cancelling the stop leg), keeps positions, fills
  and the log with each trade's max loss so trader-risk can hold the daily
  limit. Covers self-hosting zerodha/kite-mcp-server with your own Kite
  Connect API key for Claude Desktop and Claude Code, and setting the order
  tools to "Needs approval". Use when the user says "place the order", "buy
  it", "execute", "send it to Zerodha", "close my position", "exit", "flatten",
  "get me out", "square off", "connect Kite MCP", "set up paper trading", or
  after /trader-risk passes.
argument-hint: "[order hash | exit SYMBOL | flatten]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches Zerodha only after three
separate gates, every time, for every order:

1. **trader-risk passed this exact order today** (matched by the order hash,
   `sha256` of the Kite body plus the stop leg, first 12 hex digits; any
   change to quantity, trigger, stop leg or symbol is a new order that needs
   a new PASS).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the Kite MCP order tools are set to "Needs approval".

Two yeses from the user, one PASS from the rules. No yes, no order. In paper
mode (`"paper": true`, the default) gate 3 never happens: nothing is sent,
and the fill is simulated at Kite's last price into the local ledger.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Run the tools from the user's project folder: `trader/` and `.env` are
relative to it.

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                                  # every ticket, READY or not, with its hash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --hash e1bd9e7bd8d1 --yes "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --hash e1bd9e7bd8d1              # paper: simulated fill at Kite's last price
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --hash e1bd9e7bd8d1 --status placed --order-id 251029000123 --filled-qty 43 --avg 1174.60
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --hash e1bd9e7bd8d1 --order-id 251029000123 --avg 1174.60 --qty 43 --parent-order-id 251029000123 --stop-order-id 251029000124
python3 "${CLAUDE_SKILL_DIR}/ticket.py" positions
python3 "${CLAUDE_SKILL_DIR}/ticket.py" exit RELIANCE                         # the exit ticket -> trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten                               # exit tickets for every open position
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never calls Zerodha's order endpoints. It renders tickets, checks
that a reply is a real yes for that order, keeps the paper ledger, builds the
exit tickets and writes the log. `--now "2026-10-29 10:15"` pins the clock
(IST). The only network call it ever makes is a quote, for a paper fill.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `exit` and `flatten` replace it with exit tickets) |
| `trader/verdicts.json` | trader-risk's verdicts, keyed by order hash; only a PASS from today is READY |
| `trader/config.json` | `paper`, `budget_inr`, `flat_by`, `order_style` |
| `trader/log.jsonl` | one line per yes, no, placed, denied, failed and filled, with `max_loss_inr`, so trader-risk can hold the daily limit |
| `trader/portfolio.json` | `cash_inr` and `positions` (qty, avg, MIS, the cover order's parent and stop-leg ids), updated by `fill` and `result` |
| `trader/fills.json` | every fill: order hash, Kite order id, quantity, average price, time |
| `trader/plan.json` | the flat-by time for `exit` |
| `trader/prices.json` | the last prices (trader-alerts writes it), for `positions` |

## Paper mode

Zerodha has no paper account, so the pack keeps its own ledger. With
`"paper": true` in `trader/config.json`:

- `show` prints the ticket and the Kite MCP JSON it stands for, labelled
  PAPER, so the user sees exactly what live mode would send.
- After the yes, `fill --hash H` reads Kite's last price for the symbol (one
  quote call through `trader-kite/kite.py`, today's session) and records the
  fill: cash down by quantity times price, a position with that average, a
  line in `trader/fills.json` (Kite order id `PAPER-<hash>`) and a `filled`
  line in the log. No order goes anywhere.
- Exits fill the same way, at the last price, and print the realized rupees.
- Charges (Rs 20 per executed order, STT, exchange fees, GST) are not
  modelled; the paper numbers are a little better than real ones.

Going live is the user's own decision: they set `"paper": false` themselves.
Claude never switches it.

## Setup: Zerodha's Kite MCP server, self-hosted

Zerodha publishes an MCP server at github.com/zerodha/kite-mcp-server (MIT,
Go). Read on 2026-10-29 (latest release v0.4.0-dev24, 2026-10-27). Its README
says the hosted version at `https://mcp.kite.trade/mcp` "excludes potentially
destructive trading operations for security", and that for the other
operations you "generate your own API keys and run the server locally". So
for orders this pack uses a **self-hosted** server with the user's own Kite
Connect API key. The free Personal plan of a Kite Connect app is enough for
orders; the paid Connect plan is what `trader-kite` needs for candles and
quotes, and one app can do both.

1. The user has a Kite Connect app at developers.kite.trade (the same key as
   in `.env`, or a second app: Kite allows one access token per app at a
   time, and the MCP server logs in on its own).
2. Get the binary: a release asset for the platform
   (`kite-mcp-server-darwin-arm64`, `-darwin-amd64`, `-linux-amd64`,
   `-windows-amd64.exe`) from the repo's Releases page, `chmod +x` it; or
   with Go 1.21+: `git clone https://github.com/zerodha/kite-mcp-server`,
   `cd kite-mcp-server`, `go build -o kite-mcp-server`.
3. **Claude Desktop**, stdio mode (the README's "Claude Desktop (stdio
   Mode)" section): Settings > Developer > Edit Config opens
   `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`,
   Linux: `~/.config/Claude/`, Windows: `%APPDATA%\Claude\`). The user adds
   this block (also in `claude_desktop_config.example.json` here) with the
   full absolute path to the binary and types their own key and secret into
   the file:

```json
{
  "mcpServers": {
    "kite": {
      "command": "/full/path/to/your/kite-mcp-server",
      "env": {
        "APP_MODE": "stdio",
        "KITE_API_KEY": "paste your Kite Connect API key here, in this file only",
        "KITE_API_SECRET": "paste your Kite Connect API secret here, in this file only"
      }
    }
  }
}
```

   The README's other modes run the server as a local HTTP (`APP_MODE=http`,
   port 8080, endpoint `/mcp`) or SSE (`/sse`) service and connect Claude
   Desktop through `npx mcp-remote http://localhost:8080/mcp --allow-http`.
   `EXCLUDED_TOOLS=place_gtt_order,modify_gtt_order,delete_gtt_order` in the
   env hides the GTT tools the pack never uses.
   **Keys never go in the chat.** Never ask for them, never read this file
   back into the conversation, and if the user pastes a key anyway, tell them
   to delete that message and regenerate the secret at developers.kite.trade.
4. **Claude Code**: the README has no Claude Code section; the same stdio
   binary registers with
   `claude mcp add kite -e APP_MODE=stdio -e KITE_API_KEY=... -e KITE_API_SECRET=... -- /full/path/to/kite-mcp-server`
   (the user types that command in their own terminal, so the key stays out
   of the chat). Then in `.claude/settings.json` keep the order tools on ask:
   `"permissions": {"ask": ["mcp__kite__place_order", "mcp__kite__modify_order", "mcp__kite__cancel_order"]}`,
   and never put an order tool in `allow`.
5. Restart Claude. The first call in a session is the `login` tool: it
   returns a Kite login link the user opens in their browser (password and
   TOTP on Zerodha's page, never in the chat).
6. **Set the tool permissions** (Claude Desktop: Customize > Connectors, open
   kite, each tool):
   - `place_order`, `modify_order`, `cancel_order`: **Needs approval**. Never
     Always allow. Nothing in this pack places an order except through
     `place_order` after a yes, and nothing exits a cover order except
     through `cancel_order` after a yes.
   - `place_gtt_order`, `modify_gtt_order`, `delete_gtt_order`: Blocked
     (or excluded as above).
   - read-only tools (`login`, `get_profile`, `get_margins`, `get_holdings`,
     `get_positions`, `get_orders`, `get_order_history`, `get_order_trades`,
     `get_trades`, `get_quotes`, `get_ltp`, `get_ohlc`, `get_historical_data`,
     `search_instruments`): Always allow is fine.
7. Check it: ask Claude for `get_profile` (read only) and confirm the user
   name and that `equity` is among the exchanges.

**The tools, as the server's source defines them** (`mcp/post_tools.go`):

- `place_order(variety, exchange, tradingsymbol, transaction_type, quantity,
  product, order_type, price?, validity?, validity_ttl?, disclosed_quantity?,
  trigger_price?, iceberg_legs?, iceberg_quantity?, tag?)`. `variety` is one
  of `regular`, `co`, `amo`, `iceberg`, `auction`. The pack's `kite` body in
  `trader/orders.json` is exactly these parameters.
- `modify_order(variety, order_id, order_type, quantity?, price?,
  trigger_price?, validity?, disclosed_quantity?)`.
- `cancel_order(variety, order_id, parent_order_id?)`. The stock server
  drops `parent_order_id` (its Kite library never sends it, and stdio mode
  could not open a session at all), so the pack runs the patched build in
  `../kite-mcp-server` (its `PATCH.md` has the build and the `claude mcp add`
  command). The exit ticket's Kite body keeps both ids per the pack's
  contract, and the MCP call Claude makes is `cancel_order` with
  `variety: "co"`, the **stop leg's** order id and `parent_order_id` set to
  the entry's order id. If Zerodha rejects it, the user exits the position
  in the Kite app or web (Positions > Exit), and Claude records the fill
  with `ticket.py fill`. The patched server also reads the pack's session
  file (`KITE_SESSION_FILE`), so one morning login serves both.
- `get_orders(from?, limit?)`, `get_order_history(order_id)`,
  `get_positions(from?, limit?)`, `get_margins()`, `login()`.

## MIS, cover orders, the clock

- `product` is always `MIS`: intraday, squared off by Zerodha if still open.
  For stocks with F&O contracts (the ones in NSE's 15:15 Closing Auction
  Session) Zerodha squares off MIS at **15:12** and charges **Rs 50 + GST**
  each time; other stocks at 15:30. The pack's flat-by is **15:05** for
  every stock, so the user exits on their own terms.
- A **cover order** (`order_style: "co"`, the default) is the entry plus a
  stop-loss leg the exchange holds: `variety: "co"` with `trigger_price` as
  the stop. If the trigger is hit, the leg sells at market. Zerodha caps the
  stop within about 10 % of the price. A **regular** order style (for a
  stock where Zerodha does not allow CO) is a plain MIS entry plus a second
  `SL-M` sell as `stop_leg`; the one yes covers the entry and its stop,
  since the stop is what makes the trade safe.
- The target is never an order. trader-alerts rings at it; the user says
  "get me out" and the exit ticket is built.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/trader-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all".
3. Run `ticket.py approve --hash H --yes "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "yes to all", or a yes that names a different
   stock or amount). Ask again. Never turn a reply into a yes yourself.
4. Paper: `ticket.py fill --hash H`. Done; tell the user the simulated fill.
5. Live, only after "Recorded your yes": call the Kite MCP tool `place_order`
   with exactly the JSON the ticket printed. Claude's card appears: Allow
   once or Deny. Then `ticket.py result --hash H --status placed --order-id
   <order_id from the result>`, or `--status denied`, or `--status failed
   --note "..."`. A failed order is not retried without a new check and a
   new yes.
6. Live fill: `get_order_history <order_id>` shows the average price and
   filled quantity; `get_orders` shows the cover order's stop leg (the order
   whose `parent_order_id` is the entry's id). Record:
   `ticket.py fill --hash H --order-id ID --avg P --qty N --parent-order-id ID --stop-order-id LEG`.
   The log keeps the entry's max loss for trader-risk's daily limit.

## Steps: while it is open, and out by the flat-by

- trader-alerts watches the stop, the target and the flat-by
  (`alerts.py add --from-plan`). The cover order's own stop leg does the
  stopping at the exchange.
- When the stop leg fills on its own (the stop alert rang, `get_positions`
  shows the position gone), no MCP call is needed, but the ledger still is:
  `ticket.py exit SYM` builds the exit ticket, the user says yes to it as a
  record, and `ticket.py fill --hash H --order-id LEG --avg P --qty N` (the
  numbers from `get_order_history` on the stop leg) closes the position in
  `trader/portfolio.json`. In paper mode `fill --hash H` does it at the last
  price.
- By `flat_by` (15:05), anything still open gets closed:
  1. Ask Zerodha what is held: `get_positions` (read only); in paper mode,
     `ticket.py positions`.
  2. `ticket.py exit RELIANCE` (or `flatten`) writes the exit ticket to
     `trader/orders.json`: for a cover order the exit-cover-order body
     (`variety co`, `parent_order_id`, `order_id` of the stop leg); for a
     regular order a MIS SELL MARKET plus `cancel_order` for the stop leg.
  3. `/trader-risk`. An exit that only closes what the user holds is never
     blocked by the daily limits or the entry window.
  4. `ticket.py show`, then the user's yes for that one ticket, as above.
  5. Paper: `ticket.py fill --hash H`. Live: the MCP call the ticket shows
     (`cancel_order` with `variety co` for a cover order; `cancel_order` on
     the stop leg then `place_order` for a regular one), Needs approval each
     time, then `result` and `fill` with the ids from the result.

## Example

```
2026-10-29 10:15 IST, NSE open.
ORDER 1 of 1, PAPER, Zerodha Kite, hash e1bd9e7bd8d1
  BUY 43 RELIANCE on NSE, MIS (intraday), cover order, market
  stop-loss trigger 1163.00 (held by the exchange as the cover order's second leg)
  target 1185.80 (trader-alerts watches it; it is not an order)
  max loss about Rs 490.20 if the stop fills at 1163.00 (it can fill worse in a fast market)
  flat by 15:05 IST
  why: opening-range breakout on RELIANCE: 43 shares, bound by your Rs 500 max loss per trade (43 x 11.40 = Rs 490.20 at the stop)
  READY: trader-risk PASS, waiting for your yes
  Paper mode: nothing goes to Zerodha. The Kite MCP call it stands for, place_order:
    {"variety": "co", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
     "order_type": "MARKET", "quantity": 43, "product": "MIS", "trigger_price": 1163.0,
     "validity": "DAY", "tag": "dtsorb"}
  Reply "yes" to place this one order, or "no" to skip it.
```

Real numbers: RELIANCE on 2026-10-29 opened 1159.0, the 15-minute range was
1155.6 to 1170.5, the 09:35 candle closed above it at 1174.4; stop at the
range midpoint snapped down to the 0.10 tick, 1163.0; 11.40 a share at risk;
Rs 500 max loss gives 43 shares. The user replies "yes". In paper mode
`fill` records 43 shares at Kite's last price. Live, Claude calls
`place_order` with that JSON and the card reads "Claude wants to use
place_order" with two buttons: Allow once, Deny. The `why` and the plan stay
in the file; they are never sent.

## How Claude runs it

1. Read `trader/config.json` once and say PAPER or LIVE out loud before the
   first ticket of the day.
2. `show`; relay only READY tickets, one at a time, exactly as printed.
3. `approve` with the user's words, never paraphrased; on "Not recorded",
   ask again.
4. Paper: `fill`. Live: the MCP call with the printed JSON, then `result`,
   then `fill` with the ids from `get_order_history` and `get_orders`.
5. After a fill, hand over to trader-alerts (`add --from-plan`) and come back
   at the flat-by with `exit`.
6. Never run any of this from a scheduled or background task: nobody is
   there to say yes.

## What it never does

- Never places an order without all three gates, and never two orders on one
  yes (the regular style's stop leg is the one exception, and the ticket says so).
- Never places orders from a scheduled or background task.
- Never asks for, reads, stores or repeats API keys, secrets, passwords or TOTPs.
- Never switches `"paper"` to false. The user does that themselves.
- Never calls `modify_order`, the GTT tools, or any order tool on its own.
- Never promises a fill price: a market order and a stop fill at whatever
  the market gives.

## Fine print

- Education and tooling, not financial advice. A PASS is a rules check, not
  a recommendation. Most people who day trade lose money.
- Paper by default (`"paper": true`). Going live is the user's own decision,
  in their own config.
- Nothing in this pack places an order except trader-broker, and nothing is
  placed until the user says "yes" to that exact ticket: trader-risk passed
  it today, the user typed yes in the chat, and the user clicked Allow once
  on Claude's own card because the Kite MCP order tools are set to "Needs
  approval". Never set an order tool to "Always allow".
- No F&O, no short selling, no BSE and no MIS leverage unless the config
  allows it and the user confirms.
- Keys live in `.env`, the environment or the Claude Desktop config, never
  in the chat.
