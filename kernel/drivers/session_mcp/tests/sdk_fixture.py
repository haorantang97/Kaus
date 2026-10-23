"""Loopback-only MCP fixture using the installed official SDK."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp.server.mcpserver import MCPServer

transport, log_path = sys.argv[1:3]
log = Path(log_path)


def record(event):
    with log.open('a') as stream:
        stream.write(json.dumps({'event': event, 'pid': os.getpid()}) + '\n')


server = MCPServer('Kaus isolated MCP fixture')


@server.tool()
async def echo(text: str) -> str:
    record('echo')
    return text


@server.tool()
async def slow() -> str:
    record('slow-start')
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        record('slow-cancelled')
        raise
    return 'unexpected completion'


record('start')
try:
    if transport == 'stdio':
        server.run()
    else:
        import uvicorn
        from starlette.responses import PlainTextResponse
        app = server.sse_app() if transport == 'sse' else server.streamable_http_app()

        class FixtureAuth:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope['type'] == 'http' and dict(scope.get('headers', [])).get(b'x-kaus-fixture') != b'synthetic-header':
                    return await PlainTextResponse('unauthorized', status_code=401)(scope, receive, send)
                return await self.app(scope, receive, send)

        app.add_middleware(FixtureAuth)

        uvicorn.run(app, fd=int(sys.argv[3]), log_level='critical')
finally:
    record('exit')
