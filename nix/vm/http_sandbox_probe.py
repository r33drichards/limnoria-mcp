"""Executed inside actual network broker unit; no network traffic."""
import os
import socket

for path in ['/run/not-provisioned', '/var/lib/supybot/state-marker',
             '/var/lib/limnoria-managed-scripts/source-marker']:
    try:
        with open(path, 'rb'):
            pass
    except (PermissionError, FileNotFoundError):
        pass
    else:
        raise AssertionError('broker can read protected path: ' + path)
# Socket creation itself is permitted in broker, never Tcl worker; no connects.
for family in [socket.AF_INET, socket.AF_INET6]:
    with socket.socket(family, socket.SOCK_STREAM):
        pass

# AF_NETLINK must support actual self-address enumeration, not merely cause
# every request to fail closed. This queries the local kernel, never the network.
from limnoria_mcp.public_http import local_addresses
assert '127.0.0.1' in local_addresses()

# Verify the actual immutable Nix CA closure can construct a verified TLS
# context, without contacting any endpoint or trusting environment paths.
import ssl
from limnoria_mcp.public_http import PUBLIC_CA_FILE
assert PUBLIC_CA_FILE.startswith('/nix/store/')
context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
context.load_verify_locations(cafile=PUBLIC_CA_FILE)
assert context.cert_store_stats()['x509_ca'] > 0
