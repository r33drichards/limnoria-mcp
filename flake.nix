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
        mcp-js = mcp-js.packages.${system}.default;
        http-test-python = pkgs.python3.withPackages (_: [
          (pkgs.callPackage ./nix/package.nix { })
        ]);
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
        mcp-js-cli = pkgs.runCommand "limnoria-pinned-mcp-js-cli-smoke" {
          nativeBuildInputs = [ pkgs.gnugrep ];
        } ''
          # Real pinned companion binary, not the synthetic VM stub. No listener,
          # upstream, issuer request, credential, or IRC connection is started.
          server=${mcp-js.packages.${system}.default}/bin/server
          ${pkgs.coreutils}/bin/timeout 10 "$server" --help > help.txt
          for flag in --config --bind-host --http-port --jwks-url --mcp-config --policies-json; do
            grep -F -- "$flag" help.txt
          done
          printf 'invalid_limnoria_smoke_key = true\n' > invalid.toml
          if ${pkgs.coreutils}/bin/timeout 10 "$server" --config "$PWD/invalid.toml" > rejected.txt 2>&1; then
            echo "server accepted an unknown config key" >&2
            exit 1
          fi
          grep -F 'unknown key: invalid_limnoria_smoke_key' rejected.txt
          mkdir -p "$out"
          cp help.txt rejected.txt "$out/"
        '';
        control-vm = pkgs.callPackage ./nix/test-vm.nix { };
        module-eval = assert builtins.all (a: a.assertion) c.assertions;
          assert !(builtins.all (a: a.assertion) wildcardTrusted.config.assertions);
          assert !(builtins.all (a: a.assertion) groupTrusted.config.assertions);
          assert !(builtins.all (a: a.assertion) overriddenGroupTrusted.config.assertions);
          assert c.systemd.services.mcp-js-limnoria.serviceConfig.User != "supybot";
          assert c.systemd.services.limnoria-mcp-controller.serviceConfig.UMask == "0007";
          assert c.systemd.services.supybot.serviceConfig.UMask == "0007";
          assert c.systemd.services.supybot.serviceConfig.StateDirectoryMode == "0700";
          assert !(builtins.elem "supybot.service" c.systemd.services.mcp-js-limnoria.requires);
          assert !(builtins.elem "limnoria-mcp-control" c.users.users.supybot.extraGroups);
          assert !(builtins.elem "supybot" c.users.users.limnoria-mcp-controller.extraGroups);
          assert c.users.users.mcp-js-limnoria.extraGroups == [ ];
          assert c.users.users.mcp-js-limnoria.group != "limnoria-mcp-control";
          assert c.users.users.limnoria-tcl-worker.extraGroups == [ "limnoria-http-client" ];
          assert c.users.users.limnoria-http.extraGroups == [ ];
          assert c.systemd.services.limnoria-http-broker.serviceConfig.User == "limnoria-http";
          assert c.systemd.services.limnoria-tcl-worker.serviceConfig.RestrictAddressFamilies == [ "AF_UNIX" ];
          assert !(builtins.elem "limnoria-tcl-client" c.users.users.mcp-js-limnoria.extraGroups);
          assert c.systemd.services.limnoria-tcl-worker.serviceConfig.PrivateNetwork;
          assert c.systemd.sockets.limnoria-tcl-worker.socketConfig.SocketGroup == "limnoria-tcl-client";
          assert builtins.elem "/var/lib/limnoria-managed-scripts" c.systemd.services.limnoria-mcp-controller.serviceConfig.ReadWritePaths;
          assert c.systemd.services.limnoria-mcp-controller.environment.LIMNORIA_MCP_SCRIPT_ROOT == "/var/lib/limnoria-managed-scripts";
          pkgs.writeText "limnoria-module-evaluated" (builtins.unsafeDiscardStringContext (builtins.toJSON {
            bot = c.systemd.services.supybot.serviceConfig.ExecStart;
            controller = c.systemd.services.limnoria-mcp-controller.serviceConfig.ExecStart;
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
