(function () {
  var root = document.documentElement;
  var meta = document.querySelector('meta[name="theme-color"]');
  var colors = { light: '#fdfcf8', dark: '#23211c' };

  function apply(next) {
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('aver-theme', next); } catch (e) {}
    if (meta) meta.setAttribute('content', colors[next] || colors.light);
  }

  // Sync the theme-color meta with whatever the inline head script picked,
  // since that script ran before this file loaded.
  if (meta) meta.setAttribute('content', colors[root.getAttribute('data-theme')] || colors.light);

  document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      apply(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
    });
  });
})();
