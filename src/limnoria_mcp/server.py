"""Standalone FastMCP Streamable HTTP with local administrative ASGI safeguards."""
import hmac
import re
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.responses import JSONResponse


class LocalBearer:
    """Shared local-request guards; optional bearer for the authenticated bot only."""
    def __init__(self, app, token=None):
        self.app = app
        self.token = token.encode() if token is not None else None

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
            if b'origin' in headers or (self.token is not None and
                    not hmac.compare_digest(supplied, b'Bearer ' + self.token)):
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


def local_http_app(mcp):
    """Tokenless only for a strict loopback-bound trusted-host service."""
    return mcp.http_app(path='/mcp', stateless_http=True, json_response=True,
                        middleware=[Middleware(LocalBearer)])


def authenticated_http_app(mcp, token: str):
    if len(token) < 32 or token.strip() != token:
        raise ValueError('Supply an external bearer token of at least 32 characters')
    return mcp.http_app(path='/mcp', stateless_http=True, json_response=True,
                        middleware=[Middleware(LocalBearer, token=token)])


def create_app(files, runtime, token: str):
    mcp = FastMCP('Limnoria Safe Tcl scripts')

    @mcp.tool()
    def list_scripts() -> list[str]:
        """List named Tcl scripts. No Python plugin access."""
        return files.list()

    @mcp.tool()
    def read_script(name: str) -> str:
        """Read the bounded Tcl source of one named script."""
        return files.read(name)

    @mcp.tool()
    def write_script(name: str, source: str) -> dict:
        """Atomically write Tcl source; does not activate it or execute Python."""
        return files.write(name, source)

    @mcp.tool()
    def validate_script(name: str) -> dict:
        """Stage in the isolated Safe Tcl worker; discard all replies and state."""
        return runtime.validate(name)

    @mcp.tool()
    def activate_script(name: str) -> dict:
        """Validate and hot-swap one script generation; failure keeps old version."""
        return runtime.activate(name)

    @mcp.tool()
    def script_status() -> dict:
        """List active script versions without exposing per-script state."""
        return {'scripts': runtime.status(), 'bundle': runtime.bundle_status(),
                'network_effects':runtime.network_status()}

    return authenticated_http_app(mcp, token)
