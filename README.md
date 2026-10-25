# cin7-core-cli

A command line for the **Cin7 Core (DEAR Inventory) public API v2**. Every one of the API's 295 operations is a
command, generated from the API map in
[`fightmilk808/cin7-core-rebuild`](https://github.com/fightmilk808/cin7-core-rebuild)
(`data/public-api-shapes.json` + `data/public-api.csv`).

It is **read-only by default**. GETs run. POST, PUT and DELETE need `--allow-write` and a person typing the
operation name at a real terminal (see [Safety model](#safety-model)).

```text
cin7 <group> <operation> [--query-flags] [-o json|table|csv] [--all-pages]
```

| | |
|---|---|
| Groups | 32 (`sale`, `product`, `purchase`, `production`, `stock`, `customer`, `supplier`, `webhooks`, `me`, `crm`, …) |
| Public operations | 295 · 102 reads · 193 writes |
| Internal endpoints | 2,177 mapped · 1,365 sendable reads, the rest listed but never sent |
| Paged lists (`--all-pages`) | 47 |
| Runtime dependencies | none for the CLI (Python 3.10+ standard library); Textual for the optional TUI |

## Install

```bash
pipx install git+ssh://git@github.com/fightmilk808/cin7-core-cli.git
```

Or from a checkout:

```bash
git clone git@github.com:fightmilk808/cin7-core-cli.git && cd cin7-core-cli && pip install -e '.[dev]'
```

## TUI

```bash
pipx install 'cin7-core-cli[tui] @ git+ssh://git@github.com/fightmilk808/cin7-core-cli.git'
cin7 tui
```

![cin7 tui listing sales by customer reference (sample data from the test mock)](docs/tui.svg)

A [Textual](https://textual.textualize.io) terminal UI over the same spec, client, pacing and auth as the CLI. `?` lists every key.

| | |
|---|---|
| **Mode bar** | Always on. Green `WRITES OFF · reads only · API key` for public calls; **red** `INTERNAL · signed-in browser session, acting as you` when the selected operation uses your Cin7 login; amber for reference-only. |
| **Sidebar** | `ctrl+b` hide/show · `[` `]` narrower/wider · `c` compact (drops counts) · `/` filter. Width and compact are remembered. |
| **Writes** | Hidden by default. `m` shows writes and never-sent endpoints (✎ / ⛔) so you can read their shapes. Seeing them never sends them. |
| **Table** | Click a header or `s` to sort (asc, desc, off; numbers sort as numbers) · `h` hide column · `H` show all · `w` cell width 24/60/160 · the identity column (customer reference, SKU, order number or name) plus the next two most important (customer, supplier, SKU, status…) lead and stay locked while it scrolls; every id column sits at the far right · `ctrl+f` search the results as you type. |
| **Filters** | Two compact columns. Parameters with a fixed set of values are **dropdowns** (shipping and pick statuses, every true/false flag such as `IncludeBOM` or `IsActive`); the rest are text. Leave a dropdown on "any" to omit it. The form hides itself after a run: `f` brings it back. Choices come from the API Blueprint's value lists, so internal filters like a BOM type stay text until a value list is known. |
| **Colors** | Whole row: red = backordered, void, cancelled or overdue and still open; yellow = in progress; green = done. |
| **Links** | The first column of a row is a **bold**, clickable link when Cin7 has a page for it (`/Sale#<id>`, `/Product#…`, `/ProductionOrder#…`, `/MRP#…`; terminals with OSC 8 links). `o` opens it in the browser, `u` copies it. |
| **Copy** | `y` cell · `Y` row as JSON · `i` the record's id · `u` link. |
| **Summary tab** | Count rows by any column (Status, Customer, Supplier, …) with Σ totals for numeric columns, over the rows the search leaves. |
| **Record / JSON tabs** | Collapsible trees colored by type; nested lines (sale lines, BOM components) open on demand. Enter on a table row opens it in Record. |
| **Quick views** | `1` open production orders (completed hidden, soonest due first) · `2` sales waiting on stock · `3` latest MRP run (internal) · `4` sales awaiting shipment. Each fills the form, runs it and sorts it. Status values come from the Blueprint's lists; check them against your data. |
| **Pinned · Recent** | `p` pins the selected operation. Pinned and recently run operations sit at the top of the sidebar. |
| **Saved filters** | `ctrl+s` saves the form under a name ("this week's sales"); `ctrl+o` loads it (Enter) or deletes it (Delete). |
| **History** | `<` `>` step back and forward through earlier results; `k` freezes the current table in a right-hand pane to compare with the next one. |
| **Print** | `Space` marks rows, `ctrl+a` marks all, `P` prints them: see [Printing](#printing). |
| **Downloads tab** | Every export and `⬇` download with time and path, kept across sessions; Enter opens the file. |

Record links come from the route names in the rebuild map's `pages.csv`; open one to check it before relying on it.

![cin7 tui: sales sorted, colored by status, with the mode bar and main groups (sample data from the test mock)](docs/tui.svg)

**Still read-only, full stop.** Write operations can be listed (`m`) so their request shapes can be read, but there is
no way to send one from the TUI.

## Printing

Printing is often buried in Cin7 (a Print menu on a list, a button on a dashboard, a dropdown inside a run). The
map lists every print action and the screens it is on:

```bash
cin7 print where              # all 19: endpoint, one or many ids, template?, the Cin7 screens that carry it
cin7 print where production   # filter
cin7 print templates --type Sale
```

In the TUI the **🖨 PRINT** sidebar section shows the same list. To bulk print from a list view: open Sales,
Purchases, Products, Customers or Production orders, mark rows with `Space` (`ctrl+a` for all), press `P`, pick a
template, then type `print <n>` to confirm. The PDF is saved and listed on the Downloads tab. From the CLI:

```bash
cin7 print send /SaleList/Print --ids <id>,<id> --template <template-id> --dry-run
cin7 print send /SaleList/Print --ids <id>,<id> --template <template-id> --allow-print
```

Printing returns a document and changes no record, but the map marks it "may log an audit line": Cin7 might note that
you printed it. So it runs only as your signed-in browser session, only after `--allow-print` (CLI) or the typed
confirmation (TUI), and never as part of a plain read. Request formats (form-encoded, comma-separated ids) are
inferred from the map and **not yet verified live**: use `--dry-run`, then print one document first.

Not mapped, so not sendable: the production **run** "Print pick list" button, `/ProductionOrder/Print`, the RMA
prints and `/Purchase/PrintReceiveTo` (the map lists no id parameter for them). The production orders **list**
print (`/ProductionOrdersList/Print`, `orderIDs`) is mapped.

## Internal endpoints (the web app's own API)

Besides the 295 public operations, the spec carries every endpoint from the rebuild map's `endpoints.csv` (2,177),
plus the WMS, MES and report-service lists, under `web-<module>` groups (`web-mrp`, `web-sale`, `web-production`, …).
In the TUI they sit in their own purple **INTERNAL** section below the public API; the five main public groups
(Products, Sales, Transfers · Stock, Production, Purchases) are pinned above everything in bold amber.

| Marker | Meaning |
|---|---|
| plain / `↻` / `⬇` | sendable read · read sent as POST with a JSON query body · file download or export (saved to disk) |
| ✎ (amber) | public API write: viewable, never sent from the TUI |
| ⛔ (struck through) | internal endpoint the map doesn't call a read (`mrp-run`, `order-release`, …): listed, **never sent** |
| `·` (dim) | WMS / MES / report-service, reference only |

**Reads only, no exceptions.** An internal endpoint is sendable only if the map marks it a read and its name isn't a
run, void, release, import, send and so on. There is no flag to send the rest. Print endpoints (which the map says
may log an audit line in Cin7) are left out too; downloads and exports are in.

Internal endpoints use your signed-in browser session, not the API key. Copy the `Cookie` header from a Cin7 request
in DevTools (Network tab), store it as a password in 1Password, and save a reference to it:

```bash
cin7 auth setup --cookie-ref 'op://<vault>/<item>/password'
cin7 web-mrp mrp-v-mrp --all-pages -o table      # MRP runs
cin7 web-mrp mrp-results --mrp-id <id>
cin7 ops --source web --reads mrp
```

The session expires like the browser's does (the CLI says so on the redirect to the login page). These endpoints are
unofficial and can change without notice.

## Auth

Cin7 wants two headers on every call, `api-auth-accountid` and `api-auth-applicationkey`. Save **1Password
references** to them once:

```bash
cin7 auth setup --account-ref 'op://<vault>/<item>/username' --key-ref 'op://<vault>/<item>/password'
cin7 auth status    # where each credential comes from, whether writes are off (never values)
cin7 auth test      # one GET /me: "ok: connected to <Company>"
```

That writes `~/.config/cin7-cli/config` (mode 600) with the references only; it refuses anything that isn't an
`op://` reference, and it turns writes off (`CIN7_CLI_READ_ONLY=1`) unless you pass `--allow-writes`. Each run
resolves both references with **one** `op inject` call, so it costs one 1Password approval, and it works from any
shell, including ones that don't read `~/.zshrc`.

Environment variables `CIN7_ACCOUNT_ID` / `CIN7_APP_KEY` override the file and can hold either references or values.
`op run -- cin7 tui` resolves environment references once for a whole session.

The values are never written to disk, never printed, and are masked (`***`) if Cin7 ever echoes them back in an
error. `.env.example` lists every variable; keep real values out of the repo.

| Variable | Default | |
|---|---|---|
| `CIN7_ACCOUNT_ID`, `CIN7_APP_KEY` | none | value or `op://` reference |
| `CIN7_RATE_PER_MIN` | `40` | calls per minute, capped at 60 |
| `CIN7_CLI_READ_ONLY` | unset | set to `1` to switch writes off outright |
| `CIN7_BASE_URL` | `https://inventory.dearsystems.com/ExternalApi/v2` | point at a mock server for testing |
| `CIN7_CLI_STATE_DIR` | `~/.cache/cin7-cli` | where the shared pacing file lives |

`CIN7_ACCOUNT_ID`, `CIN7_APP_KEY`, `CIN7_CLI_READ_ONLY` and `CIN7_RATE_PER_MIN` can also live in the config file;
the environment wins.

## Examples

```bash
cin7 me get                                          # company, base currency, timezone, lock date
cin7 sale list --search '#25147' -o table --fields CustomerReference,OrderNumber,Status,OrderDate
cin7 sale get --id 3f2c…-…                            # full sale with Fulfilments[] Pick/Pack/Ship
cin7 sale list --updated-since 2026-10-01 --all-pages -o csv > sales.csv
cin7 product list --modified-since 2026-10-01 --all-pages
cin7 product get --sku LT-1234 --include-bom true --include-suppliers true
cin7 production order-list --status Released --all-pages -o table
cin7 stock transfer-list --all-pages --max-pages 5
cin7 webhooks list -o table
cin7 raw GET /ref/productavailability -q Sku=LT-1234   # any path, same rules
```

Finding things:

```bash
cin7 --help                     # groups
cin7 sale --help                # operations in a group, each tagged [read] or [WRITE]
cin7 sale list --help           # typed flags for every query parameter
cin7 ops fulfilment             # search every operation (command, method, path, paged)
cin7 ops --reads -o csv         # just the GETs
cin7 describe sale get          # params, request shape and response shape from the Blueprint
cin7 webhook-events Sale/Created
cin7 tui                        # browse and run reads interactively
```

### How commands are named

- **Group** = the API Blueprint group, kebab-cased: `Reference Books` → `reference-books`.
- **Operation** = resource (minus the group's own words) + verb. Verbs: GET → `get`, or `list` for a collection;
  POST → `create`; PUT → `update`; DELETE → `delete`; named actions keep their name (`order-authorise`,
  `run-complete`). So `Sale Fulfilment Pick` POST is `cin7 sale fulfilment-pick-create`.
- Lists that also return one record by `ID` (`/product`, `/customer`, `/supplier`, …) answer to `get` as well.
- **Flags** = query parameters, kebab-cased and typed: `ModifiedSince` → `--modified-since`, `IncludeBOM` →
  `--include-bom true|false`, `Page` → `--page N`. GUID flags are checked; required ones are enforced. Anything the
  map missed can go in with `-q Key=Value`.

### Output

`-o json` (default) prints Cin7's JSON. `-o table` and `-o csv` print the rows of a list (or the one record of a
detail call); nested arrays show as a count (`[3]`). `--fields A,B,C` picks and orders columns.

### Paging

`--all-pages` walks `Page=1,2,…` at `Limit` (default 100) until the list's `Total` is reached or a page comes back
short, and prints one merged envelope `{Total, Pages, <ListKey>: [...]}`. `--max-pages N` caps it. Progress goes to
stderr. Cin7 lists have no stable sort, so dedupe by ID if records may change during the walk.

## Rate limiting

Cin7 allows 60 calls a minute per API application. If your key is shared with other integrations, every call this
tool makes is one they can't. So:

- Calls are spaced at **40 a minute by default** (`--rate`, `CIN7_RATE_PER_MIN`), never more than 60.
- The spacing is **shared by every `cin7` process on the machine** through one locked file, so a shell loop of
  single calls is paced the same as one `--all-pages` run.
- A **429** waits for `Retry-After` (or 5 s, 15 s, 30 s, 60 s), pushes the shared clock back for everyone, and
  retries up to 4 times. A 5xx on a GET is retried the same way; a write is never retried.

## Errors

| Cin7 answer | What you see | Exit |
|---|---|---|
| 302 to `/Error/NotFound` | `HTTP 302: … wrong path or casing` (redirects are not followed) | 1 |
| 400 with `[{"Exception": …}]` | the exception text, e.g. `Sale not found` | 1 |
| 403 | `forbidden: the account id or application key is wrong or revoked` | 1 |
| 429 after retries | `rate limited and retries exhausted` | 1 |
| Write refused by the gate | why, and what would be needed | 2 |
| Bad flag or value | argparse usage | 2 |
| Missing credentials or `op read` failed | which variable | 3 |

## Safety model

Reads are free. A write (POST, PUT, DELETE, from a generated command or `cin7 raw`) is sent only when **all** of
these hold:

1. `CIN7_CLI_READ_ONLY` is not set. Set it on shared machines and agent hosts.
2. The command carries `--allow-write`.
3. The CLI prints the full request (method, URL, body; never the headers), and a person at the **controlling
   terminal** types the operation name back exactly, e.g. `sale create`. The answer is read from `/dev/tty`, not
   stdin, so it can't be piped, scripted or answered by an agent without a terminal.

`--dry-run` prints any request, read or write, without calling Cin7.

> **Shared or production accounts:** set `CIN7_CLI_READ_ONLY=1` wherever the key lives, so the write path is off
> outright.

## Development

```bash
pip install -e '.[dev]'
pytest -q --cov=cin7_cli                 # mock HTTP server only; no live calls
python scripts/build_spec.py ../cin7-core-rebuild   # regenerate src/cin7_cli/spec.json from the API map
```

The tests start a local HTTP server that plays Cin7 (paging, 429 with `Retry-After`, 302 on a wrong path, 400
missing record, 403) and point `CIN7_BASE_URL` at it; every other test gets a closed local port, so nothing can
reach the live API. Writes are exercised only against that mock. A test also checks that the committed `spec.json`
matches what `build_spec.py` produces when the rebuild repo is checked out next to this one.

| Path | What |
|---|---|
| [`scripts/build_spec.py`](scripts/build_spec.py) | API map → `spec.json` (names, typed flags, required marks, list keys) |
| [`src/cin7_cli/cli.py`](src/cin7_cli/cli.py) | argparse commands built from the spec |
| [`src/cin7_cli/client.py`](src/cin7_cli/client.py) | HTTP, no-redirect, retries, error text |
| [`src/cin7_cli/safety.py`](src/cin7_cli/safety.py) | the write gate |
| [`src/cin7_cli/ratelimit.py`](src/cin7_cli/ratelimit.py) | cross-process pacing |
| [`src/cin7_cli/paging.py`](src/cin7_cli/paging.py) | `--all-pages` |
| [`src/cin7_cli/output.py`](src/cin7_cli/output.py) | json / table / csv |
| [`src/cin7_cli/tui.py`](src/cin7_cli/tui.py) | `cin7 tui` (Textual, read-only) |

### Known gaps in the source map

- Two responses were only outlined by the Blueprint parser; their list keys are set by hand in `build_spec.py`
  (`/product` → `Products`, `/production/workcenters` → `Workcenters`).
- In the Blueprint, CRM `Task Category` and `Workflow` GETs both point at `/crm/task`; the commands keep that path
  as published.
- Query parameters that appear only in a path template are typed as text.
- v1-only resources (`/ExternalApi/<Resource>`) are not in the map; reach them with `CIN7_BASE_URL` plus `cin7 raw`.
