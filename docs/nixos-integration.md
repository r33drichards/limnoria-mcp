# NixOS + FastMCP + Safe Tcl + mcp-js

## Runtime separation

```
authenticated client -> loopback mcp-js companion
  -> immutable stdio FastMCP controller
       -> descriptor-confined named script.tcl files (works with bot down)
       -> authenticated loopback immutable MCPBridge (validate/activate/status)
       -> fixed helper -> root single-unit service-control broker

immutable bot MCPBridge -> bot-only Unix socket
  -> credential-free limnoria-tcl-worker UID
       -> fresh isolated Python / Safe Tcl child per invocation
```

Only **Tcl script data** is agent-writable. There is no API to write Python, load a
Python plugin, import an arbitrary module, or execute a shell. The trusted
MCPBridge Python plugin and all launchers/brokers live in the immutable Nix store.
The managed script directory is **not** a Python plugin search directory.

The bot keeps an active source snapshot. Validating/activating an edited script
uses the isolated worker; failure leaves the previous active snapshot unchanged.
Successful activation swaps only that named script, without restarting the bot.
State is bounded bot memory and resets on activation/restart. Files remain
repairable through the separate controller even if the bot is down. Bot service
start/stop/restart/status/logs likewise remain independent of the bot MCP endpoint.

## Operator configuration (not deployment)

Import this flake's `nixosModules.default` and adapt `examples/nixos.nix`. Native
`services.supybot` runs `python3Packages.limnoria`; this module extends its
`extraPackages` and immutable `plugins.MCPBridge` entry. The initial operator-owned
Limnoria configuration must load MCPBridge and use absolute state paths. Follow
README setup for the explicitly configured script dispatch context.

Provision an operator-approved runtime bearer-token file, initial bot config, and
HTTPS JWKS endpoint separately. The token is delivered via systemd credentials,
never embedded in a Nix store expression. Neither this repository nor CI creates
credentials, accounts, issuer policy, or a real IRC connection. Both HTTP services
bind loopback; no firewall ports are opened. JWKS authentication is not by itself
per-user authorization: only approved administrators should have accepted tokens.

Do not activate this module until deployment, accounts/access, credential
provisioning, IRC use, and the fixed-unit control authority have been approved.
These code changes and tests do not perform those live actions.

## Files and users

* `/var/lib/limnoria-managed-scripts` is root-owned 2770, with a dedicated group
  shared only by `supybot` and `mcp-js-limnoria`. Files are exactly `NAME/script.tcl`;
  descriptor-relative no-follow operations reject traversal, links, special files,
  and unsupported names. No filename or arbitrary path parameter exists.
* New directories request 0770 and inherit SGID/group from the root; both services
  explicitly use UMask 0007. Source files use 0660. Unprivileged code never requests
  SGID chmod, preserving `RestrictSUIDSGID` and Nix sandbox compatibility.
* Only the script directory is an extra writable shared path. The companion has
  no bot-state/group access. The bot is not in the service-control socket group.
* The Tcl worker has its own UID/group, no supplemental groups, no credentials,
  no managed-source access, no bot state, no controller state, and no access to the
  service-control socket. It receives only bounded source/state/context JSON.

## Safe Tcl worker boundary

Systemd owns `/run/limnoria-tcl-worker/worker.sock` as root, mode 0660, group
`limnoria-tcl-client` (bot only). Socket activation supplies the listener fd to the
worker; the worker has no membership in that group and cannot replace the socket.
The broker additionally requires `SO_PEERCRED` to match the exact bot UID.
Requests are single JSON lines, capped at 256 KiB with a three-second read deadline;
the worker applies stricter source/state/request quotas. Responses are capped at
64 KiB. Error responses contain no host exception or script details.

The separate worker unit has PrivateNetwork, AF_UNIX-only networking, read-only
system/home protections, empty capabilities, NoNewPrivileges, RestrictSUIDSGID,
256 MiB aggregate memory, eight tasks, and 50% CPU quota. Bot/controller credential,
state, source, and control-socket paths are explicitly inaccessible. Each actual
Tcl evaluation is a fresh child with CPU, address-space, process, output-file,
open-descriptor and core limits plus an external two-second kill/reap timeout.
No child inherits the listening socket or user-controlled environment/import path.

Safe Tcl starts from `interp -safe`, further removes commands, and grants only
validated reply, bounded state get/set, and explicit context-read capabilities.
It has no filesystem, process, socket, package-loading, arbitrary IRC-command, or
Python evaluation capability. Replies/state are released only after successful
execution. This is defense in depth, not a claim that an interpreter can never
have a vulnerability; do not remove the separate account and OS limits.

Nix packages tkinter separately. To keep the child Python `-I` isolation without
losing Tcl support, the package build replaces one trusted source constant with
an immutable Nix store tkinter site-packages path. It is not taken from an
environment variable, caller parameter, or writable directory.

## Restricted service debugging

