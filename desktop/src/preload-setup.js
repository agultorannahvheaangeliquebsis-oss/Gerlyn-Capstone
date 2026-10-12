'use strict';
// Used only by the local setup page (src/pages/setup.html). The main process
// also checks the sender, so this bridge is useless to any other page.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('gerlynSetup', Object.freeze({
  info: () => ipcRenderer.invoke('desktop:server-info'),
  save: (url, force) => ipcRenderer.invoke('desktop:save-server', { url, force: !!force }),
  cancel: () => ipcRenderer.invoke('desktop:cancel-setup'),
}));
