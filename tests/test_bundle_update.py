import fcntl
import json
import os
from pathlib import Path

import pytest

from limnoria_mcp import bundle_update as update

SHA = 'a' * 40


def builder(repository, system, home):
    assert Path(home).is_dir()
    return SHA, {'hello': 'reply hello'}


def test_publish_and_load(tmp_path):
    result = update.publish_candidate(tmp_path, 'unused', 'unused', build=builder)
    assert result['status'] == 'candidate-published'  # Not an activation ACK.
    assert update.load_current_bundle(tmp_path) == (SHA, {'hello': 'reply hello'})
    assert (tmp_path / 'candidate.json').stat().st_mode & 0o777 == 0o640


def test_failure_preserves_candidate(tmp_path):
    update.publish_candidate(tmp_path, '', '', build=builder)
    before = (tmp_path / 'candidate.json').read_bytes()
    def broken(*args):
        raise RuntimeError('failed build')
    with pytest.raises(RuntimeError):
        update.publish_candidate(tmp_path, '', '', build=broken)
    assert (tmp_path / 'candidate.json').read_bytes() == before
    with pytest.raises(ValueError):
        update.publish_candidate(tmp_path, '', '', build=lambda *args: (SHA, {'../bad': 'x'}))
    assert (tmp_path / 'candidate.json').read_bytes() == before


def test_overlapping_updates_rejected_before_build(tmp_path):
    with (tmp_path / 'update.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            update.publish_candidate(tmp_path, '', '', build=lambda *args: pytest.fail('ran build'))


@pytest.mark.parametrize('source', [None, '\x00', 'x' * (update.MAX_SCRIPT + 1)])
def test_source_validation(source):
    with pytest.raises(ValueError):
        update.validate_document({'revision': SHA, 'scripts': {'hello': source}})


@pytest.mark.parametrize('doc', [
    {}, {'revision': SHA, 'scripts': {}},
    {'revision': 'main', 'scripts': {'hello': 'x'}},
    {'revision': SHA, 'scripts': {'hello': 'x'}, 'hook': 'run'},
    {'revision': SHA, 'scripts': {'hello.py': 'x'}},
    {'revision': SHA, 'scripts': {f'x{i}': 'x' for i in range(65)}},
    {'revision': SHA, 'scripts': {f'x{i}': 'x' * 65536 for i in range(17)}},
])
def test_document_validation(doc):
    with pytest.raises(ValueError):
        update.validate_document(doc)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'directory', 'fifo'])
def test_candidate_nonregular_rejected(tmp_path, kind):
    candidate = tmp_path / 'candidate.json'
    if kind in ('symlink', 'hardlink'):
        target = tmp_path / 'target'
        target.write_text(json.dumps({'revision': SHA, 'scripts': {'x': 'x'}}))
        (candidate.symlink_to(target) if kind == 'symlink' else os.link(target, candidate))
    elif kind == 'directory':
        candidate.mkdir()
    else:
        os.mkfifo(candidate)
    with pytest.raises((ValueError, OSError)):
        update.load_current_bundle(tmp_path)


def test_duplicate_json_keys_rejected(tmp_path):
    (tmp_path / 'candidate.json').write_text(
        '{"revision":"' + SHA + '","scripts":{"x":"a","x":"b"}}')
    with pytest.raises(ValueError, match='Duplicate'):
        update.load_current_bundle(tmp_path)


def test_output_is_flat_tcl_only(tmp_path):
    (tmp_path / 'hello.tcl').write_text('reply hi')
    assert update.read_build_output(tmp_path) == {'hello': 'reply hi'}
    (tmp_path / 'activate').write_text('ignored?')
    with pytest.raises(ValueError):
        update.read_build_output(tmp_path)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'directory', 'fifo'])
def test_output_nonregular_rejected(tmp_path, kind):
    entry = tmp_path / 'evil.tcl'
    if kind in ('symlink', 'hardlink'):
        target = tmp_path.parent / (tmp_path.name + '-target')
        target.write_text('reply x')
        (entry.symlink_to(target) if kind == 'symlink' else os.link(target, entry))
    elif kind == 'directory':
        entry.mkdir()
    else:
        os.mkfifo(entry)
    with pytest.raises((ValueError, OSError)):
        update.read_build_output(tmp_path)


def test_exact_sha_fixed_output_clean_config(monkeypatch, tmp_path):
    monkeypatch.setenv('GITHUB_TOKEN', 'must-not-pass')
    monkeypatch.setenv('NIX_CONFIG', 'accept-flake-config = true')
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    calls = []
    def run(argv, env, timeout=600):
        calls.append(argv)
        assert 'GITHUB_TOKEN' not in env and 'GIT_CONFIG_COUNT' not in env
        assert env['NIX_CONFIG'] == '' and env['NIX_USER_CONF_FILES'] == '/dev/null'
        if argv[0] == 'git':
            return SHA + '\trefs/heads/main\n'
        return json.dumps([{'outputs': {'out': '/nix/store/' + 'a' * 32 + '-bundle'}}])
    monkeypatch.setattr(update, 'read_build_output', lambda path: {'hello': 'reply hi'})
    revision, _ = update.build_main('https://github.com/example/repo.git', 'x86_64-linux', str(tmp_path), run)
    assert revision == SHA
    assert calls[0][-1] == 'refs/heads/main'
    command = calls[1]
    assert command[-1] == f'github:example/repo/{SHA}#packages.x86_64-linux.scriptBundle'
    for flag in ['--no-accept-flake-config', '--no-update-lock-file', '--no-write-lock-file',
                 '--no-allow-import-from-derivation', '--no-allow-unsafe-native-code-during-evaluation']:
        assert flag in command
    for flag, value in [('--max-jobs', '1'), ('--cores', '1'), ('--timeout', '600')]:
        assert command[command.index(flag) + 1] == value
    assert not any('nixosConfigurations' in arg or '#apps' in arg for arg in command)


