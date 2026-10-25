"""Descriptor-relative file access. No symlinks, hardlinks, traversal or special files."""
import os
import re
import stat
from contextlib import contextmanager

MAX_BYTES = 256 * 1024
NAME = re.compile(r"Agent[A-Z][A-Za-z0-9_]{0,48}\Z")
FILES = frozenset({"__init__.py", "plugin.py", "config.py", "README.md"})


def plugin_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise ValueError("Managed plugin names must match Agent[A-Z][A-Za-z0-9_]{0,48}")
    return name


class PluginFiles:
    def __init__(self, root: str):
        if not os.path.isabs(root):
            raise ValueError("Plugin root must be absolute")
        # Administrator chooses root; pin its inode for all subsequent accesses.
        self.root = os.path.realpath(root)
        self.fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def close(self):
        os.close(self.fd)

    @contextmanager
    def directory(self, name: str, create=False):
        plugin_name(name)
        created = False
        if create:
            try:
                os.mkdir(name, mode=0o2770, dir_fd=self.fd)
                created = True
            except FileExistsError:
                pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            if created:
                os.fchmod(fd, 0o2770)
            yield fd
        finally:
            os.close(fd)

    def list(self):
        return sorted(n for n in os.listdir(self.fd) if NAME.fullmatch(n)
                      and stat.S_ISDIR(os.stat(n, dir_fd=self.fd, follow_symlinks=False).st_mode))

    def read(self, name: str, filename: str):
        if filename not in FILES:
            raise ValueError("Unsupported managed plugin filename")
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

    def write(self, name: str, filename: str, content: str):
        if filename not in FILES:
            raise ValueError("Unsupported managed plugin filename")
        data = content.encode("utf-8")
        if len(data) > MAX_BYTES:
            raise ValueError("File exceeds size limit")
        if filename.endswith(".py"):
            compile(content, filename, "exec")  # Syntax only; never execute here.
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
                if filename.endswith('.py'):
                    # Python timestamp caches have one-second granularity. Invalidate
                    # same-size rapid edits without following a planted cache symlink.
                    try:
                        cache = os.open('__pycache__', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                        dir_fd=directory)
                    except FileNotFoundError:
                        cache = None
                    if cache is not None:
                        try:
                            prefix = filename[:-3] + '.'
                            for entry in os.listdir(cache):
                                if entry.startswith(prefix) and entry.endswith('.pyc'):
                                    os.unlink(entry, dir_fd=cache)
                        finally:
                            os.close(cache)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        return {"plugin": name, "file": filename, "bytes": len(data)}
