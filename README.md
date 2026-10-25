# limnoria-mcp

A **FastMCP bridge for dynamically authored Safe Tcl scripts in Limnoria**, with
hot reload and a companion mcp-js NixOS service.

The Python bridge is immutable. Agents can create/edit **Tcl only**—there is no
Python-plugin write/load/reload tool, arbitrary IRC dispatcher, or generic shell.

## Architecture

```text
MCP client → authenticated mcp-js → independent stdio controller
                                ├─ confined Tcl script storage (works offline)
                                ├─ authenticated loopback → immutable bot bridge
                                │     └─ bot-only Unix socket → separate worker UID
                                │          └─ fresh Safe Tcl child per invocation
                                └─ fixed service helper → one-unit lifecycle broker
```

Standalone FastMCP serves `/mcp` on `127.0.0.1:8765`. A bearer token is required;
non-loopback Host headers and all browser Origin headers are rejected. Bodies
are bounded. The stdio controller connects to the bot only on demand, so script
repair and service recovery remain available while the bot is down.

## Dynamic scripts and hot reload

| Tool | Effect |
|---|---|
| `list_scripts` / `read_script(name)` | Inspect named Tcl source |
| `write_script(name, source)` | Atomically save candidate source; no activation |
| `validate_script(name)` | Isolated staging; discard every reply/state change |
| `activate_script(name)` | Validate then atomically activate a new generation |
| `script_status` | Active generation numbers and source hashes |
| `service_action` | Exactly start, stop or restart the configured bot service |
| `service_status` / `service_logs` | Status and bounded logs for that service only |

Names match `[A-Za-z][A-Za-z0-9_]{0,48}`. Each directory contains only an
agent-accessible `script.tcl`, at most 64 KiB. Traversal, symlink reads, hardlink
reads and special files are rejected. Atomic replacement cannot truncate an
external linked target. There is no filename parameter or Python escape hatch.

Example source for `Hello`:

```tcl
reply "Hello [context_get nick]!"
state_set last_user [context_get nick]
```

Create it with `write_script`, inspect `validate_script`, then call
`activate_script`. A Limnoria owner can invoke it in IRC:

```text
tclrun Hello optional input text
```

Edit and activate the same name to hot-reload **without restarting the bot**.
Invalid updates leave the last good generation active. Validation runs with a
synthetic context and empty state; it cannot prove every context-dependent branch
will succeed. Execution failure discards all buffered replies/state changes.

Each invocation captures one generation. In-flight old invocations may finish
and reply after an activation, but cannot modify the new generation's state.
State is deliberately reset on activation, and all activation/state is currently
in memory: after bot restart, explicitly activate saved scripts again. At most
64 scripts may be active. In optional GitOps mode, the separate updater stages a bounded Tcl-only bundle from
configured `main`; direct MCP activation is disabled. All scripts validate before
one whole-bundle swap. A complete bot-owned last-good snapshot survives rejected
updates and restarts. Rename is the journal commit point: a subsequent directory
fsync failure still activates the matching snapshot and reports uncertain crash
durability rather than falsely claiming the update was rejected. Draft edits still require an external commit/PR step;
the controller does not push or invent Git credentials. Only script data updates,
never the host/service configuration or activation hooks. See
[the updater design](docs/bundle-updater.md).

## Restricted capabilities and isolation

A fresh Tcl safe interpreter exposes only selected computation commands plus:

* `reply text`: at most three nonempty, single-line, control-character-free
  replies, each at most 350 UTF-8 bytes. Destination comes from the real IRC
  invocation—not the script. Only the bot applies successful buffered replies.
* `context_get key`: read explicit `network`, `channel`, `nick`, or `text`.
* `state_get key` / `state_set key value`: per-script-generation state, at most
  16 identifier keys, 1 KiB per value and 16 KiB total.

No `exec`, `open`, `socket`, `load`, filesystem, environment, package loading,
interpreter management, or arbitrary host callbacks are available. Tcl values
are passed as data, never interpolated into a parent interpreter program.
Capability calls and interpreter commands/time are limited. A trusted supervisor
also enforces an external two-second kill/reap deadline and OS CPU, address-space,
process, file-size, file-descriptor and core-dump limits.

**Safe Tcl alone is not an OS security boundary.** Deploy the worker as its own
unprivileged UID with no bot credentials, external IP networking, writable persistent host paths, or
service-control rights. The NixOS module supplies that separation and a bot-only
socket. Local unit tests run synthetic child processes under the test account;
they are not evidence that a live deployment has this isolation. Interpreter or
host callback vulnerabilities remain security-sensitive. Keep the bridge,
worker, broker and configuration administrator-owned and read-only.

The agent cannot access raw mcp-js filesystem/subprocess APIs. Worker scripts do
not get the service-control broker; only the separately authorized controller
can request fixed actions. Logs may contain private IRC content and are not
claimed to be secret-redacted. Stopping/restarting disconnects the bot.

## Installation and configuration

Python 3.11+, Linux and Python's `tkinter` module with Tcl 8.6+ are required.
FastAPI is not a dependency or wrapper.

```sh
python -m pip install .
```

The package registers the immutable `MCPBridge` Limnoria plugin. Supply externally
provisioned settings to the bot/controller (never put token contents in Git/Nix):

* `LIMNORIA_MCP_SCRIPT_ROOT`: existing dedicated absolute Tcl storage directory.
* `LIMNORIA_MCP_TOKEN_FILE`: external bearer token file, at least 32 characters.
* `LIMNORIA_MCP_PORT`: optional bot loopback port (default 8765).
* Controller only: `LIMNORIA_MCP_CONTROL_HELPER`, an immutable fixed helper path.

Do **not** add the script directory to Limnoria's Python plugin search path.
Administrator-provisioned root SGID plus `UMask=0007` supplies shared group access;
application code never explicitly sets SGID bits. Files use mode 0660. Keep the
worker in a different account with no membership in the shared script group.
See [NixOS integration](docs/nixos-integration.md) for the complete isolated setup.
Installing locally does not provision credentials or authorize deployment.

## Tests and verification

```sh
python -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
```

Tests exercise actual Safe Tcl evaluation and hostile scripts, resource failures,
transactional state/replies, dynamic creation and hot reload, old-generation
isolation, MCP HTTP/stdio, filesystem confinement, and fixed service-control argv.
CI separately builds/evaluates Nix and checks isolated VM services. No live IRC,
production credentials or deployment is used. See integration documentation for
current verified versions and remaining runtime gates.

References: [Tcl safe interpreters](https://www.tcl-lang.org/man/tcl9.0/TclCmd/interp.html),
[FastMCP HTTP deployment](https://gofastmcp.com/deployment/http), and
[Limnoria](https://github.com/ProgVal/Limnoria).
