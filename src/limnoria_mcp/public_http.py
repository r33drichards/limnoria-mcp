"""Public-only HTTP capability. No proxy, netrc, cookies, redirects, or retries.

Only explicitly supplied request data is transmitted. This includes private data
scripts copy from their context/state: public egress is not a data-loss filter.
The broker must run as a separate, secret-free UID, not as the Tcl worker/bot.
"""
from __future__ import annotations

import base64
import ctypes
import http.client
import ipaddress
import json
import os
import re
import signal
import socket
import ssl
import stat
import struct
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

SOCKET_PATH = '/run/limnoria-http-broker/http.sock'
MAX_BODY = 64 * 1024
MAX_RESPONSE = 256 * 1024
MAX_WIRE = 512 * 1024
TIMEOUT = 8
ADMISSION_TIMEOUT = 2
READY = b'READY\n'
# Packaging may replace this with an immutable system CA bundle path. Never read
# trust overrides from the environment or IPC request.
PUBLIC_CA_FILE = None
ALLOWED_HEADERS = frozenset({'accept', 'content-type', 'user-agent', 'authorization'})
# Explicitly reject translation/transition spaces, even on Python versions whose
# is_global classification differs. Azure WireServer is globally numbered.
DENIED = tuple(ipaddress.ip_network(x) for x in (
    '168.63.129.16/32', '100.64.0.0/10', '192.0.0.0/24', '192.88.99.0/24',
    '64:ff9b::/96', '64:ff9b:1::/48', '2001::/23', '2002::/16', '::/96',
    '::ffff:0:0/96'))


class HttpPolicyError(ValueError):
    pass


class BrokerError(HttpPolicyError):
    def __init__(self, message, *, operation_id=None, outcome='unknown'):
        super().__init__(message)
        self.operation_id = operation_id
        self.outcome = outcome


def public_address(value, own_addresses=()):
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise HttpPolicyError('Invalid destination address') from exc
    if (not address.is_global or address.is_multicast or address.is_reserved
            or address.is_unspecified or address.is_loopback or address.is_link_local
            or getattr(address, 'is_site_local', False)
            or any(address.version == net.version and address in net for net in DENIED)
            or address in {ipaddress.ip_address(x) for x in own_addresses}):
        raise HttpPolicyError('Destination is not permitted public Internet')
    return address


def local_addresses():
    """Enumerate actual interfaces without DNS or IP traffic (Linux getifaddrs)."""
    class Ifaddrs(ctypes.Structure):
        pass
    Ifaddrs._fields_ = [('next', ctypes.POINTER(Ifaddrs)), ('name', ctypes.c_char_p),
                       ('flags', ctypes.c_uint), ('addr', ctypes.c_void_p),
                       ('netmask', ctypes.c_void_p), ('ifu', ctypes.c_void_p),
                       ('data', ctypes.c_void_p)]
    libc = ctypes.CDLL(None, use_errno=True)
    head = ctypes.POINTER(Ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        raise HttpPolicyError('Unable to enumerate local interfaces')
    result = set()
    try:
        node = head
        while node:
            addr = node.contents.addr
            if addr:
                family = ctypes.c_ushort.from_address(addr).value
                if family == socket.AF_INET:
                    result.add(socket.inet_ntop(family, ctypes.string_at(addr + 4, 4)))
                elif family == socket.AF_INET6:
                    result.add(socket.inet_ntop(family, ctypes.string_at(addr + 8, 16)))
            node = node.contents.next
    finally:
        libc.freeifaddrs(head)
    return result


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    host: str
    port: int
    tls: bool
    target: str
    headers: dict
    body: bytes


def validate_request(method, url, headers=None, body=b''):
    if method not in ('GET', 'POST', 'PUT', 'DELETE') or not isinstance(url, str) or len(url) > 4096:
        raise HttpPolicyError('Only bounded GET, POST, PUT and DELETE requests are supported')
    if not url.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in url) or '\\' in url:
        raise HttpPolicyError('URL must be ASCII without whitespace or control characters')
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise HttpPolicyError('Malformed URL') from exc
    if parsed.scheme not in ('http', 'https') or not host or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise HttpPolicyError('HTTP(S) URL without credentials or fragment required')
    if '%' in host or host.endswith('.') or len(host) > 253:
        raise HttpPolicyError('Invalid destination host')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host) or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in host.split('.')):
            raise HttpPolicyError('Invalid destination hostname')
    if port is not None and not 1 <= port <= 65535:
        raise HttpPolicyError('Invalid destination port')
    if not isinstance(body, bytes) or len(body) > MAX_BODY or (method == 'GET' and body):
        raise HttpPolicyError('Invalid or oversized request body')
    if headers is None:
        headers = {}
    if not isinstance(headers, dict) or len(headers) > 8:
        raise HttpPolicyError('Invalid headers')
    clean = {}
    for key, value in headers.items():
        if not isinstance(key, str) or key.lower() not in ALLOWED_HEADERS or key.lower() in clean:
            raise HttpPolicyError('Unsupported or duplicate request header')
        if not isinstance(value, str) or not value.isascii() or len(value) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise HttpPolicyError('Invalid header value')
        clean[key.lower()] = value
    return Request(method, url, host, port or (443 if parsed.scheme == 'https' else 80),
                   parsed.scheme == 'https', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''), clean, body)


