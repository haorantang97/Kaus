"""A bounded native RPC sidecar shared by ordinary and Group conversations."""
import asyncio
import json
from pathlib import Path
import shutil
import shlex
from drivers.acp.client import AcpAgentSpec,AcpConnection,AcpTransportError
from drivers.skill_execution.node_package import locate


def _launcher_environment(command, env):
    executable=shutil.which(command,path=env.get('PATH'))
    if not executable:return None
    try:
        path=Path(executable)
        if path.stat().st_size>65536:return dict(env)
        source=path.read_text()
    except (OSError,UnicodeError):return dict(env)
    if not source.startswith(('#!/bin/sh','#!/bin/bash','#!/usr/bin/env sh','#!/usr/bin/env bash')):
        return dict(env)
    result=dict(env)
    for line in source.splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        try:parts=shlex.split(line)
        except ValueError:return None
        if len(parts)==2 and parts[0]=='export' and parts[1].startswith('PATH='):
            value=parts[1][5:];segments=value.split(':')
            if segments[-1]!='$PATH' or any(not Path(p).is_absolute() or '$' in p or '`' in p for p in segments[:-1]):return None
            result['PATH']=':'.join([*segments[:-1],result.get('PATH','')])
        elif parts and parts[0]=='exec' and len(parts)==3 and Path(parts[1]).is_absolute() and parts[2]=='$@':
            continue
        else:return None
    return result


def sidecar_spec(spec, prefix, env):
    # Global CLI flags run through the normal CLI, whose profile resolution is
    # authoritative. The verified module path covers ordinary env-based setups.
    if len(prefix)!=1:return None
    env=_launcher_environment(prefix[0],env)
    if env is None:return None
    package=locate(spec,'openclaw')
    if not package:return None
    try:
        if json.loads((package/'package.json').read_text()).get('version')!='2026.9.6':return None
    except (OSError,ValueError):return None
    runtime=package/'dist/gateway-rpc.runtime.js'
    node=shutil.which('node',path=env.get('PATH'))
    if not node or not runtime.is_file():return None
    return AcpAgentSpec(command=(node,str(Path(__file__).with_name('gateway_sidecar.mjs')),runtime.as_uri()),
        cwd=spec.cwd,inherit_env=False,env=env)


class NativeGatewayTransport:
    def __init__(self):
        self.connection=None
        self.spec=None
        self.lock=asyncio.Lock()
        self.idle=None

    async def close(self):
        if self.idle and self.idle is not asyncio.current_task():self.idle.cancel()
        self.idle=None
        async with self.lock:
            if self.connection:await self.connection.aclose()
            self.connection=None

    async def _expire(self):
        try:
            await asyncio.sleep(60)
            await self.close()
        except asyncio.CancelledError:pass

    async def call(self,spec,method,params,timeout):
        if self.idle:self.idle.cancel();self.idle=None
        async with self.lock:
            if self.connection and (not self.connection.alive or self.spec!=spec):
                await self.connection.aclose();self.connection=None
            if self.connection is None:
                self.connection=await AcpConnection.spawn(spec);self.spec=spec
            try:
                result=await self.connection.call(method,{'arguments':dict(params),'timeout':str(int(timeout*1000))},timeout=timeout+5)
                if not isinstance(result,dict):raise AcpTransportError('Gateway result unavailable')
                return result
            except BaseException:
                # A write may have reached the Gateway. Never replay it after a
                # disconnect; callers refresh the CAS/session revision first.
                await self.connection.aclose();self.connection=None
                raise
            finally:
                self.idle=asyncio.create_task(self._expire())