The existing root broker checks the companion UID and accepts only
start/stop/restart/status/logs for fixed `supybot.service`. Executable paths and
argv are immutable; no caller shell, executable, environment, unit name, extra
flag, pager, or journal selector is accepted. No sudoers/polkit/journal-group grant
is created. Logs are at most 100 records and 16 KiB with a 30-second deadline;
status exposes only ActiveState/SubState/Result. Logs can contain private bot
content and must not be forwarded blindly. This surface cannot restart or edit
the Tcl worker, controller, or other system services.

## mcp-js source and policies

Public mcp-js revision
[`54ac96cfc54da2cce2951fca83173cef7ba54318`](https://github.com/r33drichards/mcp-js/commit/54ac96cfc54da2cce2951fca83173cef7ba54318)
is pinned. Its actual `nix/options.nix`, CLI/config parser, stdio MCP transport,
and `server/src/engine/opa.rs` were checked: configuration uses `bin/server
--config`, URL-based policy sources, and a static stdio upstream. The mcp_tools
policy allows only the script-development and fixed service-control tools.
Native filesystem, subprocess, external imports, and run-js-file capabilities
are not enabled; guest fetch is explicitly denied. Raw fs prefix rules are not
used because they would not themselves provide symlink/path confinement.

Nixpkgs is revision-pinned to NixOS 26.05
`1f607aa79b6356ed45d3cd3c8079c196e79efa2c`: FastMCP 3.2.3, MCP 1.26.0,
and Limnoria 2026.3.21 (the latter confirmed by CI, distinct from local pip tests).
Native upstream supplies `services.supybot`, not a separate services.limnoria
module. Its restart burst is raised from one to ten per five minutes for debugging.

## Optional main-only automatic bundle updates

Set `services.limnoria-mcp.gitOps.enable = true` only during approved initial host
configuration. Its repository defaults to this public repo; the timer polls main
every five minutes (configurable `gitOps.interval`). It resolves an exact commit
and builds only `packages.<system>.scriptBundle`. The output is flat Tcl data,
never a host configuration, application launcher, activation hook or NixOS rebuild.
There is no per-update approval gate once this explicitly configured mode runs.

The updater is a separate unprivileged UID, never a trusted Nix user (the module
rejects username, wildcard and declared-group trust). It writes only an atomic
bounded candidate document under `/var/lib/limnoria-script-bundles`, mode 0640;
the bot has read-only group access and the controller has none. The bot validates
all candidate scripts through the isolated worker, writes its private durable
last-good snapshot, then atomically swaps the whole live bundle. Rejection keeps
both previous live and durable generations. Candidate publication is **not**
activation acknowledgement: inspect `script_status` for the active revision/error.
Direct `activate_script` is disabled in GitOps mode; MCP edits remain drafts and
cannot bypass main. Valid main reverts provide script rollback without bot restart.

Updater limits and credential denials are separate from the bot/worker. It does
not accept repository flake configuration or lock updates; main must contain a
reviewed real `flake.lock`. CI uploads its generated lockfile for review/commit.
No lockfile is fabricated or the no-lock-update restriction waived. Updater
cgroup/resource limits do **not** impose equivalent limits on nix-daemon workers;
fixed Nix build-limit flags and the host's trusted daemon sandbox/resource policy
are also required. Initial activation must verify actual daemon trust/group
membership and constraints. See [bundle updater details](bundle-updater.md).

## Validation gates

Python tests exercise Tcl capability denial/limits, constrained script storage,
last-good hot activation, protocol/authentication, and broker framing/UID checks.
CI tests minimum/current SDK dependencies on Python 3.11–3.13. Nix checks include
a real Nix CLI contract check, module evaluation, package build, and an isolated
`control-vm` fixture. The contract check imports the updater's exact argv builder,
builds only the current local locked flake's scriptBundle, and validates its actual
Nix store output with the production Tcl-only reader. It also optimises the actual
store when needed and asserts hardlinked files remain accepted only in the
root-owned, read-only store output; mutable candidates remain single-link only.
The runtime updater requires that standard multi-user Nix store ownership model.
The check does not fetch remote
main or publish/activate a candidate. Trusted CI creates and uploads the genuine
lockfile before these checks, including when later checks fail.

The VM uses a harmless synthetic bot service, actual service-control broker,
separate users, both ownership directions of offline script repair, inherited
modes/groups under real RestrictSUIDSGID/UMask, and the actual socket-activated Safe
Tcl worker. It checks valid execution, forbidden commands, runaway rejection,
recovery after failure, controller denial at the Tcl socket, and worker denial at
source/bot-state/control paths. No credential, JWKS request or IRC is used; the
real mcp-js unit is disabled. It is **not** live mcp-js authentication or IRC proof.

The local authoring environment has no Nix. Earlier pre-Tcl CI passed evaluation
but found package portability issues; those fixes are preserved. The redesigned
package/VM still require a fresh CI pass; no Nix runtime success is claimed here.
Generate/review/commit `flake.lock` from Nix CI before GitOps enablement; the CI
artifact is provided for that purpose, and no lockfile is fabricated.
