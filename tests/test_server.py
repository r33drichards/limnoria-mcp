from starlette.testclient import TestClient
from limnoria_mcp.server import create_app
from limnoria_mcp.files import PluginFiles

TOKEN = 'synthetic-test-token-not-a-secret-123456'

def test_auth_and_protocol(tmp_path):
    files = PluginFiles(str(tmp_path))
    app = create_app(files, lambda name, reload: {'plugin': name}, TOKEN)
    headers = {'Authorization': 'Bearer ' + TOKEN, 'Accept': 'application/json, text/event-stream',
               'Host': '127.0.0.1:8765'}
    with TestClient(app) as client:
        assert client.post('/mcp', json={}, headers={'Host':'127.0.0.1:8765'}).status_code == 401
        assert client.post('/mcp', json={}, headers={**headers, 'Host':'attacker.example'}).status_code == 421
        assert client.post('/mcp', json={}, headers={**headers, 'Origin': 'http://evil.example'}).status_code == 401
        response = client.post('/mcp', headers=headers, json={'jsonrpc':'2.0', 'id':1, 'method':'initialize', 'params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
        assert response.status_code == 200, response.text
        assert 'serverInfo' in response.json()['result']
        response = client.post('/mcp', headers=headers, json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
        assert {t['name'] for t in response.json()['result']['tools']} == {'list_plugins','read_plugin_file','write_plugin_file','load_plugin','reload_plugin'}
        response = client.post('/mcp', headers=headers, json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'write_plugin_file','arguments':{'plugin':'AgentHello','filename':'plugin.py','content':'x=1'}}})
        assert not response.json()['result'].get('isError'), response.text
        assert files.read('AgentHello','plugin.py') == 'x=1'
    files.close()


def test_chunked_body_limit(tmp_path):
    import anyio
    from limnoria_mcp.server import LocalBearer
    reached = []
    async def downstream(scope, receive, send):
        reached.append(True)
    app = LocalBearer(downstream, TOKEN)
    messages = iter([
        {'type':'http.request', 'body':b'x' * (512 * 1024), 'more_body':True},
        {'type':'http.request', 'body':b'x' * (512 * 1024 + 1), 'more_body':False},
    ])
    responses = []
    async def receive():
        return next(messages)
    async def send(message):
        responses.append(message)
    async def run():
        await app({'type':'http', 'headers':[(b'host',b'127.0.0.1:8765'),
                  (b'authorization',('Bearer ' + TOKEN).encode())]}, receive, send)
    anyio.run(run)
    assert responses[0]['status'] == 413
    assert reached == []
