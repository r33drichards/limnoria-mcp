import base64
import ipaddress
import json
import socket
import ssl
import time

import pytest

from limnoria_mcp import public_http as h


@pytest.mark.parametrize('address', [
    '127.0.0.1', '0.0.0.0', '10.1.2.3', '172.16.0.1', '192.168.1.1',
    '169.254.169.254', '100.100.100.200', '168.63.129.16', '224.0.0.1',
    '255.255.255.255', '192.0.0.9', '192.88.99.1', '198.18.0.1',
    '192.0.2.1', '198.51.100.1', '203.0.113.1', '240.0.0.1',
    '::', '::1', 'fc00::1', 'fec0::1', 'fe80::1', 'ff02::1', '2001:db8::1',
    '::ffff:8.8.8.8', '::ffff:127.0.0.1', '::8.8.8.8',
    '64:ff9b::808:808', '64:ff9b:1::a00:1', '2002:0808:0808::1',
    '2001:0:4136:e378:8000:63bf:3fff:fdd2',
])
def test_non_public_addresses_rejected(address):
    with pytest.raises(h.HttpPolicyError):
        h.public_address(address)


@pytest.mark.parametrize('address', ['8.8.8.8', '1.1.1.1', '2606:4700:4700::1111'])
def test_public_addresses(address):
    assert str(h.public_address(address)) == address
    with pytest.raises(h.HttpPolicyError):
        h.public_address(address, [address])


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://example.com',
    'http://user:pass@example.com', 'http://example.com/#secret',
    'http://example.com\r\nX:yes', 'http://example.com/a b',
    'http://example.com\\@127.0.0.1', 'http://[fe80::1%25eth0]/',
    'http://example.com:0', 'http://example.com:65536',
    'http://example.com.', 'http://-example.com', 'http://example..com',
    'http://éxample.com'])
def test_url_rejection(url):
    with pytest.raises(h.HttpPolicyError):
        h.validate_request('GET', url)


def test_dryrun_has_absolutely_no_network_or_interface_lookups():
    def forbidden(*args):
        pytest.fail('dryrun attempted IO')
    broker = h.PublicHttpBroker(resolver=forbidden, sender=forbidden, interfaces=forbidden)
    assert broker.request('POST', 'https://example.com/hook', body=b'private', dry_run=True)['dry_run']


def test_mixed_dns_rrset_fails_closed_before_transport():
    calls = []
    broker = h.PublicHttpBroker(resolver=lambda *_: ['8.8.8.8', '127.0.0.1'],
        sender=lambda *args: calls.append(args), interfaces=lambda: [])
    with pytest.raises(h.HttpPolicyError):
        broker.request('GET', 'https://example.com')
    assert calls == []


def test_dns_pinning_no_redirect_or_post_retry():
    calls = []
    def dns(*args):
        calls.append(('dns', args))
        return ['8.8.8.8', '1.1.1.1']
    def sender(request, ip, deadline):
        calls.append(('send', request, str(ip)))
        return {'status': 302, 'headers': [('Location', 'http://127.0.0.1')], 'body_base64': ''}
    broker = h.PublicHttpBroker(resolver=dns, sender=sender, interfaces=lambda: [])
    result = broker.request('POST', 'https://example.com/a?b=c', body=b'data')
    assert result['status'] == 302
    assert [x[0] for x in calls] == ['dns', 'send']
    assert calls[1][2] == '8.8.8.8'
    calls.clear()
    def failure(*args):
        calls.append(('send',))
        raise OSError('uncertain delivery')
    broker.sender = failure
    with pytest.raises(h.BrokerError) as exc:
        broker.request('POST', 'https://example.com', body=b'data')
    assert [x[0] for x in calls] == ['dns', 'send']


@pytest.mark.parametrize('headers', [{'Host': 'localhost'}, {'Proxy-Authorization': 'x'},
    {'Connection': 'upgrade'}, {'Transfer-Encoding': 'chunked'},
    {'Content-Length': '999'}, {'Accept': 'x\r\nFoo:bar'},
    {'accept': 'ok', 'Accept': 'duplicate'}])
def test_headers_rejected(headers):
    with pytest.raises(h.HttpPolicyError):
        h.validate_request('GET', 'http://example.com', headers)


