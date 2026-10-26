"""Credential-free, fixed-output Git main bundle staging; never activates host code.

The bot's trusted watcher separately validates Safe Tcl and atomically activates
this candidate. Publication is NOT an activation acknowledgement.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import selectors
import time
import stat
import subprocess
import tempfile

from .files import script_name

MAX_FILES = 64
MAX_SCRIPT = 64 * 1024
MAX_TOTAL = 1024 * 1024
MAX_DOCUMENT = 6 * MAX_TOTAL + 16384
REVISION = re.compile(r'[0-9a-f]{40}\Z')
REPOSITORY = re.compile(r'https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*?)(?:\.git)?\Z')
STORE_OUTPUT = re.compile(r'/nix/store/[a-z0-9]{32}-[^/]+\Z')
SYSTEM = re.compile(r'(?:x86_64|aarch64)-linux\Z')


def validate_document(document):
    if not isinstance(document, dict) or set(document) != {'revision', 'scripts'}:
        raise ValueError('Invalid bundle document')
    revision, scripts = document['revision'], document['scripts']
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise ValueError('Invalid exact Git revision')
    if not isinstance(scripts, dict) or not 1 <= len(scripts) <= MAX_FILES:
        raise ValueError('Bundle requires 1..64 scripts')
    size = 0
    for name, source in scripts.items():
        script_name(name)
        if not isinstance(source, str) or '\x00' in source:
            raise ValueError('Invalid Tcl source')
        length = len(source.encode('utf-8'))
        if length > MAX_SCRIPT:
            raise ValueError('Script exceeds size limit')
        size += length
    if size > MAX_TOTAL:
        raise ValueError('Bundle exceeds total size limit')
    return revision, scripts


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _immutable_metadata(info, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    return kind(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o222


def _read_regular(directory, name, maximum, immutable_store=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > maximum
                or (immutable_store and not _immutable_metadata(info))
                or (info.st_nlink != 1 and not immutable_store)):
            raise ValueError('Expected bounded regular file with safe ownership/link policy')
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError('File exceeds limit')
        return data


def load_current_bundle(bundle_root):
    """Pin the root and one atomic document; never follow candidate symlinks."""
    fd = os.open(bundle_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        raw = _read_regular(fd, 'candidate.json', MAX_DOCUMENT)
        return validate_document(json.loads(raw, object_pairs_hook=_unique_object))
    finally:
        os.close(fd)


def _open_build_output(output):
    output = os.fspath(output)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if not STORE_OUTPUT.fullmatch(output):
        # Local synthetic fixture path: never earns the immutable-store exception.
        return os.open(output, flags), False
    parent = os.open('/nix', flags)
    try:
        store = os.open('store', flags, dir_fd=parent)
        try:
            if os.fstat(parent).st_uid != 0 or os.fstat(store).st_uid != 0:
                raise ValueError('Expected root-owned multi-user Nix store')
            fd = os.open(output.rsplit('/', 1)[1], flags, dir_fd=store)
        finally:
            os.close(store)
    finally:
        os.close(parent)
    if not _immutable_metadata(os.fstat(fd), directory=True):
        os.close(fd)
        raise ValueError('Expected immutable root-owned store output directory')
    return fd, True


def read_build_output(output):
    # Only a pinned, root-owned read-only store directory earns the optimization
    # hardlink exception. Mutable candidates always use the strict default.
    fd, immutable_store = _open_build_output(output)
    try:
        # Scandir enumeration is bounded rather than constructing an unbounded list.
        scripts = {}
        with os.scandir(fd) as entries:
            for entry in entries:
                if len(scripts) >= MAX_FILES or not entry.name.endswith('.tcl'):
                    raise ValueError('Only a bounded flat Tcl bundle is accepted')
                name = script_name(entry.name[:-4])
                scripts[name] = _read_regular(fd, entry.name, MAX_SCRIPT, immutable_store).decode('utf-8')
        validate_document({'revision': '0' * 40, 'scripts': scripts})
        return scripts
    finally:
        os.close(fd)


def _run(argv, env, timeout=600):
    # Drain BOTH pipes concurrently with strict budgets; malicious evaluator logs
    # cannot fill disk or an unbounded Python buffer. Kill/reap on any failure.
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd='/', env=env,
                               start_new_session=True)
    output = bytearray()
    counts = {'stdout': 0, 'stderr': 0}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Bundle command deadline exceeded')
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    counts[key.data] += len(chunk)
                    limit = 64 * 1024 if key.data == 'stdout' else 256 * 1024
                    if counts[key.data] > limit:
                        raise ValueError('Command output exceeded limit')
                    if key.data == 'stdout':
                        output.extend(chunk)
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if code:
            raise RuntimeError('Bundle command failed (exit %s)' % code)
        return output.decode('utf-8')
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        process.stdout.close()
        process.stderr.close()


def clean_environment(home):
    return {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': home,
            'LANG': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
            'GIT_ALLOW_PROTOCOL': 'https', 'NIX_USER_CONF_FILES': '/dev/null',
            'NIX_CONFIG': '', 'NIX_PATH': ''}


def nix_build_argv(installable):
    """Shared fixed flags; production installable is constructed only by build_main."""
    return ['nix', '--extra-experimental-features', 'nix-command flakes',
                  '--no-accept-flake-config', '--no-allow-import-from-derivation',
                  '--no-allow-unsafe-native-code-during-evaluation',
                  '--option', 'access-tokens', '', '--option', 'sandbox', 'true',
                  'build', '--max-jobs', '1', '--cores', '1', '--timeout', '600',
                  '--no-link', '--json', '--no-update-lock-file',
                  '--no-write-lock-file', installable]


def build_main(repository, system, home, run=_run):
    match = REPOSITORY.fullmatch(repository)
    if not match or not SYSTEM.fullmatch(system):
        raise ValueError('Expected public GitHub HTTPS repository and supported Linux system')
    owner, repo = match.groups()
    repository = f'https://github.com/{owner}/{repo}.git'
    env = clean_environment(home)
    resolved = run(['git', 'ls-remote', '--exit-code', '--refs', repository,
                    'refs/heads/main'], env, timeout=60).splitlines()
    if len(resolved) != 1:
        raise ValueError('Expected exactly one main revision')
    fields = resolved[0].split()
    if len(fields) != 2 or fields[1] != 'refs/heads/main' or not REVISION.fullmatch(fields[0]):
        raise ValueError('Invalid main resolution')
    revision = fields[0]
    installable = f'github:{owner}/{repo}/{revision}#packages.{system}.scriptBundle'
    result = run(nix_build_argv(installable), env)
    builds = json.loads(result)
    if (not isinstance(builds, list) or len(builds) != 1
            or not isinstance(builds[0], dict)
            or set(builds[0].get('outputs', {})) != {'out'}):
        raise ValueError('Expected exactly one out output')
    output = builds[0]['outputs']['out']
    if not isinstance(output, str) or not STORE_OUTPUT.fullmatch(output):
        raise ValueError('Expected immutable Nix store output')
    return revision, read_build_output(output)


def publish_candidate(state_directory, repository, system, build=build_main):
    """Serialize resolve/build/publish; failure leaves the previous candidate intact.

    State directory is provisioned by the administrator, updater-owned, and not
    writable by drafts authors. This API intentionally has no activation hook.
    """
    root = os.open(state_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = None
    temporary = None
    try:
        lock = os.open('update.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
                       0o600, dir_fd=root)
        if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_nlink != 1:
            raise ValueError('Invalid update lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix='bundle-build-') as home:
            revision, scripts = build(repository, system, home)
        document = {'revision': revision, 'scripts': scripts}
        validate_document(document)
        raw = json.dumps(document, ensure_ascii=True, sort_keys=True).encode()
        if len(raw) > MAX_DOCUMENT:
            raise ValueError('Document exceeds limit')
        import secrets
        temporary = '.candidate-' + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o640, dir_fd=root)
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o640)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, 'candidate.json', src_dir_fd=root, dst_dir_fd=root)
        temporary = None
        os.fsync(root)
        return {'revision': revision, 'status': 'candidate-published', 'scripts': len(scripts)}
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=root)
        if lock is not None:
            os.close(lock)
        os.close(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--state-directory', required=True)
    parser.add_argument('--system', required=True)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Bundle updater must not run as root')
    print(json.dumps(publish_candidate(args.state_directory, args.repository, args.system)))


if __name__ == '__main__':
    main()

# Shared pure protocol validation for the bot's private last-good journal.
validate_bundle_document = validate_document
MAX_BUNDLE_BYTES = MAX_DOCUMENT


if __name__ == '__main__':
    main()
