// Sidebar redesign — standalone demo behavior.
// Two concerns: (1) collapse/expand the whole rail, (2) expand/collapse
// individual tree branches. Both persist across reloads via localStorage so
// the demo feels like a real app component, not a toy.
(function () {
  var sb = document.getElementById('sb');
  var toggle = document.getElementById('sbToggle');
  var log = document.getElementById('demoLog');
  if (!sb) return;

  var STATE_KEY = 'sb-demo-state';
  var TREE_KEY = 'sb-demo-open-trees';

  function logEvent(msg) {
    if (!log) return;
    var line = document.createElement('div');
    line.textContent = msg;
    log.prepend(line);
    while (log.children.length > 6) log.removeChild(log.lastChild);
  }

  // ---------------------------------------------------------- rail collapse
  function setState(state) {
    sb.setAttribute('data-state', state);
    toggle.setAttribute('aria-expanded', state === 'expanded' ? 'true' : 'false');
    toggle.setAttribute('aria-label', state === 'expanded' ? 'Collapse sidebar' : 'Expand sidebar');
    toggle.title = toggle.getAttribute('aria-label');
    try { localStorage.setItem(STATE_KEY, state); } catch (e) {}
  }

  var savedState = 'expanded';
  try { savedState = localStorage.getItem(STATE_KEY) || 'expanded'; } catch (e) {}
  setState(savedState);

  toggle.addEventListener('click', function () {
    var next = sb.getAttribute('data-state') === 'expanded' ? 'collapsed' : 'expanded';
    setState(next);
    logEvent('Sidebar ' + next);
  });

  // -------------------------------------------------------------- file tree
  var openTrees = {};
  try { openTrees = JSON.parse(localStorage.getItem(TREE_KEY)) || {}; } catch (e) {}

  function applyTree(btn, open) {
    var targetId = btn.getAttribute('data-target');
    var branch = document.getElementById(targetId);
    if (!branch) return;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    branch.classList.toggle('open', open);
  }

  var treeToggles = document.querySelectorAll('.sb-tree-toggle');
  treeToggles.forEach(function (btn) {
    var id = btn.getAttribute('data-target');
    applyTree(btn, openTrees[id] === true || btn.getAttribute('aria-expanded') === 'true');

    btn.addEventListener('click', function () {
      // A collapsed rail has nowhere to show a branch — expand the whole
      // sidebar first, same pattern as the icon-strip toggle in gerlyn.css.
      if (sb.getAttribute('data-state') === 'collapsed') {
        setState('expanded');
        logEvent('Sidebar expanded (via tree item)');
      }
      var nowOpen = btn.getAttribute('aria-expanded') !== 'true';
      applyTree(btn, nowOpen);
      openTrees[id] = nowOpen;
      try { localStorage.setItem(TREE_KEY, JSON.stringify(openTrees)); } catch (e) {}
      logEvent((nowOpen ? 'Opened' : 'Closed') + ' folder "' + btn.querySelector('.sb-label').textContent + '"');
    });
  });

  // Plain nav items / files: demo has no real destinations, just log clicks.
  document.querySelectorAll('.sb-item, .sb-tree-file').forEach(function (el) {
    el.addEventListener('click', function (e) {
      e.preventDefault();
      document.querySelectorAll('.sb-item--active').forEach(function (a) { a.classList.remove('sb-item--active'); });
      document.querySelectorAll('.sb-tree-file--active').forEach(function (a) { a.classList.remove('sb-tree-file--active'); });
      if (el.classList.contains('sb-item')) el.classList.add('sb-item--active');
      if (el.classList.contains('sb-tree-file')) el.classList.add('sb-tree-file--active');
      logEvent('Selected "' + el.querySelector('.sb-label').textContent + '"');
    });
  });
})();
