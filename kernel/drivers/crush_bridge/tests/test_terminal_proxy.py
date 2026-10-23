import asyncio
import json
import tempfile
from pathlib import Path

import pytest

@pytest.fixture
def socket_root():
    with tempfile.TemporaryDirectory(prefix="kaus-proxy-",dir="/tmp") as root:
        yield Path(root)

from drivers.crush_bridge.terminal_proxy import TerminalProxy


def test_initial_restore_once_and_all_config_writes_remain_private():
    proxy=TerminalProxy('/unused','workspace',{'provider':'new','model':'chosen','reasoning_effort':'high'},('old','previous'))
    request={'scope':0,'model_type':'large','model':{'provider':'old','model':'previous'}}
    path='/v1/workspaces/workspace/config/model'
    first=json.loads(proxy.rewrite('POST',path,json.dumps(request).encode()))
    assert first['scope']==1 and first['model']['model']=='chosen'
    second=json.loads(proxy.rewrite('POST',path,json.dumps(request).encode()))
    assert second['scope']==1 and second['model']['model']=='previous'
    setting=json.loads(proxy.rewrite('POST','/v1/workspaces/workspace/config/set',b'{"scope":0,"key":"mcp","value":{}}'))
    assert setting['scope']==1
    permission=b'{"action":"deny","permission":{"id":"original"}}'
    assert proxy.rewrite('POST','/v1/workspaces/workspace/permissions/grant',permission)==permission


async def test_binary_and_chunked_sse_bytes_pass_unchanged_and_cleanup(socket_root):
    tmp_path=socket_root
    source=tmp_path/'source.sock';target=tmp_path/'target.sock';received=[]
    async def native(reader,writer):
        received.append(await reader.readuntil(b'\r\n\r\n'))
        writer.write(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n3\r\n\x00\xffx\r\n0\r\n\r\n');await writer.drain();writer.close();await writer.wait_closed()
    server=await asyncio.start_unix_server(native,path=str(source));proxy=TerminalProxy(str(source),'fixture',{})
    await proxy.start(str(target))
    try:
        reader,writer=await asyncio.open_unix_connection(str(target));writer.write(b'GET /v1/workspaces/fixture/events HTTP/1.1\r\nHost: localhost\r\n\r\n');await writer.drain()
        result=await asyncio.wait_for(reader.read(),2)
        assert result.endswith(b'3\r\n\x00\xffx\r\n0\r\n\r\n')
        assert b'GET /v1/workspaces/fixture/events' in received[0]
        writer.close();await writer.wait_closed()
    finally:
        await proxy.close();server.close();await server.wait_closed()
    assert not proxy.tasks