def test_body_and_method_limits():
    for method, body in [('PATCH', b''), ('GET', b'data'), ('POST', b'x' * (h.MAX_BODY + 1))]:
        with pytest.raises(h.HttpPolicyError):
            h.validate_request(method, 'https://example.com', body=body)
    assert h.handle_request({'method': 'POST', 'url': 'https://example.com', 'body_base64': '???'})['outcome'] == 'failed_before_send'


def test_deadline_includes_dns(monkeypatch):
    monkeypatch.setattr(h, 'TIMEOUT', 0.02)
    broker = h.PublicHttpBroker(resolver=lambda *_: time.sleep(1), interfaces=lambda: [])
    before = time.monotonic()
    with pytest.raises(TimeoutError):
        broker.request('POST', 'https://example.com')
    assert time.monotonic() - before < 0.5


def test_interface_enumeration_is_local_only(monkeypatch):
    monkeypatch.setattr(socket, 'socket', lambda *_: pytest.fail('IP socket unexpectedly opened'))
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *_: pytest.fail('DNS unexpectedly used'))
    try:
        addresses = h.local_addresses()
    except h.HttpPolicyError as exc:
        # Restricted test executors can deny the local AF_NETLINK query.
        assert 'Unable to enumerate' in str(exc)
    else:
        assert '127.0.0.1' in addresses


@pytest.mark.parametrize('pinned_ca', [None, '/immutable/store/ca-bundle.crt'])
def test_transport_pins_numeric_ip_and_preserves_tls_identity(monkeypatch, pinned_ca):
    monkeypatch.setattr(h, 'PUBLIC_CA_FILE', pinned_ca)
    events = []
    class Sock:
        def settimeout(self, value): pass
        def connect(self, addr): events.append(('connect', addr))
        def close(self): pass
    class Context:
        def __init__(self, protocol):
            assert protocol == ssl.PROTOCOL_TLS_CLIENT
        def load_verify_locations(self, **kwargs): events.append(('ca', kwargs))
        def wrap_socket(self, sock, server_hostname):
            events.append(('sni', server_hostname))
            return sock
    class Response:
        status = 200
        def getheaders(self): return [('Content-Type', 'application/rss+xml')]
        def getheader(self, name, default): return default
        def read(self, limit): return b'<rss/>'
    class Connection:
        def __init__(self, host, port): events.append(('http', host, port))
        def request(self, method, target, **kwargs): events.append(('request', method, target, kwargs))
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(socket, 'socket', lambda *args: Sock())
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: pytest.fail('transport re-resolved DNS'))
    monkeypatch.setattr(ssl, 'SSLContext', Context)
    monkeypatch.setattr(h.http.client, 'HTTPConnection', Connection)
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:9999')
    monkeypatch.setenv('SSL_CERT_FILE', '/attacker/ca.pem')
    result = h.transport(h.validate_request('GET', 'https://example.com/feed'),
                         ipaddress.ip_address('8.8.8.8'), time.monotonic() + 2)
    assert ('connect', ('8.8.8.8', 443)) in events
    assert ('sni', 'example.com') in events
    assert base64.b64decode(result['body_base64']) == b'<rss/>'
    assert '/attacker/ca.pem' not in str(events)
    if pinned_ca:
        assert ('ca', {'cafile': pinned_ca, 'capath': None}) in events


def test_outcome_and_trusted_operation_id():
    ident = 'run-123:1'
    payload = {'method': 'POST', 'url': 'https://example.com', 'operation_id': ident}
    broker = h.PublicHttpBroker(resolver=lambda *_: ['127.0.0.1'], interfaces=lambda: [])
    result = h.handle_request(payload, broker)
    assert (result['operation_id'], result['outcome']) == (ident, 'failed_before_send')
    broker.resolver = lambda *_: ['8.8.8.8']
    def fail(*args):
        raise TimeoutError('secret URL should never be echoed')
    broker.sender = fail
    result = h.handle_request(payload, broker)
    assert (result['operation_id'], result['outcome']) == (ident, 'unknown')
    assert 'secret' not in json.dumps(result)
    broker.sender = lambda *_: {'status': 204, 'headers': [], 'body_base64': ''}
    assert h.handle_request(payload, broker)['outcome'] == 'completed'


