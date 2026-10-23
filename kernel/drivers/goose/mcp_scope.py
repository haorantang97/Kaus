"""Reconcile only Kaus-owned Goose session MCP entries before cold activation."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from drivers.skill_execution.common import state_root


def _path(spec, conversation):
    digest=hashlib.sha256(conversation.id.encode()).hexdigest()
    return state_root(spec)/'session-mcp/goose'/f'{digest}.json'


def _read(path):
    if not path.exists():return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size>1024*1024:
        raise UnsupportedCapabilityError('会话 MCP 记录不可读')
    try:
        value=json.loads(path.read_text())
        if value.get('schema')!=1 or not isinstance(value.get('entries'),dict):raise ValueError()
        return value
    except (ValueError,TypeError,AttributeError) as exc:
        raise UnsupportedCapabilityError('会话 MCP 记录无效') from exc


def _write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic_write_text(path,json.dumps(value,sort_keys=True));path.chmod(0o600)


def _hash(server):
    return hashlib.sha256(json.dumps(server,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _empty(name):
    return {'name':name,'command':sys.executable,'args':[str(Path(__file__).with_name('empty_mcp.py'))],'env':[]}


async def _entries(connection, session_id):
    result=await connection.call('_goose/unstable/session/extensions/list',{'sessionId':session_id})
    if not isinstance(result,dict) or not isinstance(result.get('extensions'),list):
        raise UnsupportedCapabilityError('原生 MCP 清单不可验证')
    entries={}
    for row in result['extensions']:
        extension=row.get('extension',{})
        if extension.get('type')!='mcp':continue
        server=extension.get('server',{});name=server.get('name')
        if not isinstance(name,str) or name in entries or not isinstance(row.get('extensionKey'),str):
            raise UnsupportedCapabilityError('原生 MCP 名称或来源不唯一')
        entries[name]={'key':row['extensionKey'],'hash':_hash(server)}
    return entries


async def before_runtime_session(connection,spec,conversation,metadata):
    requested=list(metadata.get('mcpServers',[]));names=[row['name'] for row in requested]
    if len(set(names))!=len(names):raise UnsupportedCapabilityError('MCP 名称重复')
    result={**metadata,'_kausGooseRequested':names,'_kausGooseRemoved':{}}
    record=_read(_path(spec,conversation))
    if not conversation.native_session_id or not record:return result
    if record.get('sessionId')!=conversation.native_session_id:
        raise UnsupportedCapabilityError('会话 MCP 记录与原生会话不匹配')
    current=await _entries(connection,conversation.native_session_id)
    for name,expected in record['entries'].items():
        actual=current.get(name)
        if actual and actual not in (expected,record.get('pending',{}).get(name)):
            raise UnsupportedCapabilityError('原生会话 MCP 已被外部修改，请重新确认配置')
    removed={name:current[name]['key'] for name in record['entries'] if name not in names and name in current}
    if removed:
        tombstones=[_empty(name) for name in removed]
        pending={name:{'key':removed[name],'hash':_hash(_empty(name))} for name in removed}
        _write(_path(spec,conversation),{**record,'pending':pending})
        result['mcpServers']=[*requested,*tombstones]
        result['_kausGooseRemoved']=removed
    return result


async def after_runtime_session(connection,spec,conversation,metadata,session_id):
    removed=metadata.get('_kausGooseRemoved',{})
    current=await _entries(connection,session_id)
    for name,key in removed.items():
        if name not in current:continue
        if current[name]!={'key':key,'hash':_hash(_empty(name))}:
            raise UnsupportedCapabilityError('待移除 MCP 的来源发生变化')
        await connection.call('_goose/unstable/session/extensions/remove',{'sessionId':session_id,'extensionKey':key})
    current=await _entries(connection,session_id)
    names=metadata.get('_kausGooseRequested',[])
    if any(name in current for name in removed) or any(name not in current for name in names):
        raise UnsupportedCapabilityError('原生 MCP 配置未完整应用')
    _write(_path(spec,conversation),{'schema':1,'sessionId':session_id,'entries':{name:current[name] for name in names}})
