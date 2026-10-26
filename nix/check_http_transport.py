"""CI-only actual pinned mcp-js -> HTTP controller contract, no bot/JWKS/systemd.

Source checked at 54ac96cf: default transport is stdio when ports are absent;
HTTP upstream config is {name, transport:'http', url}; default informational stub naming is
runjs__<server>__<tool>; invoke tools through run_js with {code}. Production frontend is HTTP+JWKS,
but this isolated client-facing stdio harness does not assert JWT coverage.
"""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import uvicorn
from limnoria_mcp.control import create_controller_app
from limnoria_mcp.files import ScriptFiles


async def check(binary, root, port):
    upstream = root / 'upstream.json'
    upstream.write_text(json.dumps([{'name': 'limnoria', 'transport': 'http',
                                    'url': f'http://127.0.0.1:{port}/mcp'}]))
    policy = root / 'tools.rego'
    policy.write_text('package mcp.tools\ndefault allow := false\n'
                      'allow if { input.server == "limnoria"; '
                      'input.tool in {"list_scripts", "read_script", "write_script", "service_status"} }\n')
    policies = root / 'policies.json'
    policies.write_text(json.dumps({'mcp_tools': {'policies': [{'url': policy.as_uri()}]}}))
    env = {'PATH': '/nonexistent', 'HOME': str(root),
           'NO_PROXY': '127.0.0.1,localhost,::1', 'no_proxy': '127.0.0.1,localhost,::1',
           # Deliberately unusable: loopback must bypass an ambient HTTP proxy.
           'HTTP_PROXY': 'http://127.0.0.1:9', 'HTTPS_PROXY': 'http://127.0.0.1:9'}
    params = StdioServerParameters(command=str(binary),
        args=['--mcp-config', str(upstream), '--policies-json', str(policies)], env=env)
    with anyio.fail_after(60):
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert 'runjs__limnoria__write_script' in names, names
                for name, args, marker in [
                    ('write_script', {'name': 'HttpContract', 'source': 'reply http-contract-ok'}, 'HttpContract'),
                    ('read_script', {'name': 'HttpContract'}, 'http-contract-ok'),
                    ('service_status', {}, 'synthetic-status'),
                ]:
                    code = 'console.log(JSON.stringify(await mcp.callTool("limnoria", ' + json.dumps(name) + ', ' + json.dumps(args) + ')));'
                    result = await session.call_tool('run_js', {'code': code})
                    assert not result.isError, result
                    assert marker in result.model_dump_json(), result
                # Also exercise the explicit documented JavaScript MCP client API.
                result = await session.call_tool('run_js', {'code':
                    'console.log(JSON.stringify(await mcp.callTool("limnoria", "list_scripts", {})));'})
                assert not result.isError, result
                assert 'HttpContract' in result.model_dump_json(), result
    assert (root / 'drafts/HttpContract/script.tcl').read_text() == 'reply http-contract-ok'


def main():
    if len(sys.argv) != 2:
        raise SystemExit('expected the pinned server executable path')
    binary = Path(sys.argv[1]).resolve(strict=True)
    if not os.access(binary, os.X_OK):
        raise SystemExit('server is not executable')
    with tempfile.TemporaryDirectory(prefix='limnoria-http-contract-') as directory:
        root = Path(directory)
        (root / 'drafts').mkdir()
        helper = root / 'synthetic-helper'
        helper.write_text(f'#!{sys.executable}\nimport json,sys\n'
                          'assert sys.argv[1:] == ["status"]\n'
                          'print(json.dumps({"action":"status","returncode":0,'
                          '"stdout":"synthetic-status","stderr":""}))\n')
        helper.chmod(0o700)
        files = ScriptFiles(str(root / 'drafts'))
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_controller_app(str(helper), files),
                                              access_log=False, log_level='warning'))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started, 'HTTP controller failed to start'
            anyio.run(check, binary, root, port)
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            listener.close()
            files.close()
        assert not thread.is_alive(), 'HTTP controller did not stop'
    print('mcp-js HTTP discovery/tool contract passed (JWT/real bot not tested)')


if __name__ == '__main__':
    main()