def test_bounded_response_headers_without_socket():
    import io
    class FakeSocket:
        def makefile(self, mode):
            return io.BytesIO(b'HTTP/1.1 200 OK\r\nX-Large: ' + b'x' * 20000 + b'\r\n\r\n')
    response = h._BoundedResponse(FakeSocket())
    with pytest.raises(h.HttpPolicyError):
        response.begin()


def test_response_body_limit_without_socket(monkeypatch):
    class Sock:
        def settimeout(self, value): pass
        def connect(self, addr): pass
        def close(self): pass
    class Response:
        status = 200
        def getheaders(self): return []
        def getheader(self, name, default): return default
        def read(self, limit): return b'x' * limit
    class Connection:
        def __init__(self, *args): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(socket, 'socket', lambda *args: Sock())
    monkeypatch.setattr(h.http.client, 'HTTPConnection', Connection)
    with pytest.raises(h.HttpPolicyError):
        h.transport(h.validate_request('GET', 'http://example.com'),
                    ipaddress.ip_address('8.8.8.8'), time.monotonic() + 1)


def test_broker_rejects_own_globally_numbered_interface():
    broker = h.PublicHttpBroker(resolver=lambda *_: ['8.8.8.8'],
        sender=lambda *_: pytest.fail('own interface contacted'), interfaces=lambda: ['8.8.8.8'])
    with pytest.raises(h.HttpPolicyError):
        broker.request('GET', 'http://example.com')