def resolve(host, port):
    return [entry[4][0] for entry in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


class _HeaderReader:
    def __init__(self, stream):
        self.stream, self.remaining, self.enabled = stream, 16384, True

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def readline(self, size=-1):
        if not self.enabled:
            return self.stream.readline(size)
        limit = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        data = self.stream.readline(limit)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise HttpPolicyError('Response headers exceed limit')
        return data


class _BoundedResponse(http.client.HTTPResponse):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fp = _HeaderReader(self.fp)

    def begin(self):
        super().begin()
        self.fp.enabled = False


def transport(request, address, deadline):
    """Connect once to the validated numeric IP; SNI/certificate use original host."""
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    raw = socket.socket(family, socket.SOCK_STREAM)
    conn = http.client.HTTPConnection(request.host, request.port)
    conn.response_class = _BoundedResponse
    try:
        raw.settimeout(max(0.001, deadline - time.monotonic()))
        raw.connect((str(address), request.port))
        if request.tls:
            # Explicit system CA context, ignoring SSL_CERT_FILE/SSL_CERT_DIR.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(
                cafile=PUBLIC_CA_FILE or ssl.get_default_verify_paths().openssl_cafile,
                capath=None if PUBLIC_CA_FILE else ssl.get_default_verify_paths().openssl_capath)
            raw = context.wrap_socket(raw, server_hostname=request.host)
        conn.sock = raw
        conn.request(request.method, request.target, body=request.body,
                     headers={**request.headers, 'Accept-Encoding': 'identity', 'Connection': 'close'})
        response = conn.getresponse()
        headers = response.getheaders()
        if sum(len(k) + len(v) for k, v in headers) > 16384:
            raise HttpPolicyError('Response headers exceed limit')
        if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
            raise HttpPolicyError('Encoded responses are not supported')
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise HttpPolicyError('Response body exceeds limit')
        if getattr(response, 'length', None) not in (None, 0):
            raise HttpPolicyError('Truncated HTTP response')
        # Redirects are returned, never followed, including for POST.
        return {'status': response.status, 'headers': headers,
                'body_base64': base64.b64encode(data).decode('ascii')}
    finally:
        conn.close()
        raw.close()


