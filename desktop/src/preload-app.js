'use strict';
// Runs in the pages served by the GerlynPLUS server. It deliberately exposes
// read-only facts and nothing that can change the app's configuration or touch
// the file system: those pages are remote content.
const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('gerlynDesktop', Object.freeze({
  isDesktop: true,
  platform: process.platform,
}));