@pytest.mark.parametrize('peer_uid,allowed,payload_present', [(1234, True, True), (4321, False, True), (1234, True, False)])
def test_unix_peer_uid_boundary(monkeypatch, peer_uid, allowed, payload_present):
    seen = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def getsockopt(self, level, option, length):
            assert (level, option, length) == (socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            return h.struct.pack('3i', 456, peer_uid, 987)
        def settimeout(self, timeout): pass
        def recv(self, limit):
            if payload_present and not hasattr(self, 'read'):
                self.read = True
                return b'{"method":"GET","url":"https://example.com"}\n'
            return b''
        def sendall(self, data):
            seen.append(('ready', data) if data == h.READY else ('response', json.loads(data)))
    class Server:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def bind(self, path): assert path == '/fixed/test.sock'
        def listen(self, number): pass
        def accept(self):
            if hasattr(self, 'accepted'):
                raise KeyboardInterrupt
            self.accepted = True
            return Connection(), None
    monkeypatch.setattr(socket, 'socket', lambda *args: Server())
    monkeypatch.setattr(h.os, 'chmod', lambda path, mode: seen.append(('mode', mode)))
    monkeypatch.setattr(h, 'handle_request', lambda payload: seen.append(('handled', payload)) or {'status': 200})
    with pytest.raises(KeyboardInterrupt):
        h.serve('/fixed/test.sock', 1234)
    assert ('mode', 0o660) in seen
    # A queued client that gave up before READY leaves no request to execute.
    assert any(x[0] == 'handled' for x in seen) is (allowed and payload_present)
    assert any(x[0] == 'ready' for x in seen) is allowed
    assert any(x[0] == 'response' for x in seen) is allowed


@pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE'])
def test_mutating_methods_dryrun_and_uncertain_timeout(method, monkeypatch):
    def forbidden(*args):
        pytest.fail('validation performed IO')
    broker = h.PublicHttpBroker(resolver=forbidden, sender=forbidden, interfaces=forbidden)
    payload = {'method': method, 'url': 'https://example.com/resource',
               'body_base64': base64.b64encode(b'data').decode(),
               'operation_id': 'trusted:1', 'dry_run': True}
    result = h.handle_request(payload, broker)
    assert result['dry_run'] and result['outcome'] == 'completed'
    sends = []
    def timed_out(*args):
        sends.append(args[0].method)
        time.sleep(1)
    broker = h.PublicHttpBroker(resolver=lambda *_: ['8.8.8.8'], sender=timed_out, interfaces=lambda: [])
    monkeypatch.setattr(h, 'TIMEOUT', 0.01)
    payload['dry_run'] = False
    result = h.handle_request(payload, broker)
    assert result['outcome'] == 'unknown'
    assert result['operation_id'] == 'trusted:1'
    assert sends == [method]  # No automatic retries after timeout.


@pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE'])
def test_alarm_during_post_send_cleanup_is_unknown(monkeypatch, method):
    sent, timers, handlers = [], [], []
    def timer(kind, value):
        timers.append(value)
        if value == 0 and timers.count(0) == 1:
            raise TimeoutError('pending alarm during cleanup')
    monkeypatch.setattr(h.signal, 'setitimer', timer)
    monkeypatch.setattr(h.signal, 'getitimer', lambda *_: (0, 0))
    previous = h.signal.SIG_DFL
    monkeypatch.setattr(h.signal, 'getsignal', lambda *_: previous)
    monkeypatch.setattr(h.signal, 'signal', lambda sig, handler: handlers.append(handler))
    def sender(*args):
        sent.append(method)
        return {'status': 200, 'headers': [], 'body_base64': ''}
    broker = h.PublicHttpBroker(resolver=lambda *_: ['8.8.8.8'], sender=sender, interfaces=lambda: [])
    result = h.handle_request({'method': method, 'url': 'https://example.com',
                              'operation_id': 'trusted:1'}, broker)
    assert sent == [method]
    assert result['outcome'] == 'unknown'
    assert result['operation_id'] == 'trusted:1'
    assert timers == [h.TIMEOUT, 0, 0]
    assert handlers[-1] == previous


@pytest.mark.parametrize('admission', [b'', b'WRONG\n', TimeoutError('queue timeout')])
def test_no_payload_before_http_admission(monkeypatch, admission):
    sent = []
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def connect(self, path): pass
        def recv(self, size):
            if isinstance(admission, Exception):
                raise admission
            return admission
        def sendall(self, data): sent.append(data)
    monkeypatch.setattr(socket, 'socket', lambda *args: Conn())
    with pytest.raises(h.BrokerError) as exc:
        h.call_broker({'method': 'DELETE', 'url': 'https://example.com', 'operation_id': 'trusted:1'})
    assert exc.value.outcome == 'failed_before_send'
    assert exc.value.operation_id == 'trusted:1'
    assert sent == []


def test_http_client_admission_then_payload_and_distinct_budget(monkeypatch):
    events, timeouts = [], []
    response = {'status': 204, 'headers': [], 'body_base64': '',
                'operation_id': 'trusted:1', 'outcome': 'completed'}
    class Conn:
        reads = [b'RE', b'ADY\n', json.dumps(response).encode(), b'']
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): timeouts.append(value)
        def connect(self, path): pass
        def recv(self, size):
            data = self.reads.pop(0)
            events.append(('recv', data))
            return data
        def sendall(self, data): events.append(('send', json.loads(data)))
        def shutdown(self, mode): pass
    monkeypatch.setattr(socket, 'socket', lambda *args: Conn())
    result = h.call_broker({'method': 'PUT', 'url': 'https://example.com', 'operation_id': 'trusted:1'})
    assert result == response
    assert [event[0] for event in events] == ['recv', 'recv', 'send', 'recv', 'recv']
    assert timeouts[0] == h.ADMISSION_TIMEOUT
    assert h.TIMEOUT + 2 in timeouts


def test_http_client_failure_after_admission_is_unknown(monkeypatch):
    sent = []
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def connect(self, path): pass
        def recv(self, size): return h.READY
        def sendall(self, data):
            sent.append(data)
            raise TimeoutError('partial payload write')
    monkeypatch.setattr(socket, 'socket', lambda *args: Conn())
    with pytest.raises(h.BrokerError) as exc:
        h.call_broker({'method': 'POST', 'url': 'https://example.com', 'operation_id': 'trusted:1'})
    assert len(sent) == 1
    assert exc.value.outcome == 'unknown'


def test_late_ready_never_transmits_operation(monkeypatch):
    now, sent = [0.0], []
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def connect(self, path): pass
        def recv(self, size):
            now[0] = h.ADMISSION_TIMEOUT + 0.01
            return h.READY
        def sendall(self, data): sent.append(data)
    monkeypatch.setattr(h.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(socket, 'socket', lambda *args: Conn())
    with pytest.raises(h.BrokerError) as exc:
        h.call_broker({'method': 'DELETE', 'url': 'https://example.com', 'operation_id': 'trusted:1'})
    assert exc.value.outcome == 'failed_before_send'
    assert not sent
