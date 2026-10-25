"""Independent stdio MCP for a deployment-supplied, fixed service control helper."""
import json
import os
import subprocess
from typing import Literal
from fastmcp import FastMCP

ACTIONS = frozenset({'start', 'stop', 'restart', 'status', 'logs'})


def invoke(helper: str, action: str):
    if action not in ACTIONS or not os.path.isabs(helper):
        raise ValueError('Invalid fixed helper or action')
    # No shell, unit, arguments, environment overrides, or journal filters from client.
    result = subprocess.run([helper, action], capture_output=True, text=True, timeout=35,
                            env={'PATH': '/nonexistent', 'LANG': 'C.UTF-8'}, check=True)
    if len(result.stdout) > 128 * 1024:
        raise ValueError('Helper output exceeds limit')
    return json.loads(result.stdout)


def main():
    helper = os.environ['LIMNORIA_MCP_CONTROL_HELPER']
    if not os.path.isabs(helper):
        raise ValueError('Helper must be an absolute immutable deployment path')
    mcp = FastMCP('Limnoria service control')

    @mcp.tool()
    def service_action(action: Literal['start', 'stop', 'restart']) -> dict:
        """Control only the deployment-configured Limnoria service, never another unit."""
        return invoke(helper, action)

    @mcp.tool()
    def service_status() -> dict:
        """Get status for the single configured Limnoria service."""
        return invoke(helper, 'status')

    @mcp.tool()
    def service_logs() -> dict:
        """Get bounded recent service logs; logs may contain private IRC content."""
        return invoke(helper, 'logs')

    register_proxy(mcp)
    mcp.run(transport='stdio')


async def proxy_tool(name: str, arguments: dict):
    """Connect only on demand: service recovery still works with the bot stopped."""
    from pathlib import Path
    import anyio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import httpx
    allowed = {'list_plugins', 'read_plugin_file', 'write_plugin_file', 'load_plugin', 'reload_plugin'}
    if name not in allowed:
        raise ValueError('Unsupported bot tool')
    token = Path(os.environ['LIMNORIA_MCP_TOKEN_FILE']).read_text().strip()
    port = int(os.environ.get('LIMNORIA_MCP_PORT', '8765'))
    with anyio.fail_after(25):
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + token},
                                     trust_env=False, timeout=20) as client:
            async with streamable_http_client(f'http://127.0.0.1:{port}/mcp',
                                              http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    return await session.call_tool(name, arguments)


def register_proxy(mcp):
    from .files import PluginFiles
    files = PluginFiles(os.environ['LIMNORIA_MCP_PLUGIN_ROOT'])
    @mcp.tool()
    async def list_plugins():
        """List managed plugin directories, even when the bot is down."""
        return files.list()

    @mcp.tool()
    async def read_plugin_file(plugin: str, filename: str):
        """Read a bounded managed plugin source file."""
        return files.read(plugin, filename)

    @mcp.tool()
    async def write_plugin_file(plugin: str, filename: str, content: str):
        """Write managed plugin Python. Loading grants code execution as the bot user."""
        return files.write(plugin, filename, content)

    @mcp.tool()
    async def load_plugin(plugin: str):
        """Load an Agent-prefixed managed plugin (executes bot-account Python)."""
        return await proxy_tool('load_plugin', {'plugin': plugin})

    @mcp.tool()
    async def reload_plugin(plugin: str):
        """Reload a managed plugin; does not expose arbitrary IRC commands."""
        return await proxy_tool('reload_plugin', {'plugin': plugin})
