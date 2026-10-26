"""Bounded loopback MCP initialization readiness probe; never invokes a tool."""
import json
import sys
import time
import urllib.error
import urllib.request


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit() or not 1 <= int(sys.argv[1]) <= 65535:
        raise SystemExit('expected controller port')
    url = 'http://127.0.0.1:' + sys.argv[1] + '/mcp'
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-03-26', 'capabilities': {},
        'clientInfo': {'name': 'systemd-readiness', 'version': '1'}}}).encode()
    # Never use ambient proxy settings for this trusted fixed loopback endpoint.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        request = urllib.request.Request(url, data=body, headers={
            'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
        try:
            with opener.open(request, timeout=1) as response:
                raw = response.read(65537)
                if response.status == 200 and len(raw) <= 65536:
                    document = json.loads(raw)
                    result = document.get('result') if isinstance(document, dict) else None
                    if (isinstance(document, dict) and document.get('id') == 1 and isinstance(result, dict)
                            and isinstance(result.get('protocolVersion'), str)
                            and isinstance(result.get('serverInfo'), dict)):
                        return
        except (OSError, urllib.error.URLError, ValueError):
            pass
        time.sleep(0.2)
    raise SystemExit('controller MCP initialization did not become ready')


if __name__ == '__main__':
    main()
