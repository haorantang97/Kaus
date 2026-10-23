import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from drivers.acp.client import AcpAgentSpec,AcpRpcError
from drivers.openclaw.gateway_transport import NativeGatewayTransport


class Connection:
    alive=True
    def __init__(self):self.calls=[];self.closed=False;self.failure=False;self.wait=False
    async def call(self,method,params,timeout):
        self.calls.append((method,params))
        if self.wait:await asyncio.sleep(30)
        if self.failure:raise AcpRpcError(-32000,'synthetic')
        return {'method':method}
    async def aclose(self):self.alive=False;self.closed=True


async def test_warm_connection_is_reused_and_env_change_reconnects(monkeypatch):
    connections=[]
    async def spawn(spec):connections.append(Connection());return connections[-1]
    monkeypatch.setattr('drivers.openclaw.gateway_transport.AcpConnection.spawn',spawn)
    transport=NativeGatewayTransport();spec=AcpAgentSpec(command=('fixture',))
    try:
        for _ in range(3):assert await transport.call(spec,'models.list',{},1)=={'method':'models.list'}
        assert len(connections)==1
        await transport.call(spec.with_env(OPENCLAW_GATEWAY_TOKEN='synthetic'),'models.list',{},1)
        assert len(connections)==2 and connections[0].closed
    finally:await transport.close()
    assert connections[1].closed


async def test_failed_write_is_not_retried_and_next_read_reconnects(monkeypatch):
    connections=[]
    async def spawn(spec):
        c=Connection();c.failure=not connections;connections.append(c);return c
    monkeypatch.setattr('drivers.openclaw.gateway_transport.AcpConnection.spawn',spawn)
    transport=NativeGatewayTransport();spec=AcpAgentSpec(command=('fixture',))
    try:
        with pytest.raises(AcpRpcError):await transport.call(spec,'config.patch',{'baseHash':'x'},1)
        assert len(connections)==1 and len(connections[0].calls)==1 and connections[0].closed
        assert await transport.call(spec,'config.get',{},1)=={'method':'config.get'}
    finally:await transport.close()


async def test_cancel_closes_native_worker(monkeypatch):
    connection=Connection();connection.wait=True
    monkeypatch.setattr('drivers.openclaw.gateway_transport.AcpConnection.spawn',AsyncMock(return_value=connection))
    transport=NativeGatewayTransport();spec=AcpAgentSpec(command=('fixture',))
    task=asyncio.create_task(transport.call(spec,'config.get',{},1))
    await asyncio.sleep(.01);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert connection.closed
    await transport.close()


def test_sidecar_uses_literal_native_launcher_path_without_evaluating_shell(tmp_path):
    from drivers.openclaw.gateway_transport import _launcher_environment
    launcher=tmp_path/'openclaw';launcher.write_text('#!/bin/sh\nexport PATH=/managed/node/bin:/managed/tools:"$PATH"\nexec /managed/openclaw "$@"\n');launcher.chmod(0o700)
    assert _launcher_environment(str(launcher),{'PATH':'/system/bin'})['PATH']=='/managed/node/bin:/managed/tools:/system/bin'
    launcher.write_text('#!/bin/sh\nexport PATH="$(danger):$PATH"\nexec /managed/openclaw "$@"\n')
    assert _launcher_environment(str(launcher),{'PATH':'/system/bin'}) is None
