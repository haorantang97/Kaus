"""No-capability placeholder used while deleting a cold Goose MCP entry.

It never launches the previous command or provides model tools. Goose removes
it immediately after activation and before Kaus permits a model turn.
"""
import json
import sys

for line in sys.stdin:
    request=json.loads(line)
    if 'id' not in request:continue
    method=request.get('method')
    if method=='initialize':
        result={'protocolVersion':request.get('params',{}).get('protocolVersion','2024-11-05'),
                'serverInfo':{'name':'kaus-empty','version':'1'},'capabilities':{}}
    elif method=='tools/list':result={'tools':[]}
    elif method=='resources/list':result={'resources':[]}
    elif method=='resources/templates/list':result={'resourceTemplates':[]}
    elif method=='prompts/list':result={'prompts':[]}
    elif method=='ping':result={}
    else:
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32601,'message':'Method not found'}}),flush=True)
        continue
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
