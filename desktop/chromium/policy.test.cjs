const { test } = require('node:test');
const assert = require('node:assert/strict');
const p = require('./policy.cjs');
test('fixed editor and auth navigation never accept arbitrary origins or local files', () => {
 assert.equal(p.launch('chatcut','https://app.chatcut.io/zh/editor/test?editor-boot-token=synthetic'),true);
 for(const url of ['file:///etc/passwd','https://app.chatcut.io.evil.org/editor/a','https://u@app.chatcut.io/editor/a','https://app.chatcut.io/profile']) assert.equal(p.launch('chatcut',url),false);
 assert.equal(p.remote('chatcut','https://accounts.google.com/signin'),true);assert.equal(p.remote('chatcut','https://example.com'),false);
});
test('each site keeps its own launch pages and navigation origins', () => {
 for(const url of ['https://www.midjourney.com/imagine','https://www.midjourney.com/explore?tab=top','https://www.midjourney.com/jobs/0a1b2c3d-1111-2222-3333-444455556666']) assert.equal(p.launch('midjourney',url),true);
 for(const url of ['https://www.midjourney.com/account','https://www.midjourney.com.evil.org/imagine','https://u@www.midjourney.com/imagine','https://app.chatcut.io/zh']) assert.equal(p.launch('midjourney',url),false);
 assert.equal(p.launch('unknown','https://www.midjourney.com/imagine'),false);assert.equal(p.launch('__proto__','https://www.midjourney.com/imagine'),false);
 assert.equal(p.remote('midjourney','https://discord.com/oauth2/authorize'),true);assert.equal(p.remote('chatcut','https://discord.com/oauth2/authorize'),false);
 assert.equal(p.remote('midjourney','https://app.chatcut.io/zh'),false);
});
test('filling is limited to sites with an input and never carries a submit key', () => {
 assert.equal(p.fillText('chatcut','a cat'),null);
 assert.equal(p.fillText('midjourney','  a cat\r\non a roof\u0007 --ar 16:9 '),'a cat on a roof --ar 16:9');
 for(const text of ['', '   ', 'x'.repeat(6001), 42]) assert.equal(p.fillText('midjourney',text),null);
 assert.equal(p.fillImage('midjourney','data:image/png;base64,iVBORw0KGgo='),'data:image/png;base64,iVBORw0KGgo=');
 for(const image of ['data:image/svg+xml;base64,PHN2Zz4=','https://example.com/a.png','data:image/png;base64,<script>']) assert.equal(p.fillImage('midjourney',image),null);
 assert.equal(p.fillImage('chatcut','data:image/png;base64,iVBORw0KGgo='),null);
});
test('main-frame origin and module identities are exact', () => {
 assert.equal(p.trusted('http://127.0.0.1:3187/'),true);assert.equal(p.trusted('http://127.0.0.1:8877/'),false);
 assert.equal(p.trusted('http://u@127.0.0.1:3187/'),false);assert.equal(p.callback('http://127.0.0.1:3187/api/workbench/callback/content-workbench?code=synthetic'),true);
 assert.equal(p.callback('http://127.0.0.1:3187/arbitrary'),false);
 for(const id of ['../a','https://example.com','a/b','']) assert.equal(p.moduleId(id),false);
});
test('layout and downloads remain in bounded scopes', () => {
 assert.deepEqual(p.layout({x:100,y:100,width:200,height:300},1000,800),{x:100,y:100,width:200,height:300});
 assert.equal(p.layout({x:-1,y:0,width:200,height:300},1000,800),null);
 assert.equal(p.layout({x:0,y:0,width:Infinity,height:300},1000,800),null);
 assert.equal(p.downloadName('../../private.mp4'),'private.mp4');assert.equal(p.downloadName('C:\\tmp\\export.mp4'),'export.mp4');
 assert.equal(p.downloadName('',"Midjourney-download"),'Midjourney-download');
});
test('the sandboxed preload advertises exactly the registered sites', () => {
 const source=require('node:fs').readFileSync(require('node:path').join(__dirname,'preload.cjs'),'utf8');
 assert.deepEqual(JSON.parse(source.match(/Object\.freeze\((\[[^\]]*\])\)/)[1].replaceAll("'",'"')),Object.keys(p.SITES));
});
test('the side browser opens web pages but not this host, local files or browser internals', () => {
 for(const url of ['https://example.com/a?q=1','http://localhost:5174/','http://127.0.0.1:8000/x','https://www.midjourney.com/imagine']) assert.equal(p.browsable(url),true);
 for(const url of ['file:///etc/passwd','chrome://settings','javascript:alert(1)','data:text/html,x','https://u:p@example.com/','http://127.0.0.1:3187/','http://localhost:8877/api','http://app.localhost:8877/']) assert.equal(p.browsable(url),false);
});
test('browser text keeps lines on ordinary pages and follows a site profile where one exists', () => {
 assert.deepEqual(p.browserText('line one\nline two','https://example.com/'),{text:'line one\nline two',selectors:[],replace:false});
 const mj=p.browserText('a cat\non a roof','https://www.midjourney.com/imagine');
 assert.equal(mj.text,'a cat on a roof');assert.equal(mj.replace,true);assert.equal(mj.selectors[0],'textarea#desktop_input_bar');
 assert.equal(p.profile('https://app.chatcut.io/zh'),null);assert.equal(p.browserText('x'.repeat(20001),'https://example.com/').text,null);
});