class PublicHttpBroker:
    def __init__(self, *, resolver=resolve, sender=transport, interfaces=local_addresses):
        self.resolver, self.sender, self.interfaces = resolver, sender, interfaces

    def request(self, method, url, headers=None, body=b'', *, dry_run=False):
        request = validate_request(method, url, headers, body)
        if dry_run:
            return {'status': 0, 'headers': [], 'body_base64': '', 'dry_run': True}
        # This synchronous service is single-threaded. SIGALRM bounds DNS lookup,
        # TLS, headers and slow-drip bodies, not merely individual socket reads.
        if threading.current_thread() is not threading.main_thread():
            raise HttpPolicyError('HTTP broker must run in its dedicated process')
        previous = signal.getsignal(signal.SIGALRM)
        if signal.getitimer(signal.ITIMER_REAL)[0]:
            raise HttpPolicyError('HTTP deadline timer already in use')
        def expired(*_):
            raise TimeoutError('Public HTTP request exceeded deadline; mutating request may have been delivered')
        dispatch_started = False
        try:
            signal.signal(signal.SIGALRM, expired)
            try:
                signal.setitimer(signal.ITIMER_REAL, TIMEOUT)
                own = self.interfaces()
                answers = self.resolver(request.host, request.port)
                if not answers or len(answers) > 64:
                    raise HttpPolicyError('Missing or excessive DNS answers')
                # Reject the entire RRset if ANY address fails policy. Connect to
                # one validated answer, never resolve again or fall back/retry.
                addresses = [public_address(answer, own) for answer in answers]
                dispatch_started = True
                return self.sender(request, addresses[0], time.monotonic() + TIMEOUT)
            finally:
                try:
                    try:
                        signal.setitimer(signal.ITIMER_REAL, 0)
                    except Exception:
                        # A pending alarm can interrupt cleanup itself. Retry
                        # disabling it, but do not discard the original failure.
                        signal.setitimer(signal.ITIMER_REAL, 0)
                        raise
                finally:
                    signal.signal(signal.SIGALRM, previous)
        except Exception as exc:
            # The phase covers both transport AND cleanup: a deadline delivered
            # after sender returns must never claim the request was not sent.
            if dispatch_started:
                raise BrokerError('Public HTTP request failed; delivery may be uncertain',
                                  outcome='unknown') from exc
            raise


def handle_request(payload, broker=None):
    import uuid
    operation_id = payload.get('operation_id') if isinstance(payload, dict) else None
    if operation_id is None:
        operation_id = uuid.uuid4().hex
    if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,96}', operation_id):
        return {'error': 'Invalid operation identifier', 'outcome': 'failed_before_send', 'operation_id': None}
    try:
        if not isinstance(payload, dict) or set(payload) - {'method', 'url', 'headers', 'body_base64', 'dry_run', 'operation_id'}:
            raise HttpPolicyError('Invalid broker request')
        if not isinstance(payload.get('dry_run', False), bool):
            raise HttpPolicyError('Invalid dry_run flag')
        try:
            body = base64.b64decode(payload.get('body_base64', ''), validate=True)
        except (ValueError, TypeError) as exc:
            raise HttpPolicyError('Invalid base64 request body') from exc
        result = (broker or PublicHttpBroker()).request(payload.get('method'), payload.get('url'),
            payload.get('headers'), body, dry_run=payload.get('dry_run', False))
        return {**result, 'operation_id': operation_id, 'outcome': 'completed'}
    except Exception as exc:
        return {'error': 'Public HTTP request failed: ' + type(exc).__name__,
                'operation_id': operation_id,
                'outcome': exc.outcome if isinstance(exc, BrokerError) else 'failed_before_send'}


