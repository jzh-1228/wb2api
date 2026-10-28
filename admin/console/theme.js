window.WBTheme = (() => {
  const key = 'wb_console_theme';
  let theme = 'light';
  try { if (localStorage.getItem(key) === 'dark') theme = 'dark'; } catch (_) {}
  function render() {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    document.querySelectorAll('[data-theme-choice]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.themeChoice === theme));
    });
  }
  function set(value) {
    theme = value === 'dark' ? 'dark' : 'light';
    try { localStorage.setItem(key, theme); } catch (_) {}
    render();
    window.dispatchEvent(new Event('wb-theme-change'));
  }
  render();
  document.addEventListener('DOMContentLoaded', render);
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) {
      theme = event.newValue === 'dark' ? 'dark' : 'light';
      render();
    }
  });
  return {set};
})();
