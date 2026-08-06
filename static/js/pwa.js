/* Registers the service worker and exposes window.PWA — install-prompt
 * capture, installed-state detection, and the update flow used by the
 * Download page (Settings > ... > Download App).
 */
window.PWA = (function () {
  var deferredPrompt = null;

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js').catch(function () {
        // Offline-install just isn't available this session — the rest of
        // the site works fine without it, so fail quietly.
      });
    });
  }

  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    deferredPrompt = e;
    document.dispatchEvent(new CustomEvent('pwa:installable'));
  });

  window.addEventListener('appinstalled', function () {
    localStorage.setItem('pwaInstalled', 'true');
    deferredPrompt = null;
    document.dispatchEvent(new CustomEvent('pwa:installed'));
  });

  function isStandalone() {
    return window.matchMedia('(display-mode: standalone)').matches
      || window.navigator.standalone === true; // iOS Safari
  }

  function isInstalled() {
    // display-mode only tells the truth once running IN standalone mode;
    // the localStorage flag (set on the 'appinstalled' event) is what
    // lets a later visit — still in the regular browser tab — know it's
    // already installed, per the spec's own recommendation.
    return isStandalone() || localStorage.getItem('pwaInstalled') === 'true';
  }

  function isIOS() {
    return /iphone|ipad|ipod/i.test(window.navigator.userAgent) && !window.MSStream;
  }

  function canPromptInstall() {
    return deferredPrompt !== null;
  }

  function promptInstall() {
    if (!deferredPrompt) return Promise.resolve('unavailable');
    var promptEvent = deferredPrompt;
    deferredPrompt = null;
    promptEvent.prompt();
    return promptEvent.userChoice.then(function (choice) {
      return choice.outcome; // 'accepted' | 'dismissed'
    });
  }

  // Checks the server for a new service worker. Resolves with:
  //   {status: 'up-to-date'}                          - already current
  //   {status: 'update-found', registration}           - new version installed, waiting
  //   {status: 'unsupported'}                           - no service worker at all
  function checkForUpdate() {
    if (!('serviceWorker' in navigator)) return Promise.resolve({ status: 'unsupported' });

    return navigator.serviceWorker.getRegistration().then(function (reg) {
      if (!reg) return { status: 'unsupported' };
      if (reg.waiting) return { status: 'update-found', registration: reg };

      return new Promise(function (resolve) {
        var settled = false;
        function finish(result) {
          if (settled) return;
          settled = true;
          resolve(result);
        }

        reg.addEventListener('updatefound', function () {
          var newWorker = reg.installing;
          if (!newWorker) return;
          newWorker.addEventListener('statechange', function () {
            if (newWorker.state === 'installed') {
              finish({ status: 'update-found', registration: reg });
            }
          });
        });

        reg.update().then(function () {
          setTimeout(function () { finish({ status: 'up-to-date' }); }, 4000);
        }).catch(function () {
          finish({ status: 'error' });
        });
      });
    });
  }

  // Activates a waiting service worker (from checkForUpdate's result) and
  // reloads once it takes control.
  function activateUpdate(registration) {
    if (!registration || !registration.waiting) return;
    registration.waiting.postMessage({ type: 'SKIP_WAITING' });
    navigator.serviceWorker.addEventListener('controllerchange', function () {
      window.location.reload();
    });
  }

  return {
    isInstalled: isInstalled,
    isIOS: isIOS,
    canPromptInstall: canPromptInstall,
    promptInstall: promptInstall,
    checkForUpdate: checkForUpdate,
    activateUpdate: activateUpdate,
  };
})();
