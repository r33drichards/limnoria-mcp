# Opt-in Git main → Safe Tcl bundle updates

The trusted host flake, Python bridge, worker, service definitions, credential
provisioning and Nix configuration stay pinned and administrator-controlled.
Only `packages.<system>.scriptBundle` from the configured **public GitHub**
repository is consumed. The updater resolves `refs/heads/main` once to a full
40-character SHA, then builds exactly that revision/output. There is no user
supplied revision, output, branch, activation command, shell hook, host rebuild,
`nixosConfigurations`, or `apps` activation path.

This is a separate credential-free unprivileged service, **not Comin host
activation**. It must not be a Nix trusted user. The host must retain Nix's sandbox
and normal daemon authorization; no updater configuration grants new trust.
Nix builds necessarily execute derivation builders within the trusted host's
build sandbox; a fixed output name does not itself make a malicious derivation
safe. Evaluator/build resource exhaustion remains possible and the service has
external resource/time limits. The client requests one build job, one core and
a 600-second build timeout; stdout/stderr are drained with fixed byte budgets.
The updater service limits do not constrain the separate Nix daemon or prove
that every builder obeys the core hint. Administrator daemon/build-worker
resource controls remain necessary; killing the client is not a guarantee of
instant daemon build cancellation. Nix evaluation is never done in the bot process.

## Publishing and approval boundary

MCP Tcl creation/editing writes a separate **draft** directory. With GitOps mode
enabled, direct draft activation is disabled: edits cannot silently bypass main.
An authorized person must export/copy the intended Tcl files to `scripts/*.tcl`,
make an explicit Git commit and PR and follow the repository’s existing CI and
merge-authorization policy before merging into main. Once on main, automatic
script activation needs no additional per-update approval from the updater. This scaffold neither creates credentials nor pushes,
merges, grants access, enables a live timer, or deploys itself. Configure any required protected-main/CI policy outside this scaffold; the updater trusts the administrator's
selected main branch and does not independently authenticate GitHub check runs.

The bundle output is a flat directory of 1–64 regular UTF-8
`<script_name>.tcl` files. Names match `[A-Za-z][A-Za-z0-9_]{0,48}`; each source is
at most 64 KiB and all sources total at most 1 MiB. NULs, symlinks, subdirectories, special files, Python and activation executables
are rejected.
An empty bundle is rejected rather than implicitly deleting all active scripts.
Nix store optimization hardlinks are allowed **only** when reading an exact
`/nix/store/<hash>-name` output, reached through descriptor-pinned no-symlink
`/nix` and `store` directories owned by root. The output directory and every Tcl
file must themselves be root-owned and have no write bits. This supports standard
multi-user NixOS stores; single-user/custom stores are unsupported. Mutable
candidate and private journal files remain strictly single-link, even if their
contents originated in the store. Outside-store fixtures never receive this
exception. Hardlinks are not an execution capability: only bounded Tcl data is
read and subsequently validated in the isolated worker.

## Transaction and recovery protocol

1. A nonblocking advisory lock serializes the entire resolve/build/publish cycle.
   A concurrent invocation fails before resolving or building.
2. Git/Nix run without inherited credentials or user config, with fresh temporary
   HOME, closed stdin and deadlines. Flake config auto-accept, import-from-
   derivation and unsafe evaluator native code are explicitly disabled. Lock file
   updates are forbidden; dependencies must already be locked in Git.
3. The complete bounded output is read as data. The updater publishes one
   fsynced `candidate.json` document using atomic replacement in its private
   state directory. It contains exactly `revision` and `scripts`; it has mode
   `0640` and is readable by the bot, not writable by draft authors. JSON overhead
   has a separate bounded size. No partial file or per-script replacement is
   exposed. Failed resolve/build/validation/publication leaves the earlier
   candidate unchanged (after rename, a subsequent fsync failure is an uncertain
   durability outcome, not a rollback claim).
4. The trusted bot polls the document, stages **every** script through the isolated
   Safe Tcl worker, persists a separate bot-private last-good document, then
   atomically replaces the whole in-memory generation map. A failed script
   prevents all activation. Removed scripts disappear only at a successful whole
   bundle swap. Every generation starts with fresh state; in-flight invocations
   retain their old snapshot and cannot write new-generation state.
5. On restart the bot restores its own validated last-good document before trying
   the latest candidate. The updater cannot write this journal. A bad candidate
   therefore cannot erase the last-good deployment even across bot restarts.

`candidate-published` means **staged**, not active. Check the bot's bundle status
for the active revision and activation errors. Repeated identical revision
publication does not reset live state. A newer main commit can deliberately
restore previous source content; manual mutable-pointer rollbacks are not an
exposed authoring capability. Validation uses a synthetic context and cannot
prove every future context-dependent path; invocation failures still fail closed.

The separate opt-in Nix module options are documented in
[nixos-integration.md](nixos-integration.md). Default configuration leaves the
updater disabled. Activation requires administrator configuration and deployment
approval; tests use local fixtures and do not contact or mutate remote main.

## Verification

`pytest -q tests/test_bundle_update.py` covers strict document/output validation,
link/special-file rejection, size limits, exact-main SHA/fixed-output argv,
ambient credential/config removal, overlap exclusion, and failed build/replace
preservation. Runtime watcher tests cover staging, acknowledgement and last-good
restore. Nix evaluation/build and VM tests remain separate gates: Python fixtures
do not claim a successful real Nix build or deployed service.

Nix command/configuration references:
[build and lock flags](https://nix.dev/manual/nix/2.28/command-ref/new-cli/nix3-build.html),
[configuration precedence and security settings](https://nix.dev/manual/nix/2.28/command-ref/conf-file.html).
