import json, socket, sys
request = {'script': sys.argv[1], 'state': {}, 'context':
           {'network': 'offline', 'channel': '#fixture', 'nick': 'tester', 'text': 'hello'}}
with socket.socket(socket.AF_UNIX) as conn:
    conn.settimeout(10)
    conn.connect('/run/limnoria-tcl-worker/worker.sock')
    conn.sendall(json.dumps(request).encode() + b'\n')
    response = bytearray()
    while len(response) <= 65536:
        chunk = conn.recv(4096)
        if not chunk:
            break
        response.extend(chunk)
assert len(response) <= 65536
print(json.dumps(json.loads(response)))
