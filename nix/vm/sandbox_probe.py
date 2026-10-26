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
