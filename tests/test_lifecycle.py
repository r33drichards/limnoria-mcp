from MCPBridge.plugin import MCPBridge
from limnoria_mcp.files import PluginFiles
import pytest

class FakeIrc:
    def __init__(self):
        self.callbacks = {}
    def getCallback(self, name):
        return self.callbacks.get(name)
    def addCallback(self, callback):
        self.callbacks[callback.name()] = callback
    def removeCallback(self, name):
        callback = self.callbacks.pop(name, None)
        return [callback] if callback else []


def test_real_limnoria_lifecycle(tmp_path):
    bridge = object.__new__(MCPBridge)
    bridge.closed = False
    bridge.irc = FakeIrc()
    bridge.files = PluginFiles(str(tmp_path))
    bridge.files.write('AgentHello', '__init__.py', 'from .plugin import Class\n')
    bridge.files.write('AgentHello', 'plugin.py', 'from supybot import callbacks\nclass AgentHello(callbacks.Plugin):\n    value = 1\nClass = AgentHello\n')
    assert bridge._load('AgentHello', False)['action'] == 'load'
    assert bridge.irc.getCallback('AgentHello').value == 1
    bridge.files.write('AgentHello', 'plugin.py', 'from supybot import callbacks\nclass AgentHello(callbacks.Plugin):\n    value = 2\nClass = AgentHello\n')
    assert bridge._load('AgentHello', True)['action'] == 'reload'
    old = bridge.irc.getCallback('AgentHello')
    assert old.value == 2
    bridge.files.write('AgentHello', 'plugin.py', 'raise RuntimeError("bad plugin")\n')
    with pytest.raises(RuntimeError):
        bridge._load('AgentHello', True)
    assert bridge.irc.getCallback('AgentHello') is old
    bridge.files.close()
