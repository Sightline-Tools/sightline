(function () {
  function setDebugVisible(enabled) {
    document.querySelectorAll('[data-debug-only]').forEach((element) => {
      element.hidden = !enabled;
      if (enabled) {
        element.style.removeProperty('display');
        element.removeAttribute('aria-hidden');
        if (element.matches('a[href^="/replay"]')) {
          element.href = `/replay?debug=${Date.now()}`;
        }
      } else {
        element.style.setProperty('display', 'none', 'important');
        element.setAttribute('aria-hidden', 'true');
        if (element.matches('a[href^="/replay"]')) {
          element.href = '/replay';
        }
      }
    });
  }

  async function refresh() {
    setDebugVisible(false);
    try {
      const response = await fetch('/api/app-info', {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) {
        setDebugVisible(false);
        return;
      }
      const info = await response.json();
      setDebugVisible(Boolean(info.debug_mode));
      document.querySelectorAll('[data-sightline-version]').forEach((element) => {
        element.textContent = info.version;
      });
      document.querySelectorAll('[data-sightline-commit]').forEach((element) => {
        element.textContent = info.build_commit;
        element.href = info.build_commit_url;
      });
      document.querySelectorAll('[data-sightline-source]').forEach((element) => {
        element.href = info.source_url;
      });
    } catch (_error) {
      setDebugVisible(false);
    }
  }

  window.SightlineShell = {refresh, setDebugVisible};
  function addProductFooter() {
    if (document.querySelector('.sightline-footer')) return;
    const footer = document.createElement('footer');
    footer.className = 'sightline-footer';
    footer.textContent = 'Sightline is a local-first combat parser. Capture and processing stay on this computer unless you explicitly export data.';
    document.body.appendChild(footer);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { addProductFooter(); refresh(); }, {once: true});
  } else {
    addProductFooter();
    refresh();
  }
}());
