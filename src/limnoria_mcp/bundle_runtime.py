"""Trusted bot-side candidate watcher; never evaluates Nix or executes build hooks."""
import json
import os
import stat
import threading
from pathlib import Path

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024  # JSON escaping of bounded 1 MiB source


def read_last_good(path):
    from .bundle_update import validate_bundle_document
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Invalid last-good file')
        data = source.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError('Last-good document too large')
    return validate_bundle_document(json.loads(data))


def persist_last_good(path, revision, candidates):
    # This parent directory is exclusively bot-owned, never an agent/updater path.
    import secrets
    target = Path(path)
    if not target.is_absolute():
        raise ValueError('Last-good path must be absolute')
    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    name = '.last-good-' + secrets.token_hex(12)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as output:
            data = json.dumps({'revision':revision, 'scripts':candidates}, ensure_ascii=True).encode()
            if len(data) > MAX_DOCUMENT_BYTES:
                raise ValueError('Last-good document too large')
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, target.name, src_dir_fd=directory, dst_dir_fd=directory)
        try:
            os.fsync(directory)
        except OSError:
            # Rename is the commit point. Do not report rejection while the
            # journal already contains the new generation; match live state and
            # explicitly disclose uncertain crash durability instead.
            return 'Bundle committed, but directory fsync failed; crash durability is uncertain'
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


class BundleWatcher:
    def __init__(self, runtime, root, last_good, interval=15):
        if not runtime.gitops or not os.path.isabs(root) or not os.path.isabs(last_good):
            raise ValueError('GitOps requires absolute candidate/private last-good paths')
        if not 5 <= interval <= 3600:
            raise ValueError('Invalid bundle poll interval')
        self.runtime, self.root, self.last_good = runtime, root, last_good
        self.interval = interval
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='limnoria-tcl-bundles', daemon=True)

    def restore(self):
        try:
            revision, candidates = read_last_good(self.last_good)
        except FileNotFoundError:
            return
        self.runtime.activate_bundle(candidates, revision)

    def check(self):
        from .bundle_update import load_current_bundle
        revision, candidates = load_current_bundle(self.root)
        if self.runtime.bundle_status()['revision'] == revision:
            return
        self.runtime.activate_bundle(candidates, revision,
            persist=lambda: persist_last_good(self.last_good, revision, candidates))

    def run(self):
        try:
            self.restore()
        except Exception:
            self.runtime.bundle_error = 'Last-good restoration failed; no scripts activated'
        while not self.stop.is_set():
            try:
                self.check()
            except Exception:
                self.runtime.bundle_error = 'Candidate rejected; previous active bundle retained'
            self.stop.wait(self.interval)
