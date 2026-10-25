"""Real MCP stdio handshake with no bot running (recovery bootstrap regression)."""
import os
import sys
from pathlib import Path
import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_offline_controller_bootstraps(tmp_path, monkeypatch):
    # Nix supplies dependencies via PYTHONPATH rather than site-packages. Prove
    # the child preserves that environment instead of replacing it with src.
    dependency_path = tmp_path / 'dependency-path'
    dependency_path.mkdir()
    (dependency_path / 'dependency_probe.py').write_text('available = True\n')
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(filter(None, [
        str(dependency_path), os.environ.get('PYTHONPATH', '')])))
    helper = tmp_path / 'helper'
    helper.write_text('#!' + sys.executable + '\nimport json, sys\nprint(json.dumps({"action":sys.argv[1],"returncode":0,"stdout":"synthetic","stderr":""}))\n')
    helper.chmod(0o700)
    async def run():
        params = StdioServerParameters(command=sys.executable,
            args=['-c', 'import dependency_probe; from limnoria_mcp.control import main; main()'],
            env={'PYTHONPATH': os.pathsep.join(filter(None, [
                     str(Path(__file__).resolve().parents[1] / 'src'),
                     os.environ.get('PYTHONPATH', '')])),
                 'LIMNORIA_MCP_CONTROL_HELPER': str(helper),
                 'LIMNORIA_MCP_SCRIPT_ROOT': str(tmp_path)})
        with anyio.fail_after(15):
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert len(tools.tools) == 9
                    result = await session.call_tool('service_action', {'action':'start'})
                    assert not result.isError
                    assert 'synthetic' in result.content[0].text
                    result = await session.call_tool('write_script', {'name':'AgentRepair', 'source':'repaired=1'})
                    assert not result.isError
                    result = await session.call_tool('read_script', {'name':'AgentRepair'})
                    assert not result.isError
                    assert result.content[0].text == 'repaired=1'
    anyio.run(run)
