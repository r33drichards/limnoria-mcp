import json, socket, sys, time
request = {'script': sys.argv[1], 'state': {}, 'phase': sys.argv[2] if len(sys.argv) > 2 else 'run', 'context':
           {'network': 'offline', 'channel': '#fixture', 'nick': 'tester', 'text': 'hello'}}
with socket.socket(socket.AF_UNIX) as conn:
    conn.settimeout(2)
    conn.connect('/run/limnoria-tcl-worker/worker.sock')
    ready = bytearray()
    deadline = time.monotonic() + 2
    while len(ready) < 6:
        remaining = deadline - time.monotonic()
        assert remaining > 0
        conn.settimeout(remaining)
        chunk = conn.recv(6 - len(ready))
        assert chunk
        ready.extend(chunk)
    assert ready == b'READY\n'
    conn.settimeout(30)
    conn.sendall(json.dumps(request).encode() + b'\n')
    response = bytearray()
    while len(response) <= 65536:
        chunk = conn.recv(4096)
        if not chunk:
            break
        response.extend(chunk)
assert len(response) <= 65536
print(json.dumps(json.loads(response)))
