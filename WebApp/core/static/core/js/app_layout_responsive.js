// Responsive side menu toggle
(function() {
  var burger = document.getElementById('burgerMenuBtn');
  var sideMenu = document.getElementById('sideMenu');
  var overlay = document.getElementById('sideMenuOverlay');
  var closeBtn = document.getElementById('closeMenuBtn');

  function openMenu() {
    sideMenu.classList.add('open');
    overlay.classList.add('open');
    burger.style.display = 'none';
  }
  function closeMenu() {
    sideMenu.classList.remove('open');
    overlay.classList.remove('open');
    burger.style.display = '';
  }

  burger && burger.addEventListener('click', function(e) {
    e.preventDefault();
    openMenu();
  });
  overlay && overlay.addEventListener('click', closeMenu);
  closeBtn && closeBtn.addEventListener('click', closeMenu);

  // Optional: close menu on ESC
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeMenu();
  });
})();
