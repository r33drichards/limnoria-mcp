{ config, lib, pkgs, ... }:
let
  cfg = config.services.limnoria-mcp;
  package = pkgs.callPackage ./package.nix { };
  updaterName = "limnoria-bundle-updater";
  updaterGroups = [ config.users.users.limnoria-bundle-updater.group ]
    ++ config.users.users.limnoria-bundle-updater.extraGroups
    ++ lib.attrNames (lib.filterAttrs (_: group: builtins.elem updaterName group.members) config.users.groups);
  updaterIsTrusted = lib.any (entry:
    entry == "*" || entry == updaterName ||
    (lib.hasPrefix "@" entry && builtins.elem (lib.removePrefix "@" entry) updaterGroups)
  ) (config.nix.settings.trusted-users or [ ]);
  workerPython = pkgs.python3.withPackages (_: [ package ]);
  scriptRoot = "/var/lib/limnoria-managed-scripts";
  brokerSource = pkgs.replaceVars ./control_broker.py {
    systemctl = "${pkgs.systemd}/bin/systemctl";
    journalctl = "${pkgs.systemd}/bin/journalctl";
  };
  helper = pkgs.writeShellScript "limnoria-service-control" ''
    exec ${pkgs.python3}/bin/python3 -I ${./control_client.py} "$@"
  '';
  control = pkgs.writeShellScript "limnoria-mcp-upstream" ''
    exec ${package}/bin/limnoria-mcp-control
  '';
  policy = pkgs.writeText "limnoria-mcp-tools.rego" ''
    package mcp.tools
    default allow := false
    allow if {
      input.server == "limnoria"
      input.tool in {"list_scripts", "read_script", "write_script", "validate_script", "activate_script", "script_status", "service_action", "service_logs", "service_status"}
    }
  '';
  denyFetch = pkgs.writeText "limnoria-deny-fetch.rego" "package mcp.fetch\ndefault allow := false\n";
  policies = pkgs.writeText "limnoria-mcp-policies.json" (builtins.toJSON {
    mcp_tools.policies = [{ url = "file://${policy}"; }];
    # Deny outbound fetch explicitly; do not enable filesystem, subprocess,
    # external modules, or run_js_file. File operations use the bounded controller.
    fetch.policies = [{ url = "file://${denyFetch}"; }];
  });
  upstream = pkgs.writeText "limnoria-mcp-upstream.json" (builtins.toJSON [{
    name = "limnoria";
    transport = "stdio";
    command = "${control}";
    args = [ ];
  }]);
  serverConfig = (pkgs.formats.toml { }).generate "limnoria-mcp-js.toml" {
    bind_host = "127.0.0.1";
    http_port = cfg.port;
    jwks_url = cfg.jwksUrl;
    mcp_config = "${upstream}";
    policies_json = "${policies}";
    allow_external_modules = false;
    allow_run_js_file = false;
    mcp_stubs = true;
  };
  hardening = {
    NoNewPrivileges = true;
    PrivateTmp = true;
    PrivateDevices = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectControlGroups = true;
    RestrictSUIDSGID = true;
    LockPersonality = true;
    UMask = "0077";
  };
