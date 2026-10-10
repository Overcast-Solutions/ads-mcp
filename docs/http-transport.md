# Local HTTP transport

Some MCP clients cannot launch a subprocess and speak only HTTP. For those,
`ads-mcp --transport http` serves the same tools, schemas and audit behavior
over stateless streamable HTTP at `/mcp` instead of stdio. stdio stays the
default and is unchanged; `http` is the only transport value that can be
selected.

## Read-only, by refusal

The endpoint is unauthenticated, so it serves read-only configurations only.
With writes enabled (`ADS_MCP_READ_ONLY=false`) the command refuses before it
binds anything and names the reason. All 35 read tools are available; the
write operations that bring the catalog to 85 are reachable over stdio only.

```bash
ADS_MCP_READ_ONLY=true .venv/bin/ads-mcp --transport http --port 0
```

Two lines go to stderr at startup: the URL being served — for example
`ads-mcp: serving MCP over HTTP at http://127.0.0.1:8000/mcp (read-only)` —
and the residual-risk warning below. Access logging is off, so request lines
are never echoed into the terminal. `SIGINT` and `SIGTERM` shut the listener
down cleanly; a port already in use or a permission error exits 2 without
serving.

## Address

`--host` accepts canonical literal loopback only: `127.x.y.z` (four decimal
octets without leading zeros), `::1` (no brackets, no zone id), or `localhost`,
which binds `127.0.0.1` directly without ever resolving the name — a poisoned
resolver cannot move the endpoint off loopback. Anything else, including a
hostname or `0.0.0.0`, is refused before anything is bound. The default is
`127.0.0.1`.

`--port` takes plain decimal digits from 0 to 65535; the default is `8000`.
Use `--port 0` to take any free ephemeral port and read the one you got from
the startup line. Both options apply to `--transport http`; passing them
without it is an error, because stdio has no address to bind.

## Request protection

Each check below runs before the MCP SDK parses anything, and no refusal
echoes the request back to the caller or into the log:

| Check | Refusal |
| --- | --- |
| `Host` must be `127.0.0.1:PORT`, `localhost:PORT` or `[::1]:PORT` for the bound port | `421` |
| `Origin`, when present, must be the `http` form of one of those authorities; an absent `Origin` is allowed | `403` |
| request bodies over 1 MiB, including chunked bodies that only exceed it while streaming | `413` |
| a POST body that is not a single JSON-RPC object with an absent or string/integer id | `400`, one fixed body |

Together these stop a browser page, and a request arriving for some other
authority, from reaching the server.

## Residual risk

Loopback binding and the `Host`/`Origin` checks are not authentication. **Any
local process on the host can read the configured accounts through this
endpoint**, using the operator's own stored credentials, with no password,
token or prompt. Loopback keeps the endpoint off the network; it does not keep
other local users, other local software or a compromised local tool out.

So run it on single-user hosts only — a workstation whose login you alone use.
On a shared, multi-tenant or jump host, use stdio, where the client launches
the server and the credentials are reachable only through that one process.
Do not publish the endpoint through a reverse proxy, port forward or SSH
tunnel: that turns a single-user convenience into the shared credential-backed
gateway [authentication](authentication.md) warns against. Writes over HTTP
wait for authenticated serving, which is not part of this release.

## Where to go next

- [Configuration](configuration.md) for `ADS_MCP_READ_ONLY` and the rest of the
  environment.
- [Credential sources](credential-sources.md) for where the credentials
  behind this endpoint come from.
- [Operator guide](operator-guide.md) before connecting account data to an MCP
  host or model provider.
