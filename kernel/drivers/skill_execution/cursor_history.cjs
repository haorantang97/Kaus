'use strict';
const input=JSON.parse(process.argv[2]);
const noNetwork=()=>{throw Error('Network is unavailable for native history reads')};
require('node:net').connect=noNetwork;require('node:net').createConnection=noNetwork;
require('node:tls').connect=noNetwork;globalThis.fetch=noNetwork;
(async()=>{
  const {Blob,Store,context}=await require('./cursor_native.cjs')(input);
  const blob=await Blob.initAndLoad(input.database), store=new Store(blob,blob);
  try {
    await store.resetFromDb(context);
    const id=store.getId();
    if(id!==input.id)throw Error('Native session identity mismatch');
    const history=await store.getFullConversation(context), entries=[], missing=new Set();
    for(const [turnIndex,row] of (history.turns||[]).entries()){
      if(row.turn.case!=='agentConversationTurn')continue;
      const turn=row.turn.value, prefix='native:'+input.id+':'+(turn.userMessage?.messageId||turnIndex);
      if(turn.userMessage?.text && !turn.userMessage.isSimulatedMsg)entries.push({entry_id:prefix+':user',role:'user',text:turn.userMessage.text});
      if(turn.userMessage?.selectedContext?.selectedImages?.length)missing.add('non_text_content');
      for(const [stepIndex,step] of turn.steps.entries()){
        if(step.message.case==='assistantMessage' && step.message.value.text)
          entries.push({entry_id:prefix+':'+stepIndex,role:'assistant',text:step.message.value.text});
        if(step.message.case==='toolCall')missing.add('tool_records');
      }
    }
    process.stdout.write(JSON.stringify({entries,missing:[...missing]}));
  } finally {await store.dispose()}
})().catch(error=>{console.error(error.message);process.exitCode=1});
