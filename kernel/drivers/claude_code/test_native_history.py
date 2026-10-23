import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from drivers.claude_code.native_history import load_native_history


def write(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))


def fixture(tmp_path):
    sid=str(uuid4())
    path=tmp_path/'home/projects/project'/f'{sid}.jsonl'
    spec=SimpleNamespace(build_env=lambda:{'CLAUDE_CONFIG_DIR':str(tmp_path/'home')})
    rows=[{'sessionId':sid,'uuid':'user-1','type':'user','message':{'role':'user','content':'question'}},
          {'sessionId':sid,'uuid':'answer-1','parentUuid':'user-1','type':'assistant',
           'message':{'role':'assistant','content':[{'type':'text','text':'answer'}]}}]
    write(path,rows)
    return sid,path,spec,rows


async def test_exact_full_local_claude_history_stable_and_read_only(tmp_path):
    sid,path,spec,rows=fixture(tmp_path)
    original=path.read_bytes()
    first=await load_native_history(spec,None,sid)
    assert first.complete and first.missing==()
    assert [(e.entry_id,e.text) for e in first.entries]==[('native:user-1','question'),('native:answer-1','answer')]
    assert await load_native_history(spec,None,sid)==first
    assert path.read_bytes()==original
    path.unlink()
    with pytest.raises(FileNotFoundError):
        await load_native_history(spec,None,sid)


@pytest.mark.parametrize('change',['tool','image','compacted','branch','duplicate','invalid'])
async def test_claude_rich_or_incomplete_history_never_claims_complete(tmp_path,change):
    sid,path,spec,rows=fixture(tmp_path)
    if change in ('tool','image'):
        rows[1]['message']['content'].append({'type':'tool_use' if change=='tool' else 'image','id':'tool-1'})
    elif change=='compacted':
        rows.append({'sessionId':sid,'type':'system','subtype':'compact_boundary'})
    elif change=='branch':
        rows.append({**rows[1],'uuid':'branch-answer'})
    elif change=='duplicate':
        rows.append(rows[1])
    else:
        rows[1]['message']['content']={'unknown':'value'}
    write(path,rows)
    history=await load_native_history(spec,None,sid)
    assert not history.complete and history.missing


async def test_claude_unverified_identity_or_symlink_is_rejected(tmp_path):
    sid,path,spec,rows=fixture(tmp_path)
    rows[0]['sessionId']=str(uuid4())
    write(path,rows)
    with pytest.raises(ValueError):
        await load_native_history(spec,None,sid)
    outside=tmp_path/'outside.jsonl'
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(ValueError):
        await load_native_history(spec,None,sid)
