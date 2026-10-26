import json, socket
with socket.socket(socket.AF_UNIX) as connection:
    connection.connect('/run/limnoria-mcp-control/control.sock')
    connection.sendall(b'restart sshd.service\n')
    response = json.loads(connection.recv(4096))
    assert response == {'error': 'unsupported action'}, response
