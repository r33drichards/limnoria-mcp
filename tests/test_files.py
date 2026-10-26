import os
import pytest
from limnoria_mcp.files import ScriptFiles, MAX_BYTES

@pytest.fixture
def files(tmp_path):
    fs = ScriptFiles(str(tmp_path))
    yield fs
    fs.close()


def test_write_read(files):
    files.write('AgentHello', 'answer = 42\n')
    assert files.read('AgentHello') == 'answer = 42\n'
    assert files.list() == ['AgentHello']

@pytest.mark.parametrize('name', ['../Owner', '/etc', '', 'a.b', '123hello', 'AgentHello/../x'])
def test_names(files, name):
    with pytest.raises(ValueError):
        files.write(name, '')

def test_no_python_file_api(files):
    with pytest.raises(TypeError):
        files.write('Hello', 'plugin.py', 'evil python')


def test_links(files, tmp_path):
    outside = tmp_path / 'outside'
    outside.write_text('secret')
    (tmp_path / 'AgentLink').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        files.read('AgentLink')
    (tmp_path / 'AgentHello').mkdir()
    path = tmp_path / 'AgentHello' / 'script.tcl'
    path.symlink_to(outside)
    with pytest.raises(OSError):
        files.read('AgentHello')
    files.write('AgentHello', 'ok=1')
    assert outside.read_text() == 'secret'
    path.unlink()
    os.link(outside, path)
    with pytest.raises(ValueError):
        files.read('AgentHello')
    files.write('AgentHello', 'ok=2')
    assert outside.read_text() == 'secret'


def test_bounds_and_syntax(files):
    with pytest.raises(ValueError):
        files.write('AgentHello', 'x' * (MAX_BYTES + 1))
    # Storage accepts candidates; isolated validation is an explicit later action.
    files.write('AgentHello', 'invalid Tcl {')


def test_group_write_without_privileged_mode_changes(files, tmp_path, monkeypatch):
    """Works under RestrictSUIDSGID/Nix: only regular-file non-SGID chmod."""
    import stat
    real_mkdir, real_fchmod = os.mkdir, os.fchmod
    def mkdir(path, mode=0o777, **kwargs):
        assert mode == 0o770
        return real_mkdir(path, mode, **kwargs)
    def fchmod(fd, mode):
        assert stat.S_ISREG(os.fstat(fd).st_mode), 'Do not clear inherited directory SGID'
        assert mode == 0o660
        return real_fchmod(fd, mode)
    monkeypatch.setattr(os, 'mkdir', mkdir)
    monkeypatch.setattr(os, 'fchmod', fchmod)
    old_umask = os.umask(0o007)
    try:
        files.write('AgentShared', 'value=1')
        files.write('AgentShared', 'value=2')
    finally:
        os.umask(old_umask)
    directory = tmp_path / 'AgentShared'
    assert stat.S_IMODE(directory.stat().st_mode) & 0o777 == 0o770
    assert stat.S_IMODE((directory / 'script.tcl').stat().st_mode) == 0o660
    assert files.read('AgentShared') == 'value=2'
