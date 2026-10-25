"""Actual wrapped Limnoria command and Safe Tcl; fake transport sends no live IRC."""
import logging
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from supybot import callbacks, ircmsgs
from MCPBridge.plugin import MCPBridge
from limnoria_mcp.files import ScriptFiles
from limnoria_mcp.runtime import ScriptRuntime
from limnoria_mcp.tcl_worker import run_limited

class Transport:
    network = 'test-network'
    def __init__(self):
        self.replies, self.errors = [], []
    def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))
    def errorNoCapability(self, cap, Raise=False):
        raise callbacks.Error("owner required")
    def error(self, text):
        self.errors.append(text)


def test_wrapped_owner_command_uses_real_context_and_fixed_reply(tmp_path):
    files = ScriptFiles(str(tmp_path))
    runtime = ScriptRuntime(files, execute=run_limited)
    files.write('Hello', 'reply "[context_get network]|[context_get channel]|[context_get nick]|[context_get text]"')
    runtime.activate('Hello')
    host = SimpleNamespace(runtime=runtime, log=logging.getLogger('test-bridge'))
    transport = Transport()
    message = ircmsgs.privmsg('#allowed', 'tclrun Hello input', prefix='alice!user@example.invalid')
    with patch('supybot.ircdb.checkCapability', return_value=True) as capability:
        MCPBridge.tclrun(host, transport, message, ['Hello', 'input'])
    capability.assert_called_once_with(message.prefix, 'owner')
    assert transport.replies == [('test-network|#allowed|alice|input', {'prefixNick':False})]
    with patch('supybot.ircdb.checkCapability', return_value=False):
        with pytest.raises(callbacks.Error):
            MCPBridge.tclrun(host, transport, message, ['Hello', 'input'])
    assert len(transport.replies) == 1
    files.close()
