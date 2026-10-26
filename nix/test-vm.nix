# Isolated runtime test: synthetic bot only, no real IRC, credentials or JWKS.
{ pkgs }:
let
  lib = pkgs.lib;
  package = pkgs.callPackage ./package.nix { };
  python = pkgs.python3.withPackages (_: [ package ]);
  fileProbe = ./vm/file_probe.py;
  publishFixture = ./vm/publish_fixture.py;
  bundleProbe = ./vm/bundle_probe.py;
  sandboxProbe = ./vm/sandbox_probe.py;
  workerProbe = ./vm/worker_probe.py;
  invalidProbe = ./vm/invalid_probe.py;
  publicHttpProbe = ./vm/public_http_probe.py;
  httpControllerProbe = ./vm/http_controller_probe.py;
in pkgs.testers.runNixOSTest {
  name = "limnoria-control-boundaries";
  nodes.machine = { config, ... }: {
    imports = [ ./module.nix ];
    services.supybot = {
      enable = true;
      configFile = pkgs.writeText "synthetic-supybot.cfg" "";
    };
    services.limnoria-mcp = {
      enable = true;
      mcpJsPackage = pkgs.writeShellScriptBin "server" "exit 1";
      tokenFile = "/run/not-provisioned";
      jwksUrl = "https://unused.example.invalid/jwks";
      gitOps.enable = true;
    };
    # Provision the identities/layout but never fetch/build from a network here.
    systemd.services.limnoria-bundle-updater.enable = false;
    systemd.timers.limnoria-bundle-updater.enable = false;
    systemd.tmpfiles.rules = [ "d /var/lib/limnoria-script-bundles 0750 limnoria-bundle-updater limnoria-script-readers - -" ];
    systemd.services.mcp-js-limnoria.enable = false;
    # Exercise controller recovery without provisioning any bot bearer credential.
    # Proxy-only tools are not called against the synthetic bot.
    systemd.services.limnoria-mcp-controller.serviceConfig.LoadCredential = lib.mkForce [ ];
    systemd.services.limnoria-http-broker.serviceConfig.ExecStartPre =
      "${python}/bin/python3 ${./vm/http_sandbox_probe.py}";
    systemd.services.limnoria-tcl-worker.serviceConfig.ExecStartPre =
      "${pkgs.python3}/bin/python3 -I ${sandboxProbe}";
    systemd.services.supybot.serviceConfig = {
      LoadCredential = lib.mkForce [ ];
      ExecStart = lib.mkForce (toString (pkgs.writeShellScript "synthetic-bot" ''
        echo BOT-ONLY-FIXTURE-MARKER
        exec ${pkgs.coreutils}/bin/sleep infinity
      ''));
    };
    systemd.services.unrelated-fixture = {
      wantedBy = [ "multi-user.target" ];
      script = "echo UNRELATED-FIXTURE-MARKER";
      serviceConfig.Type = "oneshot";
    };
    environment.systemPackages = [ python pkgs.util-linux pkgs.iproute2 ];
  };
  # Python stays in ordinary .py files: Python quotes and ${...} text must not
  # accidentally terminate/interpolate Nix strings. Only these fixed store paths
  # are substituted; no request-controlled values enter the driver program.
  testScript = builtins.replaceStrings
    [ "@plainPython@" "@packagePython@" "@controlClient@" "@fileProbe@"
      "@publishFixture@" "@bundleProbe@" "@workerProbe@" "@invalidProbe@" "@httpControllerProbe@" "@publicHttpProbe@" ]
    [ "${pkgs.python3}/bin/python3" "${python}/bin/python3" "${./control_client.py}"
      "${fileProbe}" "${publishFixture}" "${bundleProbe}" "${workerProbe}" "${invalidProbe}" "${httpControllerProbe}" "${publicHttpProbe}" ]
    (builtins.readFile ./vm/driver.py);
}
