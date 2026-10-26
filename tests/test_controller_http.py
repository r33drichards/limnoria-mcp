"""Tokenless loopback HTTP controller recovery with Host/Origin boundaries."""
import socket
import subprocess
from pathlib import Path
import sys
import threading
import time
import anyio
import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient
from limnoria_mcp.control import create_controller_app
from limnoria_mcp.files import ScriptFiles
from limnoria_mcp.server import create_app

BOT_TOKEN = 'synthetic-bot-token-for-tests-11111111111'


def test_controller_local_guards_and_bot_still_requires_auth(tmp_path):
    files = ScriptFiles(str(tmp_path))
    app = create_controller_app('/immutable/synthetic-helper', files)
    headers = {'Host':'127.0.0.1:8767'}
    with TestClient(app) as client:
        # The explicitly authorized local controller needs no bearer; protocol
        # requests still go through Host, Origin and bounded-body protection.
        result = client.post('/mcp', headers={**headers,'Accept':'application/json, text/event-stream'},
            json={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
        assert result.status_code == 200
        assert client.post('/mcp', headers={**headers,'Host':'attacker.example'},json={}).status_code == 421
        assert client.post('/mcp', headers={**headers,'Origin':'http://localhost'},json={}).status_code == 401
        assert client.post('/mcp',headers=headers,content=b'x' * (1024 * 1024 + 1)).status_code == 413
    with TestClient(create_app(files, None, BOT_TOKEN)) as bot:
        assert bot.post('/mcp', headers=headers, json={}).status_code == 401
    files.close()


def test_real_http_controller_recovers_with_bot_offline(tmp_path):
    helper = tmp_path / 'helper'
    helper.write_text('#!' + sys.executable + '\nimport json,sys\nprint(json.dumps({"action":sys.argv[1],"returncode":0,"stdout":"synthetic","stderr":""}))\n')
    helper.chmod(0o700)
    files = ScriptFiles(str(tmp_path))
    # No bot process or proxy environment is configured at all.
    app = create_controller_app(str(helper), files)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets':[listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        # Exercise the exact systemd readiness probe against real FastMCP HTTP;
        # it initializes only and must never invoke recovery tools.
        subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] /
                       'nix' / 'controller_ready.py'), str(port)], check=True, timeout=25)
        async def run():
            async with httpx.AsyncClient(trust_env=False) as client:
                async with streamable_http_client(f'http://127.0.0.1:{port}/mcp', http_client=client) as streams:
                    async with ClientSession(streams[0],streams[1]) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert len(tools.tools) == 9
                        result = await session.call_tool('service_action', {'action':'start'})
                        assert not result.isError
                        assert 'synthetic' in result.content[0].text
                        result = await session.call_tool('write_script', {'name':'Repair','source':'reply repaired'})
                        assert not result.isError
                        result = await session.call_tool('read_script', {'name':'Repair'})
                        assert not result.isError
                        assert result.content[0].text == 'reply repaired'
        anyio.run(run)
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
        files.close()


def test_entrypoint_is_loopback_only_and_needs_no_bot_connection(tmp_path, monkeypatch):
    from unittest.mock import patch
    from limnoria_mcp.control import main
    monkeypatch.setenv('LIMNORIA_MCP_CONTROL_HELPER', '/immutable/test-helper')
    monkeypatch.setenv('LIMNORIA_MCP_SCRIPT_ROOT', str(tmp_path))
    monkeypatch.setenv('LIMNORIA_MCP_CONTROLLER_PORT', '8767')
    monkeypatch.setenv('LIMNORIA_MCP_HOST', '0.0.0.0')  # no configurable bind escape
    monkeypatch.delenv('LIMNORIA_MCP_TOKEN_FILE', raising=False)
    with patch('uvicorn.run') as run:
        main()
    assert run.call_args.kwargs['host'] == '127.0.0.1'
    assert run.call_args.kwargs['port'] == 8767
