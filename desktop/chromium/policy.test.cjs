const { test } = require('node:test');
const assert = require('node:assert/strict');
const p = require('./policy.cjs');
test('fixed editor and auth navigation never accept arbitrary origins or local files', () => {
 assert.equal(p.launch('https://app.chatcut.io/zh/editor/test?editor-boot-token=synthetic'),true);
 for(const url of ['file:///etc/passwd','https://app.chatcut.io.evil.org/editor/a','https://u@app.chatcut.io/editor/a','https://app.chatcut.io/profile']) assert.equal(p.launch(url),false);
 assert.equal(p.remote('https://accounts.google.com/signin'),true);assert.equal(p.remote('https://example.com'),false);
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
});
