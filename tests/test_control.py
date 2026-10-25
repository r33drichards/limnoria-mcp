import pytest
from unittest.mock import patch
from limnoria_mcp.control import invoke

@pytest.mark.parametrize('action', ['shell', 'restart other.service', ';id', 'logs --all'])
def test_invalid_actions(action):
    with pytest.raises(ValueError):
        invoke('/fixed/helper', action)


def test_fixed_argv():
    with patch('subprocess.run') as run:
        run.return_value.stdout = '{"returncode":0}'
        assert invoke('/fixed/helper', 'restart') == {'returncode':0}
        assert run.call_args.args == (['/fixed/helper', 'restart'],)
        assert not run.call_args.kwargs.get('shell', False)
