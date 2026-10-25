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
    allowed = {'list_scripts', 'read_script', 'write_script', 'validate_script', 'activate_script', 'script_status'}
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
    from .files import ScriptFiles
    files = ScriptFiles(os.environ['LIMNORIA_MCP_SCRIPT_ROOT'])
    @mcp.tool()
    async def list_scripts():
        """List Tcl scripts, even when the bot is down."""
        return files.list()

    @mcp.tool()
    async def read_script(name: str):
        """Read bounded Tcl source, never Python plugin files."""
        return files.read(name)

    @mcp.tool()
    async def write_script(name: str, source: str):
        """Write Tcl source only. Activation is a separate explicit action."""
        return files.write(name, source)

    @mcp.tool()
    async def validate_script(name: str):
        """Validate in isolated staging with no live reply/state effects."""
        return await proxy_tool('validate_script', {'name': name})

    @mcp.tool()
    async def activate_script(name: str):
        """Atomically activate a validated Tcl generation; no bot restart."""
        return await proxy_tool('activate_script', {'name': name})

    @mcp.tool()
    async def script_status():
        """Get active Tcl generation hashes, not internal script state."""
        return await proxy_tool('script_status', {})
