"""Actual Unix peer/DAC and internal-address rejection; no public requests."""
from limnoria_mcp.public_http import call_broker, BrokerError

try:
    call_broker({'method': 'POST', 'url': 'http://127.0.0.1:8767/mcp',
                 'operation_id': 'vm-blocked-local-request'})
except BrokerError as exc:
    assert exc.outcome == 'failed_before_send', exc
else:
    raise AssertionError('loopback request must never be sent')
print('PUBLIC-HTTP-INTERNAL-DENIED')
