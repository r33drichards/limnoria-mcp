"""Actual tokenless loopback controller protocol/authorization-boundary fixture."""
import json
import urllib.error
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
url = 'http://127.0.0.1:8767/mcp'


def rpc(method, params, extra_headers=None):
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
    headers.update(extra_headers or {})
    request = urllib.request.Request(url, json.dumps({
        'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode(), headers)
    with opener.open(request, timeout=40) as response:
        raw = response.read(262145)
        assert len(raw) <= 262144
        document = json.loads(raw)
        assert 'error' not in document, document
        return document['result']


def call(name, arguments):
    result = rpc('tools/call', {'name': name, 'arguments': arguments})
    assert not result.get('isError'), result
    return result


initialized = rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                'clientInfo': {'name': 'vm-probe', 'version': '1'}})
assert 'serverInfo' in initialized
names = {tool['name'] for tool in rpc('tools/list', {})['tools']}
assert {'read_script', 'write_script', 'service_action', 'service_status'} <= names
assert not {'run_shell', 'write_plugin_file', 'load_plugin'} & names
call('write_script', {'name': 'HttpDraft', 'source': 'reply http-ok'})
assert 'http-ok' in str(call('read_script', {'name': 'HttpDraft'}))
assert 'ActiveState=inactive' in str(call('service_status', {}))
call('service_action', {'action': 'start'})
assert 'ActiveState=active' in str(call('service_status', {}))
call('service_action', {'action': 'stop'})
assert 'ActiveState=inactive' in str(call('service_status', {}))
for headers, expected in [({'Host': 'evil.invalid'}, 421),
                          ({'Origin': 'http://evil.invalid'}, 401),
                          ({'Origin': 'null'}, 401)]:
    try:
        rpc('tools/list', {}, headers)
    except urllib.error.HTTPError as exc:
        assert exc.code == expected
    else:
        raise AssertionError('Host/Origin guard accepted browser request')
print('TOKENLESS-LOOPBACK-CONTROLLER-PASSED')
