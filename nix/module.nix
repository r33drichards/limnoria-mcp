{ config, lib, pkgs, ... }:
let
  cfg = config.services.limnoria-mcp;
  package = pkgs.callPackage ./package.nix { };
  pluginRoot = "/var/lib/limnoria-managed-plugins";
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
      input.tool in {"list_plugins", "read_plugin_file", "write_plugin_file", "load_plugin", "reload_plugin", "service_action", "service_logs", "service_status"}
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
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      { assertion = config.services.supybot.enable; message = "Enable services.supybot and provide its initial configFile first."; }
      { assertion = lib.hasPrefix "/" cfg.tokenFile && !(lib.hasPrefix "/nix/store/" cfg.tokenFile); message = "tokenFile must be a runtime absolute path outside /nix/store."; }
      { assertion = lib.hasPrefix "https://" cfg.jwksUrl; message = "jwksUrl must use HTTPS."; }
      { assertion = cfg.port != cfg.botPort; message = "mcp-js and bot ports must differ."; }
    ];
    services.supybot.extraPackages = p: [ (pkgs.callPackage ./package.nix { python3Packages = p; }) ];
    services.supybot.plugins.MCPBridge = ../src/MCPBridge;
    systemd.tmpfiles.rules = [ "d '${pluginRoot}' 2770 root limnoria-managed-plugins - -" ];
    systemd.services.supybot = {
      # The upstream default burst of one start / five minutes prevents normal
      # debugging restarts. Still rate-limit repeated failures.
      startLimitBurst = lib.mkForce 10;
      environment = {
        PYTHONDONTWRITEBYTECODE = "1";
        LIMNORIA_MCP_PLUGIN_ROOT = pluginRoot;
        LIMNORIA_MCP_TOKEN_FILE = "%d/limnoria-token";
        LIMNORIA_MCP_PORT = toString cfg.botPort;
      };
      serviceConfig = {
        LoadCredential = [ "limnoria-token:${cfg.tokenFile}" ];
        ReadWritePaths = [ pluginRoot ];
      };
    };
    users.groups.limnoria-mcp-control = { };
    users.groups.limnoria-managed-plugins = { };
    users.users.supybot.extraGroups = [ "limnoria-managed-plugins" ];
    users.users.mcp-js-limnoria = {
      isSystemUser = true;
      group = "limnoria-mcp-control";
      extraGroups = [ "limnoria-managed-plugins" ];
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
        LIMNORIA_MCP_PLUGIN_ROOT = pluginRoot;
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
        ReadWritePaths = [ pluginRoot ];
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
