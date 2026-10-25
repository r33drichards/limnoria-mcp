{
  description = "Limnoria MCPBridge and isolated mcp-js service control";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/1f607aa79b6356ed45d3cd3c8079c196e79efa2c";
  # Verified source revision; update deliberately and review policy/CLI changes.
  inputs.mcp-js.url = "github:r33drichards/mcp-js/54ac96cfc54da2cce2951fca83173cef7ba54318";
  outputs = { self, nixpkgs, mcp-js }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      eachSystem = nixpkgs.lib.genAttrs systems;
    in {
      packages = eachSystem (system: let pkgs = import nixpkgs { inherit system; }; in {
        default = pkgs.callPackage ./nix/package.nix { };
        # Only inert Tcl source files; no executable activation output.
        scriptBundle = pkgs.runCommand "limnoria-tcl-script-bundle" { } ''
          mkdir -p "$out"
          cp ${./scripts}/*.tcl "$out/"
        '';
      });
      checks = eachSystem (system: let
        pkgs = import nixpkgs { inherit system; };
        evaluated = nixpkgs.lib.nixosSystem {
          inherit system;
          modules = [ self.nixosModules.default {
            system.stateVersion = "25.05";
            boot.loader.grub.enable = false;
            fileSystems."/" = { device = "/dev/test-root"; fsType = "ext4"; };
            services.supybot.enable = true;
            services.supybot.configFile = pkgs.writeText "supybot-test.cfg" "";
            services.limnoria-mcp = {
              enable = true;
              tokenFile = "/run/operator-provisioned-token";
              jwksUrl = "https://auth.example.invalid/.well-known/jwks.json";
            };
          } ];
        };
        c = evaluated.config;
        wildcardTrusted = evaluated.extendModules { modules = [{
          services.limnoria-mcp.gitOps.enable = true;
          nix.settings.trusted-users = nixpkgs.lib.mkForce [ "*" ];
        }]; };
        overriddenGroupTrusted = evaluated.extendModules { modules = [{
          services.limnoria-mcp.gitOps.enable = true;
          users.users.limnoria-bundle-updater.group = nixpkgs.lib.mkForce "wheel";
          nix.settings.trusted-users = nixpkgs.lib.mkForce [ "@wheel" ];
        }]; };
        groupTrusted = evaluated.extendModules { modules = [{
          services.limnoria-mcp.gitOps.enable = true;
          nix.settings.trusted-users = nixpkgs.lib.mkForce [ "@limnoria-script-readers" ];
        }]; };
      in {
        control-vm = pkgs.callPackage ./nix/test-vm.nix { };
        module-eval = assert builtins.all (a: a.assertion) c.assertions;
          assert !(builtins.all (a: a.assertion) wildcardTrusted.config.assertions);
          assert !(builtins.all (a: a.assertion) groupTrusted.config.assertions);
          assert !(builtins.all (a: a.assertion) overriddenGroupTrusted.config.assertions);
          assert c.systemd.services.mcp-js-limnoria.serviceConfig.User != "supybot";
          assert c.systemd.services.mcp-js-limnoria.serviceConfig.UMask == "0007";
          assert c.systemd.services.supybot.serviceConfig.UMask == "0007";
          assert !(builtins.elem "supybot.service" c.systemd.services.mcp-js-limnoria.requires);
          assert !(builtins.elem "limnoria-mcp-control" c.users.users.supybot.extraGroups);
          assert !(builtins.elem "supybot" c.users.users.mcp-js-limnoria.extraGroups);
          assert c.users.users.limnoria-tcl-worker.extraGroups == [ ];
          assert !(builtins.elem "limnoria-tcl-client" c.users.users.mcp-js-limnoria.extraGroups);
          assert c.systemd.services.limnoria-tcl-worker.serviceConfig.PrivateNetwork;
          assert c.systemd.sockets.limnoria-tcl-worker.socketConfig.SocketGroup == "limnoria-tcl-client";
          assert builtins.elem "/var/lib/limnoria-managed-scripts" c.systemd.services.mcp-js-limnoria.serviceConfig.ReadWritePaths;
          assert c.systemd.services.mcp-js-limnoria.environment.LIMNORIA_MCP_SCRIPT_ROOT == "/var/lib/limnoria-managed-scripts";
          pkgs.writeText "limnoria-module-evaluated" (builtins.unsafeDiscardStringContext (builtins.toJSON {
            bot = c.systemd.services.supybot.serviceConfig.ExecStart;
            companion = c.systemd.services.mcp-js-limnoria.serviceConfig.ExecStart;
            broker = c.systemd.services.limnoria-mcp-control.serviceConfig.ExecStart;
            worker = c.systemd.services.limnoria-tcl-worker.serviceConfig.ExecStart;
          }));
      });
      nixosModules.default = { pkgs, ... }: {
        imports = [ ./nix/module.nix ];
        services.limnoria-mcp.mcpJsPackage = nixpkgs.lib.mkDefault mcp-js.packages.${pkgs.stdenv.hostPlatform.system}.default;
      };
    };
}
