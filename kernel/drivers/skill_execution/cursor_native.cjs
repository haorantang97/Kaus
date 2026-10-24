'use strict';
// Read the installed native store codec without invoking the CLI entry point.
const fs=require('node:fs'), path=require('node:path'), Module=require('node:module'), crypto=require('node:crypto');
module.exports = async function(input) {
  for(const [file,digest] of Object.entries(input.modules))
    if(crypto.createHash('sha256').update(fs.readFileSync(path.join(input.package,file))).digest('hex')!==digest)
      throw Error('Cursor native store version changed');
  const original=Module.prototype._compile, index=path.join(input.package,'index.js');
  Module.prototype._compile=function(code,file){
    if(file===index){
      const anchor='var __webpack_exports__=__webpack_require__("./src/main.tsx")';
      if(code.split(anchor).length!==2)throw Error('Cursor entry requires verification');
      code=code.replace(anchor,'globalThis[Symbol.for("kaus.cursor.store")]=__webpack_require__');
    }
    return original.call(this,code,file);
  };
  require(index);
  Module.prototype._compile=original;
  const r=globalThis[Symbol.for('kaus.cursor.store')];
  await r.e(1348);
  return {require:r, Blob:r('../cursor-sdk-local-runtime/dist/run-store/sqlite-blob-store.js').M,
    Store:r('../agent-kv/dist/index.js').pH, context:r('../context/dist/core.js').q6()};
};