@pytest.mark.parametrize('repository', ['file:///tmp/repo', 'https://user:pass@github.com/a/b',
    'https://github.com/a/b#apps.x', 'https://github.com/a/b?ref=evil', 'https://evil.example/a/b',
    'git@github.com:a/b', 'https://github.com/a/b/main'])
def test_untrusted_repo_rejected(repository):
    with pytest.raises(ValueError):
        update.build_main(repository, 'x86_64-linux', '/tmp', lambda *a, **kw: pytest.fail('command'))


@pytest.mark.parametrize('resolution', ['', 'main refs/heads/main', SHA + ' refs/heads/evil',
    SHA + ' refs/heads/main\n' + SHA + ' refs/heads/main'])
def test_bad_main_resolution_rejected(resolution):
    with pytest.raises(ValueError):
        update.build_main('https://github.com/a/b', 'x86_64-linux', '/tmp',
                          lambda *a, **kw: resolution)


def test_atomic_replace_failure_leaves_old(tmp_path, monkeypatch):
    update.publish_candidate(tmp_path, '', '', build=builder)
    old = (tmp_path / 'candidate.json').read_bytes()
    def fail(*args, **kwargs):
        raise OSError('injected replace failure')
    monkeypatch.setattr(update.os, 'replace', fail)
    with pytest.raises(OSError):
        update.publish_candidate(tmp_path, '', '', build=lambda *args: ('b' * 40, {'new': 'x'}))
    assert (tmp_path / 'candidate.json').read_bytes() == old
    assert not list(tmp_path.glob('.candidate-*'))


def test_command_runner_bounded_and_reaped():
    import sys
    environment = update.clean_environment('/tmp')
    assert update._run([sys.executable, '-c', 'print("ok")'], environment, timeout=2) == 'ok\n'
    for stream in ('stdout', 'stderr'):
        with pytest.raises(ValueError, match='output exceeded'):
            update._run([sys.executable, '-c',
                f'import sys; sys.{stream}.write("x" * 1048576)'], environment, timeout=2)
    with pytest.raises(TimeoutError):
        update._run([sys.executable, '-c', 'import time; time.sleep(10)'], environment, timeout=0.05)


@pytest.fixture
def optimized_store(tmp_path, monkeypatch):
    """Synthetic root-owned metadata; no actual /nix access or chown privilege."""
    import stat
    from types import SimpleNamespace
    nix = tmp_path / 'nix'
    root = nix / 'store' / ('a' * 32 + '-bundle')
    root.mkdir(parents=True)
    outside = tmp_path / 'optimized-link'
    outside.write_text('reply optimized')
    os.link(outside, root / 'hello.tcl')
    outside.chmod(0o444)
    root.chmod(0o555)
    actual_open, actual_fstat = os.open, os.fstat
    def mapped_open(path, *args, **kwargs):
        return actual_open(nix if path == '/nix' else path, *args, **kwargs)
    def root_metadata(fd):
        info = actual_fstat(fd)
        return SimpleNamespace(st_uid=0, st_mode=info.st_mode, st_nlink=info.st_nlink,
                               st_size=info.st_size)
    monkeypatch.setattr(update.os, 'open', mapped_open)
    monkeypatch.setattr(update.os, 'fstat', root_metadata)
    yield root, '/nix/store/' + root.name, actual_fstat
    root.chmod(0o755)


def test_optimized_store_hardlinks_accepted(optimized_store):
    root, store_path, _ = optimized_store
    assert update.read_build_output(store_path) == {'hello': 'reply optimized'}
    # Identical metadata at a non-store path does not earn a hardlink exception.
    with pytest.raises(ValueError):
        update.read_build_output(root)


@pytest.mark.parametrize('target', ['file', 'directory'])
def test_mutable_store_rejected(optimized_store, target):
    root, store_path, _ = optimized_store
    (root / 'hello.tcl' if target == 'file' else root).chmod(0o755)
    with pytest.raises(ValueError):
        update.read_build_output(store_path)


def test_nonroot_store_rejected(optimized_store, monkeypatch):
    _, store_path, real_fstat = optimized_store
    from types import SimpleNamespace
    def nonroot_metadata(fd):
        info = real_fstat(fd)
        return SimpleNamespace(st_uid=1000, st_mode=info.st_mode,
                               st_nlink=info.st_nlink, st_size=info.st_size)
    monkeypatch.setattr(update.os, 'fstat', nonroot_metadata)
    with pytest.raises(ValueError, match='root-owned'):
        update.read_build_output(store_path)


def test_store_symlink_rejected(optimized_store):
    root, store_path, _ = optimized_store
    root.chmod(0o755)
    (root / 'hello.tcl').unlink()
    (root / 'hello.tcl').symlink_to(root.parent.parent.parent / 'optimized-link')
    root.chmod(0o555)
    with pytest.raises(OSError):
        update.read_build_output(store_path)


def test_store_candidate_still_rejects_hardlinks(optimized_store):
    root, _, _ = optimized_store
    root.chmod(0o755)
    os.link(root / 'hello.tcl', root / 'candidate.json')
    with pytest.raises(ValueError):
        update.load_current_bundle(root)
