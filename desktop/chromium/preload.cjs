const { contextBridge, ipcRenderer } = require('electron');
// Only the trusted Kaus main frame gets this bridge. No module or remote preload.
contextBridge.exposeInMainWorld('kausChatCutNative', Object.freeze({
  version: 1,
  send: message => ipcRenderer.invoke('chatcut:surface', message),
}));