def _call_broker(payload, socket_path=SOCKET_PATH):
    data = json.dumps(payload, ensure_ascii=True).encode() + b'\n'
    if len(data) > MAX_WIRE:
        raise BrokerError('Broker request exceeds limit', operation_id=payload.get('operation_id'), outcome='failed_before_send')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        try:
            admission_deadline = time.monotonic() + ADMISSION_TIMEOUT
            conn.settimeout(ADMISSION_TIMEOUT)
            conn.connect(socket_path)
            ready = bytearray()
            while len(ready) < len(READY):
                remaining = admission_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Broker admission deadline exceeded')
                conn.settimeout(remaining)
                part = conn.recv(len(READY) - len(ready))
                if not part:
                    raise HttpPolicyError('Broker closed before admission')
                ready.extend(part)
            if time.monotonic() >= admission_deadline:
                raise TimeoutError('Broker admission deadline exceeded')
            if bytes(ready) != READY:
                raise HttpPolicyError('Invalid broker admission response')
        except Exception as exc:
            raise BrokerError('Public HTTP broker did not admit request',
                              operation_id=payload.get('operation_id'), outcome='failed_before_send') from exc
        # No operation bytes are sent until the sequential broker is ready.
        # Queue time cannot eat into this distinct active-request budget.
        request_deadline = time.monotonic() + TIMEOUT + 2
        conn.settimeout(TIMEOUT + 2)
        conn.sendall(data)
        conn.shutdown(socket.SHUT_WR)
        result = bytearray()
        while len(result) <= MAX_WIRE:
            remaining = request_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Broker response deadline exceeded')
            conn.settimeout(remaining)
            chunk = conn.recv(8192)
            if not chunk:
                break
            result.extend(chunk)
    if len(result) > MAX_WIRE:
        raise HttpPolicyError('Broker response exceeds limit')
    response = json.loads(result)
    if not isinstance(response, dict) or response.get('outcome') not in {'completed', 'failed_before_send', 'unknown'}:
        raise HttpPolicyError('Invalid broker response')
    if payload.get('operation_id') is not None and response.get('operation_id') != payload['operation_id']:
        raise HttpPolicyError('Broker operation identifier mismatch')
    if 'error' in response:
        raise BrokerError(response['error'], operation_id=response.get('operation_id'),
                          outcome=response.get('outcome', 'unknown'))
    return response


def call_broker(payload, socket_path=SOCKET_PATH):
    try:
        return _call_broker(payload, socket_path)
    except BrokerError:
        raise
    except Exception as exc:
        raise BrokerError('Public HTTP IPC failed; delivery may be uncertain',
                          operation_id=payload.get('operation_id'), outcome='unknown') from exc


def serve(socket_path, allowed_uid):
    """Fixed socket, explicit Linux SO_PEERCRED; no TCP or administrative API."""
    # A private RuntimeDirectory owned by broker must contain this path.
    try:
        existing = os.lstat(socket_path)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
            raise HttpPolicyError('Refusing to replace unexpected broker socket path')
        os.unlink(socket_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(socket_path)
        os.chmod(socket_path, 0o660)
        server.listen(8)
        while True:
            conn, _ = server.accept()
            with conn:
                _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != allowed_uid:
                    continue
                conn.settimeout(2)
                try:
                    conn.sendall(READY)
                    data = bytearray()
                    receive_deadline = time.monotonic() + 2
                    while len(data) <= MAX_WIRE:
                        remaining = receive_deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('IPC receive deadline exceeded')
                        conn.settimeout(remaining)
                        chunk = conn.recv(8192)
                        if not chunk:
                            break
                        data.extend(chunk)
                    if len(data) > MAX_WIRE:
                        raise HttpPolicyError('Broker request exceeds limit')
                    response = handle_request(json.loads(data))
                except Exception as exc:
                    # Never echo URLs, headers or body (may contain credentials).
                    response = {'error': 'Public HTTP request failed: ' + type(exc).__name__ + '; mutating request delivery may be uncertain'}
                try:
                    conn.sendall(json.dumps(response).encode())
                except OSError:
                    pass


def main():
    import argparse
    import pwd
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', default=SOCKET_PATH)
    parser.add_argument('--allowed-user', required=True)
    args = parser.parse_args()
    serve(args.socket, pwd.getpwnam(args.allowed_user).pw_uid)


if __name__ == '__main__':
    main()
