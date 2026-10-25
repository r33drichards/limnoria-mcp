# Isolated runtime test: synthetic bot only, no real IRC, credentials or JWKS.
{ pkgs }:
let
  lib = pkgs.lib;
  package = pkgs.callPackage ./package.nix { };
  python = pkgs.python3.withPackages (_: [ package ]);
  fileProbe = pkgs.writeText "managed-files-probe.py" ''
    import sys
    from limnoria_mcp.files import PluginFiles
    files = PluginFiles('/var/lib/limnoria-managed-plugins')
    if len(sys.argv) == 2:
        files.write('AgentFixture', 'plugin.py', 'value = ' + repr(sys.argv[1]) + '\n')
    print(files.read('AgentFixture', 'plugin.py'))
    files.close()
  '';
  invalidProbe = pkgs.writeText "invalid-control-probe.py" ''
    import json, socket
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect('/run/limnoria-mcp-control/control.sock')
        connection.sendall(b'restart sshd.service\n')
        response = json.loads(connection.recv(4096))
        assert response == {'error': 'unsupported action'}, response
  '';
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
    };
    systemd.services.mcp-js-limnoria.enable = false;
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
    environment.systemPackages = [ python pkgs.util-linux ];
  };
  testScript = ''
    import json
    start_all()
    machine.wait_for_unit("multi-user.target")
    machine.wait_for_unit("supybot.service")
    machine.wait_until_succeeds("test -S /run/limnoria-mcp-control/control.sock")
    client = "runuser -u mcp-js-limnoria -- ${pkgs.python3}/bin/python3 -I ${./control_client.py} "
    def action(name):
        result = json.loads(machine.succeed(client + name))
        assert result["returncode"] == 0, result
        return result
    assert "ActiveState=active" in action("status")["stdout"]
    action("stop")
    machine.wait_until_fails("systemctl is-active --quiet supybot.service")

    # Same confined API, real separate UIDs, while the bot process is absent.
    probe = "${python}/bin/python3 ${fileProbe}"
    machine.succeed("runuser -u mcp-js-limnoria -- " + probe + " controller-edit")
    assert "controller-edit" in machine.succeed("runuser -u supybot -- " + probe)
    machine.succeed("runuser -u supybot -- " + probe + " bot-edit")
    assert "bot-edit" in machine.succeed("runuser -u mcp-js-limnoria -- " + probe)
    machine.fail("runuser -u mcp-js-limnoria -- ls /var/lib/supybot")
    machine.fail("runuser -u supybot -- ${pkgs.python3}/bin/python3 -I ${./control_client.py} restart")
    machine.fail(client + "daemon-reload")
    machine.fail(client + "restart sshd.service")
    machine.succeed("runuser -u mcp-js-limnoria -- ${pkgs.python3}/bin/python3 -I ${invalidProbe}")
    assert "ActiveState=inactive" in action("status")["stdout"]

    action("start")
    machine.wait_for_unit("supybot.service")
    before = machine.succeed("systemctl show --property=MainPID --value supybot.service").strip()
    action("restart")
    machine.wait_for_unit("supybot.service")
    after = machine.succeed("systemctl show --property=MainPID --value supybot.service").strip()
    assert before != after
    machine.succeed("journalctl --sync")
    logs = action("logs")["stdout"]
    assert "BOT-ONLY-FIXTURE-MARKER" in logs
    assert "UNRELATED-FIXTURE-MARKER" not in logs
    assert len(logs.encode()) <= 16384
    machine.succeed("systemctl is-active --quiet limnoria-mcp-control.service")
  '';
}
