import socket
for path in ['/run/not-provisioned', '/var/lib/supybot/state-marker',
             '/var/lib/limnoria-managed-scripts/source-marker']:
    try:
        with open(path, 'rb') as stream:
            stream.read(1)
    except (PermissionError, FileNotFoundError):
        pass
    else:
        raise AssertionError('worker can read a protected host path')
try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
except OSError:
    pass
else:
    sock.close()
    raise AssertionError('worker can create an Internet socket')
print('WORKER-SANDBOX-PROBE-PASSED')
# The real controller is already listening on host loopback. A Tcl worker must
# not acquire its intentionally tokenless capabilities through that HTTP path.
try:
    connection = socket.create_connection(('127.0.0.1', 8767), timeout=1)
except OSError:
    pass
else:
    connection.close()
    raise AssertionError('worker can reach the host HTTP controller')