in {
  options.services.limnoria-mcp = {
    enable = lib.mkEnableOption "Limnoria MCPBridge with a separate mcp-js controller";
    mcpJsPackage = lib.mkOption { type = lib.types.package; description = "Reviewed mcp-js package providing bin/server."; };
    tokenFile = lib.mkOption {
      type = lib.types.str;
      description = "Absolute runtime path to an operator-provisioned bearer token; never put its contents in Nix/store. Shared through systemd credentials.";
    };
    jwksUrl = lib.mkOption {
      type = lib.types.str;
      description = "Operator-approved JWKS endpoint required to authenticate clients to the mcp-js HTTP endpoint.";
    };
    port = lib.mkOption { type = lib.types.port; default = 8766; };
    botPort = lib.mkOption { type = lib.types.port; default = 8765; };
    gitOps = {
      enable = lib.mkEnableOption "main-only Tcl bundle updates (direct activation disabled)";
      repository = lib.mkOption {
        type = lib.types.str;
        default = "https://github.com/r33drichards/limnoria-mcp";
        description = "Approved public GitHub repository; updater resolves main to an exact commit and builds only scriptBundle.";
      };
      interval = lib.mkOption { type = lib.types.str; default = "5min"; };
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      { assertion = config.services.supybot.enable; message = "Enable services.supybot and provide its initial configFile first."; }
      { assertion = lib.hasPrefix "/" cfg.tokenFile && !(lib.hasPrefix "/nix/store/" cfg.tokenFile); message = "tokenFile must be a runtime absolute path outside /nix/store."; }
      { assertion = lib.hasPrefix "https://" cfg.jwksUrl; message = "jwksUrl must use HTTPS."; }
      { assertion = !cfg.gitOps.enable || config.nix.enable; message = "GitOps bundle builds require the Nix daemon."; }
      { assertion = !cfg.gitOps.enable || (config.nix.settings.sandbox or true) == true; message = "GitOps bundle builds require the Nix sandbox; do not trust the updater user."; }
      { assertion = !cfg.gitOps.enable || !updaterIsTrusted; message = "The bundle updater must not be a trusted Nix user."; }
      { assertion = !cfg.gitOps.enable || builtins.match "https://github.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+" cfg.gitOps.repository != null; message = "gitOps.repository must be a credential-free public GitHub repository URL."; }
      { assertion = cfg.port != cfg.botPort; message = "mcp-js and bot ports must differ."; }
    ];
    services.supybot.extraPackages = p: [ (pkgs.callPackage ./package.nix { python3Packages = p; }) ];
    services.supybot.plugins.MCPBridge = ../src/MCPBridge;
    systemd.tmpfiles.rules = [ "d '${scriptRoot}' 2770 root limnoria-managed-scripts - -" ];
    systemd.services.supybot = {
      after = [ "limnoria-tcl-worker.socket" ];
      requires = [ "limnoria-tcl-worker.socket" ];
      # The upstream default burst of one start / five minutes prevents normal
      # debugging restarts. Still rate-limit repeated failures.
      startLimitBurst = lib.mkForce 10;
      environment = {
        PYTHONDONTWRITEBYTECODE = "1";
        LIMNORIA_MCP_SCRIPT_ROOT = scriptRoot;
        LIMNORIA_MCP_TOKEN_FILE = "%d/limnoria-token";
        LIMNORIA_MCP_PORT = toString cfg.botPort;
        LIMNORIA_TCL_WORKER_SOCKET = "/run/limnoria-tcl-worker/worker.sock";
      } // lib.optionalAttrs cfg.gitOps.enable {
        LIMNORIA_MCP_BUNDLE_DIR = "/var/lib/limnoria-script-bundles";
        LIMNORIA_MCP_LAST_GOOD_FILE = "${config.services.supybot.stateDir}/tcl-last-good.json";
      };
      serviceConfig = {
        # Also set explicitly here: shared directories depend on group-write.
        UMask = lib.mkForce "0007";
        LoadCredential = [ "limnoria-token:${cfg.tokenFile}" ];
        ReadWritePaths = [ scriptRoot ];
        RestrictAddressFamilies = lib.mkAfter [ "AF_UNIX" ];
      };
    };
    users.groups.limnoria-mcp-control = { };
    users.groups.limnoria-managed-scripts = { };
    users.groups.limnoria-tcl-client = { };
    users.groups.limnoria-tcl-worker = { };
    users.users.limnoria-tcl-worker = {
      isSystemUser = true;
      group = "limnoria-tcl-worker";
    };
    users.users.supybot.extraGroups = [ "limnoria-managed-scripts" "limnoria-tcl-client" ]
      ++ lib.optional cfg.gitOps.enable "limnoria-script-readers";
    users.users.mcp-js-limnoria = {
      isSystemUser = true;
      group = "limnoria-mcp-control";
      extraGroups = [ "limnoria-managed-scripts" ];
    };
    systemd.sockets.limnoria-tcl-worker = {
      description = "Bot-only Safe Tcl worker socket";
      wantedBy = [ "sockets.target" ];
      socketConfig = {
        ListenStream = "/run/limnoria-tcl-worker/worker.sock";
        SocketUser = "root";
        SocketGroup = "limnoria-tcl-client";
        SocketMode = "0660";
        DirectoryMode = "0755";
        RemoveOnStop = true;
      };
    };
    systemd.services.limnoria-tcl-worker = {
      description = "Credential-free unprivileged Safe Tcl worker";
      requires = [ "limnoria-tcl-worker.socket" ];
      after = [ "limnoria-tcl-worker.socket" ];
      environment.PYTHONDONTWRITEBYTECODE = "1";
      serviceConfig = hardening // {
        ExecStart = "${workerPython}/bin/python3 ${./tcl_broker.py}";
        User = "limnoria-tcl-worker";
        Group = "limnoria-tcl-worker";
        WorkingDirectory = "/";
        PrivateNetwork = true;
        RestrictAddressFamilies = [ "AF_UNIX" ];
        RestrictNamespaces = true;
        CapabilityBoundingSet = "";
        MemoryDenyWriteExecute = true;
        MemoryMax = "256M";
        TasksMax = 8;
        CPUQuota = "50%";
        InaccessiblePaths = [
          "-/run/credentials" "-${cfg.tokenFile}"
          "-${toString config.services.supybot.configFile}" "-${config.services.supybot.stateDir}"
          "-/var/lib/mcp-js-limnoria" "-${scriptRoot}"
          "-/run/limnoria-mcp-control" "-/var/lib/limnoria-script-bundles"
        ];
        Restart = "on-failure";
        RestartSec = 1;
      };
    };
    users.groups.limnoria-script-readers = lib.mkIf cfg.gitOps.enable { };
    users.users.limnoria-bundle-updater = lib.mkIf cfg.gitOps.enable {
      isSystemUser = true;
      group = "limnoria-script-readers";
    };
    systemd.services.limnoria-bundle-updater = lib.mkIf cfg.gitOps.enable {
      description = "Build main-pinned Tcl bundle data (never activate host services)";
      after = [ "network-online.target" "nix-daemon.socket" ];
      wants = [ "network-online.target" ];
      path = [ pkgs.git pkgs.nix ];
      environment.HOME = "/var/lib/limnoria-script-bundles";
      serviceConfig = hardening // {
        Type = "oneshot";
        ExecStart = "${workerPython}/bin/python3 -m limnoria_mcp.bundle_update --repository ${cfg.gitOps.repository} --state-directory /var/lib/limnoria-script-bundles --system ${pkgs.stdenv.hostPlatform.system}";
        User = "limnoria-bundle-updater";
        Group = "limnoria-script-readers";
        StateDirectory = "limnoria-script-bundles";
        StateDirectoryMode = "0750";
        UMask = lib.mkForce "0027";
        WorkingDirectory = "/var/lib/limnoria-script-bundles";
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
        CapabilityBoundingSet = "";
        TimeoutStartSec = "15min";
        LimitFSIZE = "64M";
        MemoryMax = "1G";
        TasksMax = 64;
        InaccessiblePaths = [
          "-/run/credentials" "-${cfg.tokenFile}" "-${config.services.supybot.stateDir}"
          "-${toString config.services.supybot.configFile}" "-${scriptRoot}"
          "-/var/lib/mcp-js-limnoria" "-/run/limnoria-mcp-control" "-/run/limnoria-tcl-worker"
        ];
      };
    };
    systemd.timers.limnoria-bundle-updater = lib.mkIf cfg.gitOps.enable {
      wantedBy = [ "timers.target" ];
      timerConfig = { OnBootSec = "2min"; OnUnitActiveSec = cfg.gitOps.interval; };
    };
    # No sudoers or polkit grant, no journal group, no shell exposed to callers.
    systemd.services.limnoria-mcp-control = {
      description = "Fixed single-unit Limnoria service broker";
      wantedBy = [ "multi-user.target" ];
      serviceConfig = hardening // {
        ExecStart = "${pkgs.python3}/bin/python3 -I ${brokerSource}";
        User = "root";
        Group = "limnoria-mcp-control";
        RuntimeDirectory = "limnoria-mcp-control";
        RuntimeDirectoryMode = "0750";
        UMask = lib.mkForce "0007";
        RestrictAddressFamilies = [ "AF_UNIX" ];
        CapabilityBoundingSet = "";
        Restart = "on-failure";
      };
    };
    systemd.services.mcp-js-limnoria = {
      description = "Authenticated mcp-js companion for Limnoria";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" "limnoria-mcp-control.service" ];
      requires = [ "limnoria-mcp-control.service" ];
      # Intentionally no Requires/BindsTo supybot: recovery must survive its exit.
      environment = {
        LIMNORIA_MCP_CONTROL_HELPER = "${helper}";
        LIMNORIA_MCP_SCRIPT_ROOT = scriptRoot;
        PYTHONDONTWRITEBYTECODE = "1";
        LIMNORIA_MCP_TOKEN_FILE = "%d/limnoria-token";
        LIMNORIA_MCP_PORT = toString cfg.botPort;
      };
      serviceConfig = hardening // {
        ExecStart = "${cfg.mcpJsPackage}/bin/server --config ${serverConfig}";
        User = "mcp-js-limnoria";
        Group = "limnoria-mcp-control";
        LoadCredential = [ "limnoria-token:${cfg.tokenFile}" ];
        UMask = lib.mkForce "0007";
        ReadWritePaths = [ scriptRoot ];
        StateDirectory = "mcp-js-limnoria";
        WorkingDirectory = "/var/lib/mcp-js-limnoria";
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
        CapabilityBoundingSet = "";
        Restart = "on-failure";
        RestartSec = 2;
      };
    };
  };
}
