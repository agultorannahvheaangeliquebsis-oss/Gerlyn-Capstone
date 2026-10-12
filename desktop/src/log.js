'use strict';
/** Minimal size-capped file log under userData/logs. Never logs page data. */
const fs = require('fs');
const path = require('path');

const MAX_BYTES = 1024 * 1024;

function createLogger(dir) {
  let file = null;
  try {
    fs.mkdirSync(dir, { recursive: true });
    file = path.join(dir, 'desktop.log');
    if (fs.existsSync(file) && fs.statSync(file).size > MAX_BYTES) {
      fs.renameSync(file, path.join(dir, 'desktop.previous.log'));
    }
  } catch (e) {
    file = null;
  }

  function write(level, message) {
    const line = `${new Date().toISOString()} ${level} ${message}\n`;
    if (level === 'ERROR') process.stderr.write(line);
    if (!file) return;
    try { fs.appendFileSync(file, line); } catch (e) { /* disk full / locked: ignore */ }
  }

  return {
    file,
    info: (message) => write('INFO', message),
    warn: (message) => write('WARN', message),
    error: (message) => write('ERROR', message),
  };
}

module.exports = { createLogger };
