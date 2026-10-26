---
name: cin7-mirror-db
description: Mirror chosen Cin7 Core (DEAR Inventory) entities into Postgres or SQLite for reporting, using the cin7 CLI to pull and a small paced sync script to upsert by id, incrementally where the endpoint has a modified/updated-since parameter, on a schedule. Use when someone wants Cin7 data in their own database, a BI tool or SQL over products, sales, purchases, suppliers or customers, a nightly sync, or asks how to keep a local copy of Cin7 current. Reads from Cin7 only; it never writes back.
---

# Mirror Cin7 into a database

The shape: `cin7 <group> list --all-pages -o json` per entity, one table per entity, upsert by id, run on a timer. Cin7 is only ever read.

## 1. Pick entities and their id

Look each one up; don't assume. `cin7 describe <group> <op>` prints `list_key`, `paged`, the params and the response shape.

| Entity | Command | Rows under | Id column | Incremental flag |
|---|---|---|---|---|
| Products | `product list` | `Products` | `ID` | `--modified-since` |
| Suppliers | `supplier list` | `SupplierList` | `ID` | `--modified-since` |
| Customers | `customer list` | `CustomerList` | `ID` | `--modified-since` |
| Product families | `product-family list` | `ProductFamilies` | `ID` | `--modified-since` |
| Sales | `sale list` | `SaleList` | `SaleID` | `--updated-since` (also `--created-since`, `--updated-until`) |
| Purchases | `purchase list` | `PurchaseList` | `ID` | `--updated-since` (also `--updated-until`) |
| Production orders | `production order-list` | `ProductionOrderListItems` | `TaskID` | none: full pull |
| Stock transfers | `stock transfer-list` | `StockTransferList` | `TaskID` | none: full pull |
| Locations, brands, categories, tax, ... | `location list` etc. | see `describe` | `ID` | none: small, full pull |

Everything not marked has no such parameter in the spec; pull it whole. List rows are summaries. Sale and purchase lines need `cin7 sale get --id <guid>`, one call per record: fetch only for rows that changed.

## 2. Create the tables

Two options. Keep the first unless someone needs typed columns now.

- **Raw plus keys** (default): `id text primary key`, the few columns you filter on, `data jsonb` (SQLite: `text`), `synced_at`. Add typed views later.
- **Typed**: take names and types from the `response` shape in `cin7 describe <group> <op>` (`guid`, `string`, `number`, `boolean`, `date`). The [cin7-core-rebuild](https://github.com/fightmilk808/cin7-core-rebuild) map's `modules/*/schema.sql` is the web app's data models, not the API's response rows, so use it for relationships ([docs/RELATIONSHIPS.md](https://github.com/fightmilk808/cin7-core-rebuild/blob/main/docs/RELATIONSHIPS.md)), and `modules/20-public-api/groups/<group>.md` for field types, lengths and allowed values.

## 3. The sync script

```python
import json, os, subprocess, sys, time

def pull(args, key):
    out = subprocess.run(["cin7", *args, "--all-pages", "-o", "json"],
                         capture_output=True, text=True)
    if out.returncode:
        sys.exit(f"cin7 failed ({out.returncode}): {out.stderr.strip()[-300:]}")
    return json.loads(out.stdout)[key]

last_sync = "2000-01-01"   # read from your sync_state table; first run pulls everything
started = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))  # new watermark, one day back
rows = pull(["supplier", "list", "--modified-since", last_sync], "SupplierList")
# upsert: INSERT ... ON CONFLICT (id) DO UPDATE (Postgres), or INSERT OR REPLACE (SQLite)
# in one transaction per entity; store `started` as the watermark only after commit.
```

1. Write the watermark after the commit, not before. A failed run then retries the same window.
2. Subtract a day: `date` flags take a date, the clock on your side and Cin7's can differ, and an upsert absorbs the repeats.
3. First run has no watermark: pull everything once. `--max-pages 2` first is a cheap trial.
4. Entities with no incremental flag: full pull, upsert, then (optionally) delete ids that were not seen in this pull.

## Rules

1. **Reads only.** Set `CIN7_CLI_READ_ONLY=1` where the script runs, and never use `--allow-write` or `cin7 raw` with a non-GET.
2. **Stay under the limit.** Cin7 allows 60 calls a minute per application, often shared. The CLI paces at 40 (`--rate`, `CIN7_RATE_PER_MIN`, cap 60), shared by every `cin7` process on the machine through one lock file. Run entities one after another, not in parallel threads with their own timers.
3. **Credentials** come from `CIN7_ACCOUNT_ID` and `CIN7_APP_KEY` (values or `op://` references, e.g. under `op run --`). Never write them into the script, the schedule file or the database. Database passwords the same way.
4. **Deletes are invisible.** An incremental pull never says a record was removed. Do a full pull of that entity weekly and compare ids.
5. **Rows can repeat or move** between pages while Cin7 is changing (no stable sort). Upsert by id and it does not matter.
6. **Don't mirror what you can query.** For a one-off answer, `cin7 ... -o csv` is enough.

## Schedule it

| Where | How |
|---|---|
| Linux | `cron`/systemd timer running `flock -n /tmp/cin7-sync.lock python sync.py` |
| macOS | a launchd agent with `StartInterval` |
| Anywhere | one run at a time; hourly is plenty for most reports |

Log the row count per entity and the exit code. Exit 3 is missing credentials; 1 is a Cin7 or network error.

## When it fails

| Symptom | Cause |
|---|---|
| Exit 3 | Credentials missing; check `cin7 auth status`. |
| `rate limited and retries exhausted` | Another integration uses the same key; lower `CIN7_RATE_PER_MIN` or run less often. |
| `KeyError` on the list key | Wrong key: read `list_key` from `cin7 describe`. |
| Row counts below Cin7's | Run was stopped by `--max-pages`, or a failed run left the watermark ahead. |
| Duplicate-key errors | Missing unique constraint on the id column. |
