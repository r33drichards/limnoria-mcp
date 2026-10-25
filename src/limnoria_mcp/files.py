"""Descriptor-relative file access. No symlinks, hardlinks, traversal or special files."""
import os
import re
import stat
from contextlib import contextmanager

MAX_BYTES = 64 * 1024
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,48}\Z")
FILENAME = "script.tcl"


def script_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise ValueError("Script names must match [A-Za-z][A-Za-z0-9_]{0,48}")
    return name


class ScriptFiles:
    def __init__(self, root: str):
        if not os.path.isabs(root):
            raise ValueError("Script root must be absolute")
        # Administrator chooses root; pin its inode for all subsequent accesses.
        self.root = os.path.realpath(root)
        self.fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def close(self):
        os.close(self.fd)

    @contextmanager
    def directory(self, name: str, create=False):
        script_name(name)
        if create:
            try:
                # Inherit the administrator-provisioned parent's SGID/group.
                # Explicit SGID chmod is forbidden by RestrictSUIDSGID and Nix;
                # chmod(0770) would incorrectly clear inherited SGID. Runtime
                # UMask=0007 supplies group access without changing that bit.
                os.mkdir(name, mode=0o770, dir_fd=self.fd)
            except FileExistsError:
                pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            yield fd
        finally:
            os.close(fd)

    def list(self):
        return sorted(n for n in os.listdir(self.fd) if NAME.fullmatch(n)
                      and stat.S_ISDIR(os.stat(n, dir_fd=self.fd, follow_symlinks=False).st_mode))

    def read(self, name: str):
        filename = FILENAME
        with self.directory(name) as directory:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as f:
                info = os.fstat(f.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Only single-link regular files are allowed")
                value = f.read(MAX_BYTES + 1)
                if len(value) > MAX_BYTES:
                    raise ValueError("File exceeds size limit")
                return value.decode("utf-8")

    def write(self, name: str, content: str):
        filename = FILENAME
        data = content.encode("utf-8")
        if len(data) > MAX_BYTES:
            raise ValueError("File exceeds size limit")
        with self.directory(name, create=True) as directory:
            # Replace, don't follow or truncate an existing inode (including hardlinks).
            import secrets
            temporary = ".mcp-" + secrets.token_hex(12)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o660, dir_fd=directory)
            try:
                os.fchmod(fd, 0o660)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        return {"script": name, "bytes": len(data)}
