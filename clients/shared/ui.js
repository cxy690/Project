(() => {
  "use strict";
  const visible = (el) => el && !el.closest('[hidden], .hide') && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const focusable = (root) => [...root.querySelectorAll('a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])')].filter(visible);
  const main = document.querySelector('main, #panel-auth, .card');
  if (main) {
    if (!main.id) main.id = 'main-content';
    const skip = document.createElement('a');
    skip.className = 'skip-link';
    skip.href = '#' + main.id;
    skip.textContent = '跳到主要内容';
    skip.addEventListener('click', () => {
      if (!main.hasAttribute('tabindex')) main.tabIndex = -1;
      main.focus();
    });
    document.body.prepend(skip);
  }

  let active = null;
  const history = new WeakMap();
  const sync = () => {
    const dialogs = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"]')].filter(visible);
    const next = dialogs[dialogs.length - 1] || null;
    if (next !== active) {
      const previous = active;
      active = next;
      if (next) {
        if (!history.has(next)) history.set(next, document.activeElement);
        if (!next.contains(document.activeElement)) {
          if (!next.hasAttribute('tabindex')) next.tabIndex = -1;
          (focusable(next)[0] || next).focus({ preventScroll: true });
        }
      } else if (previous) {
        const origin = history.get(previous);
        if (visible(origin)) origin.focus({ preventScroll: true });
      }
      if (previous && !visible(previous)) history.delete(previous);
    }
  };
  document.addEventListener('keydown', (event) => {
    if (!active || !visible(active)) return;
    if (event.key === 'Tab') {
      const items = focusable(active);
      const first = items[0] || active;
      const last = items[items.length - 1] || active;
      if (event.shiftKey && (document.activeElement === first || !active.contains(document.activeElement))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !active.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    }
    if (event.key === 'Escape' && !event.defaultPrevented) {
      const close = active.querySelector('[data-dialog-close], [aria-label="关闭"], .modal-close, .close');
      if (visible(close)) close.click();
    }
  });
  new MutationObserver(sync).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['hidden', 'class', 'style', 'aria-modal'] });
  sync();
})();
