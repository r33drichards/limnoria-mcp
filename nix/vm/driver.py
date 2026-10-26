import json
start_all()
machine.wait_for_unit("multi-user.target")
machine.wait_for_unit("supybot.service")
machine.wait_until_succeeds("test -S /run/limnoria-mcp-control/control.sock")
client = "runuser -u limnoria-mcp-controller -- @plainPython@ -I @controlClient@ "
def action(name):
    result = json.loads(machine.succeed(client + name))
    assert result["returncode"] == 0, result
    return result
assert "ActiveState=active" in action("status")["stdout"]
action("stop")
machine.wait_until_fails("systemctl is-active --quiet supybot.service")
machine.wait_for_unit("limnoria-mcp-controller.service")
# Frontend UID has no raw files/broker privileges, but trusted local HTTP is
# explicitly tokenless and must provide recovery while the bot endpoint is down.
machine.succeed("runuser -u mcp-js-limnoria -- @plainPython@ -I @httpControllerProbe@")
machine.succeed("ss -lnt | grep -F '127.0.0.1:8767'")
machine.fail("ss -lnt | grep -E '(0.0.0.0|\\*|\\[::\\]):8767'")

# Same confined API, real separate UIDs, while the bot process is absent.
probe = "@packagePython@ @fileProbe@"
def write_as(user, text, plugin="AgentFixture"):
    # Exercise actual systemd UMask and SUID/SGID syscall filtering, not only
    # Unix identities. SGID must be inherited, never explicitly requested.
    result = machine.succeed("systemd-run --quiet --wait --pipe --collect -p User=" + user
                    + " -p UMask=0007 -p RestrictSUIDSGID=yes -p NoNewPrivileges=yes -- "
                    + probe + " " + text + " " + plugin)
    assert text in result
write_as("limnoria-mcp-controller", "controller-edit")
assert machine.succeed("stat -c '%a %G' /var/lib/limnoria-managed-scripts/AgentFixture").strip() == "2770 limnoria-managed-scripts"
assert machine.succeed("stat -c '%a %G' /var/lib/limnoria-managed-scripts/AgentFixture/script.tcl").strip() == "660 limnoria-managed-scripts"
assert "controller-edit" in machine.succeed("runuser -u supybot -- " + probe)
write_as("supybot", "bot-edit")
assert "bot-edit" in machine.succeed("runuser -u limnoria-mcp-controller -- " + probe)
# The reverse ownership direction must create a fresh directory, not only
# replace a file inside one that the controller already created.
write_as("supybot", "bot-created", "AgentBotCreated")
assert machine.succeed("stat -c '%a %U %G' /var/lib/limnoria-managed-scripts/AgentBotCreated").strip() == "2770 supybot limnoria-managed-scripts"
write_as("limnoria-mcp-controller", "controller-repaired", "AgentBotCreated")
assert machine.succeed("stat -c '%a %U %G' /var/lib/supybot").strip() == "700 supybot supybot"
machine.fail("runuser -u limnoria-mcp-controller -- ls /var/lib/supybot")
machine.fail("runuser -u mcp-js-limnoria -- ls /var/lib/supybot")
machine.fail("runuser -u mcp-js-limnoria -- ls /var/lib/limnoria-managed-scripts")
machine.fail("runuser -u mcp-js-limnoria -- @plainPython@ -I @controlClient@ status")
machine.fail("runuser -u supybot -- @plainPython@ -I @controlClient@ restart")
machine.fail(client + "daemon-reload")
machine.fail(client + "restart sshd.service")
machine.succeed("runuser -u limnoria-mcp-controller -- @plainPython@ -I @invalidProbe@")
assert "ActiveState=inactive" in action("status")["stdout"]

action("start")
machine.wait_for_unit("supybot.service")
before = machine.succeed("systemctl show --property=MainPID --value supybot.service").strip()
machine.succeed("chmod 0755 /var/lib/supybot")
action("restart")
assert machine.succeed("stat -c %a /var/lib/supybot").strip() == "700"
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
worker = "runuser -u supybot -- @plainPython@ -I @workerProbe@ "
result = json.loads(machine.succeed(worker + "'reply hello'"))
assert result == {"replies": ["hello"], "state": {}, "effects": []}
machine.wait_for_unit("limnoria-tcl-worker.service")
# Staging uses a synthetic response without DNS or any broker connection.
staged = json.loads(machine.succeed(worker + "'reply [dict get [http_get https://unused.example.invalid/feed] outcome]' validate"))
assert staged == {"replies": ["dry_run"], "state": {}, "effects": []}

for script in ["exec id", "open /etc/passwd", "socket localhost 80", "while {1} {}"]:
    import shlex
    result = json.loads(machine.succeed(worker + shlex.quote(script)))
    assert "error" in result
# Failed/limited script cannot poison the next fresh invocation.
assert json.loads(machine.succeed(worker + "'reply recovered'"))["replies"] == ["recovered"]
machine.fail("runuser -u limnoria-mcp-controller -- @plainPython@ -I @workerProbe@ 'reply forbidden'")
machine.fail("runuser -u limnoria-tcl-worker -- ls /var/lib/limnoria-managed-scripts")
machine.fail("runuser -u limnoria-tcl-worker -- ls /var/lib/supybot")
machine.fail("runuser -u limnoria-tcl-worker -- @plainPython@ -I @controlClient@ status")
assert machine.succeed("systemctl show -p User --value limnoria-tcl-worker.service").strip() == "limnoria-tcl-worker"
assert machine.succeed("systemctl show -p PrivateNetwork --value limnoria-tcl-worker.service").strip() == "yes"

# Actual broker Unix capability: only worker can call it; loopback blocked
# before sending anything. No real public endpoint or webhook is used.
machine.wait_for_unit("limnoria-http-broker.service")
http_probe = "@packagePython@ @publicHttpProbe@"
assert "PUBLIC-HTTP-INTERNAL-DENIED" in machine.succeed("runuser -u limnoria-tcl-worker -- " + http_probe)
blocked = json.loads(machine.succeed(worker + "'http_get http://127.0.0.1:8767/mcp'"))
assert "error" in blocked
assert len(blocked["effects"]) == 1
assert blocked["effects"][0]["outcome"] == "failed_before_send"

for user in ["supybot", "limnoria-mcp-controller", "mcp-js-limnoria", "limnoria-bundle-updater"]:
    machine.fail("runuser -u " + user + " -- " + http_probe)
machine.fail("runuser -u limnoria-http -- ls /var/lib/supybot")
machine.fail("runuser -u limnoria-http -- ls /var/lib/limnoria-managed-scripts")
machine.fail("runuser -u limnoria-http -- @plainPython@ -I @controlClient@ status")

# Synthetic candidate publication under the updater UID; no Nix/network call.
publish = "runuser -u limnoria-bundle-updater -- @packagePython@ @publishFixture@ "
activate = "runuser -u supybot -- @packagePython@ @bundleProbe@ "
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
machine.fail("runuser -u limnoria-mcp-controller -- cat /var/lib/limnoria-script-bundles/candidate.json")
machine.fail("runuser -u limnoria-bundle-updater -- cat /var/lib/supybot/tcl-last-good.json")
machine.fail("runuser -u limnoria-bundle-updater -- @plainPython@ -I @controlClient@ restart")
