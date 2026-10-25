"""Standalone FastMCP Streamable HTTP with local administrative ASGI safeguards."""
import hmac
import re
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.responses import JSONResponse


class LocalBearer:
    def __init__(self, app, token):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            headers = dict(scope['headers'])
            supplied = headers.get(b'authorization', b'')
            # Require a loopback Host even for authenticated non-browser clients.
            host = headers.get(b'host', b'')
            if not re.fullmatch(rb'(127\.0\.0\.1|localhost)(:[0-9]{1,5})?', host):
                await JSONResponse({'detail': 'Invalid Host'}, status_code=421)(scope, receive, send)
                return
            # No browser origins, even with a token.
            if b'origin' in headers or not hmac.compare_digest(supplied, b'Bearer ' + self.token):
                await JSONResponse({'detail': 'Unauthorized'}, status_code=401)(scope, receive, send)
                return
            size = headers.get(b'content-length', b'0')
            try:
                too_large = int(size) > 1024 * 1024
            except ValueError:
                too_large = True
            if too_large:
                await JSONResponse({'detail': 'Request too large'}, status_code=413)(scope, receive, send)
                return
            # Bound chunked bodies as well as declared Content-Length.
            total = 0
            chunks = []
            while True:
                message = await receive()
                if message['type'] != 'http.request':
                    return
                total += len(message.get('body', b''))
                if total > 1024 * 1024:
                    await JSONResponse({'detail': 'Request too large'}, status_code=413)(scope, receive, send)
                    return
                chunks.append(message.get('body', b''))
                if not message.get('more_body'):
                    break
            delivered = False
            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
                return await receive()
            await self.app(scope, bounded_receive, send)
            return
        await self.app(scope, receive, send)


def create_app(files, lifecycle, token: str):
    if len(token) < 32 or token.strip() != token:
        raise ValueError('Supply an external bearer token of at least 32 characters')
    mcp = FastMCP('Limnoria managed plugins')

    @mcp.tool()
    def list_plugins() -> list[str]:
        """List managed plugin directories (not all installed bot plugins)."""
        return files.list()

    @mcp.tool()
    def read_plugin_file(plugin: str, filename: str) -> str:
        """Read __init__.py, plugin.py, config.py or README.md in a managed plugin."""
        return files.read(plugin, filename)

    @mcp.tool()
    def write_plugin_file(plugin: str, filename: str, content: str) -> dict:
        """Write a managed plugin file. Loading Python grants bot-account code execution."""
        return files.write(plugin, filename, content)

    @mcp.tool()
    def load_plugin(plugin: str) -> dict:
        """Load an Agent-prefixed plugin on the bot loop. Executes Python as bot user."""
        return lifecycle(plugin, False)

    @mcp.tool()
    def reload_plugin(plugin: str) -> dict:
        """Reload a managed plugin on the bot loop. Not an arbitrary IRC command."""
        return lifecycle(plugin, True)

    # FastMCP owns its HTTP session-manager lifespan; do not wrap or double-run it.
    return mcp.http_app(path='/mcp', stateless_http=True, json_response=True,
                        middleware=[Middleware(LocalBearer, token=token)])
