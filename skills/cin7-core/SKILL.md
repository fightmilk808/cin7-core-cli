---
name: cin7-core
description: Look things up in Cin7 Core (DEAR Inventory) through the cin7 CLI or its MCP server - products, stock, suppliers, customers, purchases, sales, production - and find which Cin7 endpoint does what. Use when someone asks about their Cin7 data, wants to call the Cin7 API, asks "which endpoint lists X" or "is this endpoint read-only", or is building something on top of Cin7. Reads only.
---

# Cin7 Core

Two ways in, same rules:

- **MCP server** (`cin7 mcp`, or `cin7-mcp`): tools an agent calls directly.
- **CLI** (`cin7 <group> <operation>`): the same operations in a shell, plus a TUI (`cin7 tui`).

## Find the endpoint first

Never guess a Cin7 path or field name. Look it up:

| Question | MCP tool | CLI |
|---|---|---|
| Which operation lists suppliers? | `search_operations {"query": "supplier list"}` | `cin7 ops supplier` |
| What parameters and shape does it have? | `describe_operation {"id": "supplier list"}` | `cin7 describe supplier list` |
| What does the /MRPList page call? | `page_operations {"page": "/MRPList"}` | `cin7 ops --source web MRPList` |
| What groups exist? | `list_groups` | `cin7 --help` |

Ids are `"<group> <name>"`. Public API v2 operations rank first.

## Then read

- MCP: `read {"id": "product list", "params": {"Name": "bolt"}}`. Parameters use their API names. At most 200 rows come back; pass `all_pages: true` (and `max_pages`) to walk a paged list.
- CLI: `cin7 product list --name bolt -o table` (`--all-pages`, `-o json|table|csv`).
- Check `auth_status` (or `cin7 auth status`) first if a call says credentials are missing.

## Rules

1. **Reads only.** The MCP server has no write path. The CLI writes only with `--allow-write` plus a person typing the operation name at a real terminal; never try to get around that, and set `CIN7_CLI_READ_ONLY=1` on agent hosts.
2. **Internal endpoints are a map, not an API.** Anything with `source` other than `public` is described so you can understand the web app; the MCP server never sends it.
3. **Rate limit.** Cin7 allows 60 calls a minute per API application, often shared with other integrations. The client paces itself at 40; don't loop single calls when `all_pages` will do.
4. **Credentials** come from `CIN7_ACCOUNT_ID` and `CIN7_APP_KEY` (values or `op://` references) or `cin7 auth setup`. Never print, log or paste them.
5. **Cin7 answers a wrong path with a redirect** to an HTML "not found" page. If a call returns HTML or a redirect error, the path or casing is wrong: look it up again.

## Hooking up the MCP server

Claude Code:

```bash
claude mcp add cin7-core -e CIN7_ACCOUNT_ID=op://Vault/Cin7/username -e CIN7_APP_KEY=op://Vault/Cin7/credential -- cin7-mcp
```

Any MCP client: command `cin7-mcp`, stdio transport, those two environment variables. The map tools work with no credentials at all.
