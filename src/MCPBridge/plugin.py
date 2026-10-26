"""Immutable host bridge: no agent-controlled Python imports or IRC dispatch."""
import os
import threading
from pathlib import Path
import uvicorn
from supybot import callbacks
from supybot.commands import wrap, optional
from limnoria_mcp.files import ScriptFiles
from limnoria_mcp.runtime import ScriptRuntime
from limnoria_mcp.server import create_app


class MCPBridge(callbacks.Plugin):
    """Owner-invoked Safe Tcl scripts through a separate unprivileged worker."""
    threaded = True

    def __init__(self, irc):
        super().__init__(irc)
        self.files = ScriptFiles(os.environ['LIMNORIA_MCP_SCRIPT_ROOT'])
        bundle_root = os.environ.get('LIMNORIA_MCP_BUNDLE_DIR')
        self.runtime = ScriptRuntime(self.files, gitops=bool(bundle_root))
        self.watcher = None
        if bundle_root:
            from limnoria_mcp.bundle_runtime import BundleWatcher
            self.watcher = BundleWatcher(self.runtime, bundle_root,
                os.environ['LIMNORIA_MCP_LAST_GOOD_FILE'],
                int(os.environ.get('LIMNORIA_MCP_BUNDLE_POLL_SECONDS', '15')))
            self.watcher.thread.start()
        token = Path(os.environ['LIMNORIA_MCP_TOKEN_FILE']).read_text().strip()
        app = create_app(self.files, self.runtime, token)
        port = int(os.environ.get('LIMNORIA_MCP_PORT', '8765'))
        self.server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                                   access_log=False, log_level='warning'))
        self.thread = threading.Thread(target=self.server.run, name='limnoria-mcp', daemon=True)
        self.thread.start()

    def tclrun(self, irc, msg, args, name, text):
        """<script> [text]

        Run an activated Safe Tcl script. Owner capability required. Replies are
        confined to this invocation; scripts cannot choose another destination.
        """
        context = {'network': irc.network, 'channel': msg.args[0],
                   'nick': msg.nick, 'text': text or ''}
        try:
            result = self.runtime.invoke(name, context)
        except Exception as exc:
            effects = getattr(exc, 'effects', [])
            identifiers = ', '.join(effect['operation_id'] for effect in effects)
            detail = (' Operations: ' + identifiers) if identifiers else ''
            irc.error('Safe Tcl failed; replies/state were discarded, but external HTTP effects '
                      'may have occurred. Do not automatically retry.' + detail)
            return
        for reply in result['replies']:
            irc.reply(reply, prefixNick=False)

    tclrun = wrap(tclrun, [('checkCapability', 'owner'), 'something', optional('text')])

    def die(self):
        if self.watcher:
            self.watcher.stop.set()
            self.watcher.thread.join(timeout=5)
        self.server.should_exit = True
        self.thread.join(timeout=5)
        if not self.thread.is_alive():
            self.files.close()
        super().die()
