# NixOS + FastMCP + actual mcp-js integration

## Topology and recovery

```
authenticated MCP client
    -> mcp-js-limnoria (127.0.0.1:8766, JWKS verification)
        -> immutable stdio limnoria-mcp-control (same unprivileged UID)
            -> descriptor-confined managed plugin files (works offline)
            -> on-demand authenticated HTTP MCPBridge in supybot (127.0.0.1:8765)
            -> fixed helper -> Unix socket -> root single-unit broker
```

The companion has **no dependency on the bot being up**. The stdio bridge statically
advertises tools and connects to the bot only when a plugin tool is called. This
matters because mcp-js connects configured upstreams at startup: a direct HTTP
upstream would otherwise make cold-start recovery fail while the bot is down.
Start/stop/restart/status/logs and bounded plugin file read/write/list remain
callable while the bot is down. Only load/reload require a live bot. A bot reload does not terminate the separate controller.

## Configuration, not deployment

Import `nixosModules.default` from this flake and adapt `examples/nixos.nix`.
The bridge uses the standalone **FastMCP** framework, not a FastAPI wrapper.
The module uses native **`services.supybot`**, whose Python environment contains
`python3Packages.limnoria`; it extends `extraPackages` and installs immutable
`MCPBridge` using the native `plugins` option. The operator's Limnoria config must
load MCPBridge (`supybot.plugins.MCPBridge: True`) and use absolute state paths,
as required by the upstream module. Generate/review the config with Limnoria's
wizard rather than copying any live credentials into the repository.

Required runtime inputs are an operator-provisioned bearer-token file (at least
32 characters, shared with the bot through systemd credentials), a valid initial
bot config, and an approved HTTPS JWKS issuer for the companion's clients. No
secret values appear in generated Nix store files. The module creates neither a
token nor an identity-provider configuration. Both HTTP listeners are loopback
only, and this module opens no firewall ports. Any reverse proxy/network exposure
and issuer/audience policy require separate review. JWKS configuration trusts
identities accepted by that issuer; it is not a per-user authorization policy.

**Do not run `nixos-rebuild switch` until deployment, account/access changes, token
provisioning, IRC connection, and the root control broker are explicitly approved.**
The committed CI runs builds/tests only, with no real runtime credential or IRC.

## Authority boundaries

* `/var/lib/limnoria-managed-plugins` is root-owned, mode 2770, with a
  dedicated shared group containing only bot and companion users. New plugin
  directories use 2770 and source files 0660. Both units get exactly this shared
  path as an extra ReadWritePaths entry, without sharing the bot-state directory.
  Controller file tools operate directly for offline repair. API file
  operations use descriptor-relative, no-follow opens and constrained filenames.
  MCPBridge and the broker are immutable Nix store code, not managed files.
* `mcp-js-limnoria` is a separate user, **not** in `supybot`, `wheel`, or
  `systemd-journal`. It cannot directly access bot state. Files are read/written
  through the constrained controller MCP API, not `fs` or filesystem-prefix Rego
  rules. Only the managed source directory is shared; bot state remains private.
* The broker accepts only the exact companion UID using `SO_PEERCRED`; the
  root-owned 0750 runtime directory and group-scoped 0660 socket also exclude
  the bot UID. No sudo or polkit rule is installed.
* The helper accepts one enum: start, stop, restart, status, logs. The root broker
  owns the executable paths and fixed `supybot.service` argument. No caller can
  supply a unit, shell, executable, environment, pager, extra flag, or journal
  selector. Logs are at most 100 records and 16 KiB, with a 30-second subprocess
  deadline. Status exposes only ActiveState/SubState/Result. Logs may contain
  private IRC content or secrets emitted by plugins; do not forward them blindly.
* Native mcp-js filesystem, subprocess, external-module and run-js-file access
  are not enabled; outbound guest `fetch` is explicitly denied. Its `mcp_tools`
  policy permits exactly the eight bridge tools. Credentialed stdio bootstrap
  is trusted operator configuration, not guest subprocess access.

**Writable Python plugins grant arbitrary Python execution as the bot user when
loaded/reloaded. This is not a secure sandbox for untrusted code.** Such code can
read bot-account data and credentials or use the bot's IRC connection. UID
separation protects the controller's root control socket from that bot identity;
it does not make a malicious plugin safe. Give MCP access only to trusted bot
administrators, review diffs before load, and keep bot credentials least-privilege.

## Source verification

The flake pins public mcp-js main commit
[`54ac96cfc54da2cce2951fca83173cef7ba54318`](https://github.com/r33drichards/mcp-js/commit/54ac96cfc54da2cce2951fca83173cef7ba54318).
Checked at that exact revision:

* `nix/options.nix`, `server/src/cli.rs`, `server/src/config.rs`: `bin/server
  --config` and bind_host/http_port/jwks_url/mcp_config/policies_json/
  allow_external_modules/allow_run_js_file/mcp_stubs keys.
* `server/src/engine/mcp_client.rs`: array entries with name, transport=`stdio`,
  command, args; `mcp.listTools` / `mcp.callTool` callable through run_js.
* `server/src/engine/opa.rs`: policy sources require `url` (not `inline`);
  local file URLs and mcp_tools + fetch policy categories are supported.
* `server/src/engine/fs.rs`: host filesystem policy gates the supplied path
  before normal filesystem access. A naive startswith check is not symlink or
  traversal confinement, so it is deliberately not used here.

Nixpkgs is pinned to NixOS 26.05 commit
`1f607aa79b6356ed45d3cd3c8079c196e79efa2c`, whose Python MCP package is 1.26.0
and standalone FastMCP package is 3.2.3 (verified from the exact revision's
`pkgs/development/python-modules/fastmcp/default.nix`).
Current unstable's MCP 2.x is deliberately not used with this package's `<2`
constraint. Native [supybot module source](https://github.com/NixOS/nixpkgs/blob/nixos-26.05/nixos/modules/services/networking/supybot.nix)
provides enable/stateDir/configFile/plugins/extraPackages and runs `supybot.service`.
The upstream start-limit burst is raised from 1 to 10 per five minutes for debug
restarts; repeated crashes can still hit systemd rate limits.

## Validation and remaining gates

Local Python broker unit tests cover peer rejection, invalid actions, exact argv,
trailing payload rejection, clean environment, and bounded output. Project tests
cover plugin/API behavior separately. All 29 tests pass locally with standalone
FastMCP 3.2.3 on both MCP 1.26.0 and 1.30.0. CI also declares Python 3.11–3.13
coverage (not run locally). The authoring environment has **no Nix**:
Nix evaluation, package build, and actual systemd runtime are **not yet verified**.
CI includes `nix flake check --no-build`, a module evaluation fixture, the
Python package build, and `checks.x86_64-linux.control-vm`. The isolated NixOS VM
runs the real broker against a harmless synthetic `supybot.service`: cross-UID
offline file repair, start/stop/restart/status, bounded unit-scoped logs, bot UID
socket denial, and invalid unit/action rejection. The real companion is disabled,
no token is provisioned, and no IRC network is used. The CI job is bounded to 60
minutes. **This VM test has been authored but not run locally.** It does not test
live mcp-js authentication, HTTP identity configuration, or a real IRC bot, and
it does not build the large upstream mcp-js Rust package or deploy. Those remain
explicitly authorized integration/activation gates after the synthetic VM passes.
Generate and commit `flake.lock` in a Nix-enabled environment;
top-level inputs are already revision-pinned, but no lockfile is fabricated here.
