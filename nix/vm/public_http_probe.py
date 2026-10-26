"""Require real IPC admission, then internal-address rejection; no public traffic."""
import json
import socket
import time

# Do not use call_broker's error wrapper here: both a denied Unix connection
# and an admitted request rejected by destination policy are failed_before_send.
# The UID test must distinguish those boundaries, not accept either outcome.
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
    conn.settimeout(2)
    conn.connect('/run/limnoria-http-broker/http.sock')
    ready = bytearray()
    deadline = time.monotonic() + 2
    while len(ready) < 6:
        remaining = deadline - time.monotonic()
        assert remaining > 0
        conn.settimeout(remaining)
        chunk = conn.recv(6 - len(ready))
        assert chunk, 'broker refused admission'
        ready.extend(chunk)
    assert time.monotonic() < deadline
    assert ready == b'READY\n'
    conn.settimeout(10)
    conn.sendall(json.dumps({'method': 'POST', 'url': 'http://127.0.0.1:8767/mcp',
                            'operation_id': 'vm-blocked-local-request'}).encode())
    conn.shutdown(socket.SHUT_WR)
    response = bytearray()
    while len(response) <= 65536:
        chunk = conn.recv(4096)
        if not chunk:
            break
        response.extend(chunk)
    assert len(response) <= 65536
    document = json.loads(response)
    assert document['operation_id'] == 'vm-blocked-local-request'
    assert document['outcome'] == 'failed_before_send'
    assert 'error' in document
print('PUBLIC-HTTP-INTERNAL-DENIED')
