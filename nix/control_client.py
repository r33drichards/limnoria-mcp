"""Unprivileged client: exactly one enum argument, fixed Unix socket."""
import json
import socket
import sys


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"start", "stop", "restart", "status", "logs"}:
        raise SystemExit("expected exactly one service action")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(35)
        conn.connect("/run/limnoria-mcp-control/control.sock")
        conn.sendall(sys.argv[1].encode("ascii") + b"\n")
        chunks = bytearray()
        while len(chunks) <= 400000:
            chunk = conn.recv(8192)
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > 400000:
            raise SystemExit("broker response exceeds limit")
    response = json.loads(chunks)
    if "error" in response:
        raise SystemExit("broker rejected action")
    print(json.dumps(response))


if __name__ == "__main__":
    main()
