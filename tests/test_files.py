import os
import pytest
from limnoria_mcp.files import PluginFiles, MAX_BYTES

@pytest.fixture
def files(tmp_path):
    fs = PluginFiles(str(tmp_path))
    yield fs
    fs.close()


def test_write_read(files):
    files.write('AgentHello', 'plugin.py', 'answer = 42\n')
    assert files.read('AgentHello', 'plugin.py') == 'answer = 42\n'
    assert files.list() == ['AgentHello']

@pytest.mark.parametrize('name', ['../Owner', '/etc', 'Owner', 'MCPBridge', 'agentHello', 'AgentHello/../x'])
def test_names(files, name):
    with pytest.raises(ValueError):
        files.write(name, 'plugin.py', '')

@pytest.mark.parametrize('filename', ['../config', '/etc/passwd', 'other.py', 'a/b.py'])
def test_filename(files, filename):
    with pytest.raises(ValueError):
        files.write('AgentHello', filename, '')


def test_links(files, tmp_path):
    outside = tmp_path / 'outside'
    outside.write_text('secret')
    (tmp_path / 'AgentLink').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        files.read('AgentLink', 'plugin.py')
    files.write('AgentHello', 'plugin.py', '')
    path = tmp_path / 'AgentHello' / 'config.py'
    path.symlink_to(outside)
    with pytest.raises(OSError):
        files.read('AgentHello', 'config.py')
    files.write('AgentHello', 'config.py', 'ok=1')
    assert outside.read_text() == 'secret'
    path.unlink()
    os.link(outside, path)
    with pytest.raises(ValueError):
        files.read('AgentHello', 'config.py')
    files.write('AgentHello', 'config.py', 'ok=2')
    assert outside.read_text() == 'secret'


def test_bounds_and_syntax(files):
    with pytest.raises(ValueError):
        files.write('AgentHello', 'README.md', 'x' * (MAX_BYTES + 1))
    with pytest.raises(SyntaxError):
        files.write('AgentHello', 'plugin.py', 'invalid ! python')
