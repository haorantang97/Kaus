'use strict';
// Read only the installed native parsers. Skip its executable entry point,
// built-in synchronization, telemetry, MCP startup and account endpoints.
const fs=require('node:fs'),path=require('node:path'),Module=require('node:module'),crypto=require('node:crypto');
const input=JSON.parse(process.argv[2]);
const index=path.join(input.package,'index.js');
for(const [file,digest] of Object.entries(input.modules))
  if(crypto.createHash('sha256').update(fs.readFileSync(path.join(input.package,file))).digest('hex')!==digest)throw Error('Cursor inventory version changed');
const noNetwork=()=>{throw Error('Network unavailable in read-only Skill inventory')};
require('node:net').connect=noNetwork;require('node:net').createConnection=noNetwork;
require('node:tls').connect=noNetwork;globalThis.fetch=noNetwork;
const original=Module.prototype._compile;
Module.prototype._compile=function(code,file){
  if(file===index){
    const anchor='var __webpack_exports__=__webpack_require__("./src/main.tsx")';
    if(code.split(anchor).length!==2)throw Error('Cursor inventory entry requires verification');
    code=code.replace(anchor,'globalThis[Symbol.for("kaus.cursor.inventory")]=__webpack_require__');
  }
  return original.call(this,code,file);
};
require(index);
(async()=>{
 const r=globalThis[Symbol.for('kaus.cursor.inventory')],native=r('../local-exec/dist/index.js'),ctx=r('../context/dist/core.js').q6();
 const work=input.cwd?[input.cwd]:[],home=input.home;
 const loader=new native.EVC(ctx,work,home,{},false,undefined,()=>true,Promise.resolve(),'cli');
 const local=new native.Npg(work,home,()=>({importThirdPartyPlugins:true,loadUserLocalPlugins:true,loadCursorFirstParty:false}));
 await local.getAllEnabledPlugins();
 for(const directory of input.plugins)await local.registerExtensionPlugin({path:directory,extensionId:'kaus-explicit-source'});
 const plugins=await local.reload();
 const pluginLoader=new native.Rxj(ctx,()=>({importThirdPartyPlugins:true}),null,local);
 const rows=[...await loader.getAllCursorRules(ctx),...await loader.getAllAgentSkills(ctx),...await pluginLoader.getAllAgentSkills(ctx)];
 // alwaysApply plugin Skills appear only in rules, so include their source too.
 const paths=[...rows.map(row=>row.fullPath),...plugins.flatMap(plugin=>plugin.skills.map(skill=>path.join(plugin.installPath,skill.path)))];
 loader.dispose();pluginLoader.dispose();
 process.stdout.write('\n'+JSON.stringify({paths:[...new Set(paths)].filter(p=>path.basename(p)==='SKILL.md')}));
})().catch(error=>{console.error(error.message);process.exitCode=1});
