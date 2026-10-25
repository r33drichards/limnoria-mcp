import concurrent.futures
import importlib.util
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from supybot import callbacks, conf, plugin, schedule
from limnoria_mcp.files import PluginFiles, plugin_name
from limnoria_mcp.server import create_app


class MCPBridge(callbacks.Plugin):
    """Local MCP bridge. No commands are exposed to IRC users."""
    def __init__(self, irc):
        super().__init__(irc)
        self.irc = irc
        self.files = PluginFiles(os.environ['LIMNORIA_MCP_PLUGIN_ROOT'])
        token = Path(os.environ['LIMNORIA_MCP_TOKEN_FILE']).read_text().strip()
        self.closed = False
        app = create_app(self.files, self.lifecycle, token)
        port = int(os.environ.get('LIMNORIA_MCP_PORT', '8765'))
        self.server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                                   access_log=False, log_level='warning'))
        self.thread = threading.Thread(target=self.server.run, name='limnoria-mcp', daemon=True)
        self.thread.start()

    def lifecycle(self, name, reload):
        plugin_name(name)
        if self.closed:
            raise RuntimeError('Bridge is stopping')
        future = concurrent.futures.Future()
        def run():
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(self._load(name, reload))
            except Exception as exc:
                future.set_exception(exc)
        schedule.addEvent(run, time.time(), name='mcp-' + uuid.uuid4().hex)
        try:
            return future.result(timeout=15)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise RuntimeError('Bot loop timed out; action may be running, inspect status before retry') from None

    def _load(self, name, reload):
        if self.closed:
            raise RuntimeError('Bridge is stopping')
        # Validate every supported source file without following links before import.
        self.files.read(name, '__init__.py')
        for filename in ('plugin.py', 'config.py'):
            try:
                self.files.read(name, filename)
            except FileNotFoundError:
                pass
        existing = self.irc.getCallback(name)
        if bool(existing) != bool(reload):
            raise ValueError('Use reload for a loaded plugin, load for a new plugin')
        old_modules = {k: v for k, v in sys.modules.copy().items()
                       if k == name or k.startswith(name + '.')}
        for key in old_modules:
            del sys.modules[key]
        path = str(Path(self.files.root) / name / '__init__.py')
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        removed = []
        try:
            spec.loader.exec_module(module)
            removed = self.irc.removeCallback(name) if existing else []
            callback = plugin.loadPluginClass(self.irc, module)
        except Exception:
            for key in list(sys.modules):
                if key == name or key.startswith(name + '.'):
                    del sys.modules[key]
            sys.modules.update(old_modules)
            for cb in removed:
                self.irc.addCallback(cb)
            raise
        cleanup_errors = 0
        for cb in removed:
            try:
                cb.die()
            except Exception:
                cleanup_errors += 1
        conf.registerPlugin(callback.name(), True)
        return {'plugin': callback.name(), 'action': 'reload' if reload else 'load',
                'cleanup_errors': cleanup_errors}

    def die(self):
        self.closed = True
        self.server.should_exit = True
        self.thread.join(timeout=5)
        if not self.thread.is_alive():
            self.files.close()
        super().die()
