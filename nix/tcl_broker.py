"""Socket-activated, unprivileged Safe Tcl broker; only the bot UID may call it."""
import json
import os
import pwd
import socket
import struct
import time

MAX_REQUEST = 256 * 1024
MAX_RESPONSE = 64 * 1024


def serve_connection(conn, allowed_uid, execute):
    _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != allowed_uid:
        return
    # Admission is sent only by the serial accept loop when execution is free.
    # Clients must not enqueue a script before this handshake. A restarted bot
    # timing out behind an old invocation therefore leaves no deferred effects.
    conn.settimeout(2)
    conn.sendall(b'READY\n')
    deadline = time.monotonic() + 3
    data = bytearray()
    while len(data) <= MAX_REQUEST:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('request deadline exceeded')
        conn.settimeout(remaining)
        chunk = conn.recv(min(8192, MAX_REQUEST + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if b'\n' in chunk:
            break
    try:
        if time.monotonic() >= deadline:
            raise ValueError('request deadline exceeded')
        if len(data) > MAX_REQUEST or not data.endswith(b'\n') or data.count(b'\n') != 1:
            raise ValueError('invalid framing')
        request = json.loads(data)
        # The trusted worker validates schema and separately bounds its child.
        result = execute(request)
        response = json.dumps(result, ensure_ascii=False).encode('utf-8') + b'\n'
        if len(response) > MAX_RESPONSE:
            raise ValueError('oversized result')
    except (ValueError, TypeError, RuntimeError, UnicodeError, RecursionError) as exc:
        response = json.dumps({"error": "script execution rejected or failed",
                               "effects": getattr(exc, "effects", [])}).encode() + b"\n"
        if len(response) > MAX_RESPONSE:
            response = b'{"error":"script execution rejected; effect details unavailable","effects":[]}\n'
    conn.settimeout(2)
    conn.sendall(response)


def main():
    from limnoria_mcp.tcl_worker import run_limited
    if os.environ.pop('LISTEN_FDS', '') != '1' or os.environ.pop('LISTEN_PID', '') != str(os.getpid()):
        raise SystemExit('exactly one systemd-activated socket required')
    allowed_uid = pwd.getpwnam('supybot').pw_uid
    # fd 3 is provided by systemd, not a caller-supplied path. The worker UID
    # cannot create/replace the root-owned socket or access bot credentials.
    with socket.socket(fileno=3) as listener:
        while True:
            conn, _ = listener.accept()
            with conn:
                try:
                    serve_connection(conn, allowed_uid, run_limited)
                except OSError:
                    pass


if __name__ == '__main__':
    main()
