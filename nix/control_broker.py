"""Root-only single-unit broker. Installed immutable; never import bot code."""
import json
import os
import pwd
import resource
import socket
import struct
import subprocess
import tempfile

SOCKET = "/run/limnoria-mcp-control/control.sock"
USER = "limnoria-mcp-controller"
SYSTEMCTL = "@systemctl@"
JOURNALCTL = "@journalctl@"
UNIT = "supybot.service"
ACTIONS = frozenset({"start", "stop", "restart", "status", "logs"})
MAX_OUTPUT = 16384


def command(action):
    if action in {"start", "stop", "restart"}:
        return [SYSTEMCTL, "--no-ask-password", action, UNIT]
    if action == "status":
        return [SYSTEMCTL, "--no-pager", "show", "--property=ActiveState,SubState,Result", UNIT]
    if action == "logs":
        return [JOURNALCTL, "--no-pager", "--quiet", "--unit=" + UNIT,
                "--lines=100", "--output=short-iso"]
    raise ValueError("unsupported action")


def limit_output():
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))


def execute(action):
    argv = command(action)
    # A file-size bound avoids unbounded pipe capture even for giant log records.
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.STDOUT, cwd="/", timeout=30,
                                    env={"LANG": "C.UTF-8", "SYSTEMD_PAGER": "cat"},
                                    shell=False, preexec_fn=limit_output, check=False)
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            returncode = 124
        output.seek(0)
        text = output.read(MAX_OUTPUT).decode("utf-8", errors="replace")
    return {"action": action, "returncode": returncode, "stdout": text,
            "stderr": "", "output_limit_bytes": MAX_OUTPUT}


def serve_connection(conn, allowed_uid):
    _, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != allowed_uid:
        return
    conn.settimeout(3)
    data = bytearray()
    while len(data) < 64:
        chunk = conn.recv(64 - len(data))
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in chunk:
            break
    try:
        action = bytes(data).decode("ascii").removesuffix("\n")
        if action not in ACTIONS:
            raise ValueError("unsupported action")
        response = execute(action)
    except (ValueError, UnicodeError):
        response = {"error": "unsupported action"}
    conn.sendall(json.dumps(response).encode() + b"\n")


def main():
    uid = pwd.getpwnam(USER).pw_uid
    os.umask(0o007)
    try:
        os.unlink(SOCKET)
    except FileNotFoundError:
        pass
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(SOCKET)
        os.chmod(SOCKET, 0o660)
        listener.listen(8)
        while True:
            conn, _ = listener.accept()
            with conn:
                try:
                    serve_connection(conn, uid)
                except (OSError, subprocess.SubprocessError):
                    # Never include request data or arbitrary exception text in logs.
                    pass


if __name__ == "__main__":
    main()
