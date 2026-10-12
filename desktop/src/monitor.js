'use strict';
/**
 * Is the GerlynPLUS server actually reachable?
 *
 * Like the web app's own check (static/js/offline-sync.js) this asks the app
 * itself -- GET /offline/ping/ -- rather than trusting that a network adapter
 * is up. The answer only steers *performance* decisions in this shell (serve
 * bundled assets immediately instead of waiting out a timeout); it never
 * decides whether a change was saved. That is the server's `applied` answer,
 * handled entirely by the page's offline queue.
 */
const PING_PATH = '/offline/ping/';

class ConnectivityMonitor {
  /**
   * @param {object} opts
   * @param {() => string} opts.getOrigin
   * @param {(url: string, init: object) => Promise<Response>} opts.fetchNetwork
   * @param {(online: boolean) => void} [opts.onChange]
   */
  constructor({
    getOrigin, fetchNetwork, onChange, onlineIntervalMs = 30000, offlineIntervalMs = 8000, timeoutMs = 4000,
  }) {
    this.getOrigin = getOrigin;
    this.fetchNetwork = fetchNetwork;
    this.onChange = onChange || (() => {});
    this.onlineIntervalMs = onlineIntervalMs;
    this.offlineIntervalMs = offlineIntervalMs;
    this.timeoutMs = timeoutMs;
    this.online = null; // unknown until the first probe finishes
    this._timer = null;
    this._running = false;
    this._inflight = null;
  }

  isOnline() {
    return this.online;
  }

  probe() {
    if (this._inflight) return this._inflight;
    this._inflight = this._probeOnce().finally(() => { this._inflight = null; });
    return this._inflight;
  }

  async _probeOnce() {
    let ok = false;
    try {
      const res = await this.fetchNetwork(`${this.getOrigin()}${PING_PATH}?t=${Date.now()}`, {
        bypassCustomProtocolHandlers: true,
        cache: 'no-store',
        signal: AbortSignal.timeout(this.timeoutMs),
        headers: { Accept: 'application/json' },
      });
      const body = res.ok ? await res.json() : null;
      ok = !!(body && body.ok === true);
    } catch (e) {
      ok = false;
    }
    if (ok !== this.online) {
      this.online = ok;
      try { this.onChange(ok); } catch (e) { /* listener fault must not stop probing */ }
    }
    return ok;
  }

  start() {
    if (this._running) return;
    this._running = true;
    const tick = async () => {
      if (!this._running) return;
      await this.probe();
      if (!this._running) return;
      this._timer = setTimeout(tick, this.online ? this.onlineIntervalMs : this.offlineIntervalMs);
    };
    tick();
  }

  stop() {
    this._running = false;
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
  }
}

module.exports = { ConnectivityMonitor, PING_PATH };
