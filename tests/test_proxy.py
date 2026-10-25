"""Real Streamable HTTP client round trip, with synthetic local credentials."""
import socket
import threading
import time
import anyio
import uvicorn
from limnoria_mcp.control import proxy_tool
from limnoria_mcp.files import PluginFiles
from limnoria_mcp.server import create_app


def test_proxy_roundtrip(tmp_path, monkeypatch):
    token = tmp_path / 'test-token'
    token.write_text('synthetic-test-only-token-for-local-tests-0000')
    plugins = tmp_path / 'plugins'
    plugins.mkdir()
    files = PluginFiles(str(plugins))
    app = create_app(files, lambda name, reload: {'plugin': name}, token.read_text())
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets':[listener]}, daemon=True)
    thread.start()
    monkeypatch.setenv('LIMNORIA_MCP_TOKEN_FILE', str(token))
    monkeypatch.setenv('LIMNORIA_MCP_PORT', str(port))
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        async def run():
            result = await proxy_tool('write_plugin_file', {'plugin':'AgentRoundtrip','filename':'plugin.py','content':'value=1'})
            assert not result.isError
            result = await proxy_tool('read_plugin_file', {'plugin':'AgentRoundtrip','filename':'plugin.py'})
            assert result.content[0].text == 'value=1'
        anyio.run(run)
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
        files.close()
