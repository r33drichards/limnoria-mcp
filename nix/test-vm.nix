# Isolated runtime test: synthetic bot only, no real IRC, credentials or JWKS.
{ pkgs }:
let
  lib = pkgs.lib;
  package = pkgs.callPackage ./package.nix { };
  python = pkgs.python3.withPackages (_: [ package ]);
  fileProbe = pkgs.writeText "managed-files-probe.py" ''
    import sys
    from limnoria_mcp.files import ScriptFiles
    files = ScriptFiles('/var/lib/limnoria-managed-scripts')
    name = sys.argv[2] if len(sys.argv) == 3 else 'AgentFixture'
    if len(sys.argv) >= 2:
        files.write(name, 'reply {' + sys.argv[1] + '}\n')
    print(files.read(name))
    files.close()
  '';
  publishFixture = pkgs.writeText "publish-fixture-bundle.py" ''
    import sys
    from limnoria_mcp.bundle_update import publish_candidate
    mode = sys.argv[1]
    revision = {'a':'a', 'b':'b', 'bad':'c'}[mode] * 40
    source = 'exec id' if mode == 'bad' else 'reply revision-' + mode
    publish_candidate('/var/lib/limnoria-script-bundles',
        'https://github.com/example/fixture', 'x86_64-linux',
        build=lambda repository, system, home: (revision, {'Greeting': source}))
  '';
  bundleProbe = pkgs.writeText "bundle-activation-probe.py" ''
    import pathlib, sys
    from limnoria_mcp.files import ScriptFiles
    from limnoria_mcp.runtime import ScriptRuntime
    from limnoria_mcp.bundle_runtime import BundleWatcher
    files = ScriptFiles('/var/lib/limnoria-managed-scripts')
    runtime = ScriptRuntime(files, gitops=True)
    last_good = '/var/lib/supybot/tcl-last-good.json'
    watcher = BundleWatcher(runtime, '/var/lib/limnoria-script-bundles', last_good)
    watcher.restore()
    old = pathlib.Path(last_good).read_bytes() if pathlib.Path(last_good).exists() else None
    old_status = runtime.status()
    try:
        watcher.check()
    except ValueError:
        assert sys.argv[1] == 'reject'
        assert pathlib.Path(last_good).read_bytes() == old
        assert runtime.status() == old_status
    else:
        assert sys.argv[1] != 'reject'
    expected = sys.argv[2]
    assert runtime.bundle_status()['revision'] == expected * 40
    reply = runtime.invoke('Greeting', {'network':'offline', 'channel':'#fixture',
                                      'nick':'tester', 'text':''})
    assert reply['replies'] == ['revision-' + expected]
    try:
        runtime.activate('Greeting')
    except ValueError:
        pass
    else:
        raise AssertionError('draft activation bypassed main')
    files.close()
  '';
  sandboxProbe = pkgs.writeText "worker-sandbox-probe.py" ''
    import socket
    for path in ['/run/not-provisioned', '/var/lib/supybot/state-marker',
                 '/var/lib/limnoria-managed-scripts/source-marker']:
        try:
            with open(path, 'rb') as stream:
                stream.read(1)
        except (PermissionError, FileNotFoundError):
            pass
        else:
            raise AssertionError('worker can read a protected host path')
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        pass
    else:
        sock.close()
        raise AssertionError('worker can create an Internet socket')
    print('WORKER-SANDBOX-PROBE-PASSED')
  '';
  workerProbe = pkgs.writeText "safe-tcl-worker-probe.py" ''
    import json, socket, sys
    request = {'script': sys.argv[1], 'state': {}, 'context':
               {'network': 'offline', 'channel': '#fixture', 'nick': 'tester', 'text': 'hello'}}
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(10)
        conn.connect('/run/limnoria-tcl-worker/worker.sock')
        conn.sendall(json.dumps(request).encode() + b'\n')
        response = bytearray()
        while len(response) <= 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
    assert len(response) <= 65536
    print(json.dumps(json.loads(response)))
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
      gitOps.enable = true;
    };
    # Provision the identities/layout but never fetch/build from a network here.
    systemd.services.limnoria-bundle-updater.enable = false;
    systemd.timers.limnoria-bundle-updater.enable = false;
    systemd.tmpfiles.rules = [ "d /var/lib/limnoria-script-bundles 0750 limnoria-bundle-updater limnoria-script-readers - -" ];
    systemd.services.mcp-js-limnoria.enable = false;
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
    def write_as(user, text, plugin="AgentFixture"):
        # Exercise actual systemd UMask and SUID/SGID syscall filtering, not only
        # Unix identities. SGID must be inherited, never explicitly requested.
        result = machine.succeed("systemd-run --quiet --wait --pipe --collect -p User=" + user
                        + " -p UMask=0007 -p RestrictSUIDSGID=yes -p NoNewPrivileges=yes -- "
                        + probe + " " + text + " " + plugin)
        assert text in result
    write_as("mcp-js-limnoria", "controller-edit")
    assert machine.succeed("stat -c '%a %G' /var/lib/limnoria-managed-scripts/AgentFixture").strip() == "2770 limnoria-managed-scripts"
    assert machine.succeed("stat -c '%a %G' /var/lib/limnoria-managed-scripts/AgentFixture/script.tcl").strip() == "660 limnoria-managed-scripts"
    assert "controller-edit" in machine.succeed("runuser -u supybot -- " + probe)
    write_as("supybot", "bot-edit")
    assert "bot-edit" in machine.succeed("runuser -u mcp-js-limnoria -- " + probe)
    # The reverse ownership direction must create a fresh directory, not only
    # replace a file inside one that the controller already created.
    write_as("supybot", "bot-created", "AgentBotCreated")
    assert machine.succeed("stat -c '%a %U %G' /var/lib/limnoria-managed-scripts/AgentBotCreated").strip() == "2770 supybot limnoria-managed-scripts"
    write_as("mcp-js-limnoria", "controller-repaired", "AgentBotCreated")
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

    # Actual socket activation and isolated -I tkinter import; no bot token or IRC.
    machine.succeed("printf sentinel > /run/not-provisioned; chmod 0644 /run/not-provisioned")
    machine.succeed("touch /var/lib/supybot/state-marker /var/lib/limnoria-managed-scripts/source-marker; chmod 0644 /var/lib/supybot/state-marker /var/lib/limnoria-managed-scripts/source-marker")
    worker = "runuser -u supybot -- ${pkgs.python3}/bin/python3 -I ${workerProbe} "
    result = json.loads(machine.succeed(worker + "'reply hello'"))
    assert result == {"replies": ["hello"], "state": {}}
    machine.wait_for_unit("limnoria-tcl-worker.service")
    for script in ["exec id", "open /etc/passwd", "socket localhost 80", "while {1} {}"]:
        import shlex
        result = json.loads(machine.succeed(worker + shlex.quote(script)))
        assert "error" in result
    # Failed/limited script cannot poison the next fresh invocation.
    assert json.loads(machine.succeed(worker + "'reply recovered'"))["replies"] == ["recovered"]
    machine.fail("runuser -u mcp-js-limnoria -- ${pkgs.python3}/bin/python3 -I ${workerProbe} 'reply forbidden'")
    machine.fail("runuser -u limnoria-tcl-worker -- ls /var/lib/limnoria-managed-scripts")
    machine.fail("runuser -u limnoria-tcl-worker -- ls /var/lib/supybot")
    machine.fail("runuser -u limnoria-tcl-worker -- ${pkgs.python3}/bin/python3 -I ${./control_client.py} status")
    assert machine.succeed("systemctl show -p User --value limnoria-tcl-worker.service").strip() == "limnoria-tcl-worker"
    assert machine.succeed("systemctl show -p PrivateNetwork --value limnoria-tcl-worker.service").strip() == "yes"

    # Synthetic candidate publication under the updater UID; no Nix/network call.
    publish = "runuser -u limnoria-bundle-updater -- ${python}/bin/python3 ${publishFixture} "
    activate = "runuser -u supybot -- ${python}/bin/python3 ${bundleProbe} "
    bot_pid = machine.succeed("systemctl show --property=MainPID --value supybot.service")
    machine.succeed(publish + "a")
    machine.succeed(activate + "accept a")
    machine.succeed(publish + "bad")
    machine.succeed(activate + "reject a")
    machine.succeed(publish + "b")
    machine.succeed(activate + "accept b")
    machine.succeed(publish + "a")
    machine.succeed(activate + "accept a")
    assert machine.succeed("systemctl show --property=MainPID --value supybot.service") == bot_pid
    machine.fail("runuser -u mcp-js-limnoria -- cat /var/lib/limnoria-script-bundles/candidate.json")
    machine.fail("runuser -u limnoria-bundle-updater -- cat /var/lib/supybot/tcl-last-good.json")
    machine.fail("runuser -u limnoria-bundle-updater -- ${pkgs.python3}/bin/python3 -I ${./control_client.py} restart")
  '';
}
