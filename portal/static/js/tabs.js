// Tabs for an admin page. A button marked data-<name>-tab opens the panel marked
// data-<name>-panel with the same value; the bar itself is marked data-<name>-tabs.
//
// Without JavaScript every panel shows in order and the bar stays hidden. The open tab
// is remembered per page, so saving a setting (which reloads the page) returns to the
// same tab; a #hash in the link wins, so a redirect can choose the tab.

export function initTabs({ name, storageKey }) {
  const tabList = document.querySelector(`[data-${name}-tabs]`);
  if (!tabList) return;
  const tabAttr = `data-${name}-tab`;
  const tabs = [...tabList.querySelectorAll(`[${tabAttr}]`)];
  const panels = [...document.querySelectorAll(`[data-${name}-panel]`)];
  const tabName = (tab) => tab.getAttribute(tabAttr);

  function show(wanted, { focus = false } = {}) {
    const target = tabs.find((tab) => tabName(tab) === wanted) || tabs[0];
    tabs.forEach((tab) => {
      const selected = tab === target;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      tab.classList.toggle('is-active', selected);
    });
    panels.forEach((panel) => { panel.hidden = panel.getAttribute(`data-${name}-panel`) !== tabName(target); });
    if (focus) target.focus();
    try { sessionStorage.setItem(storageKey, tabName(target)); } catch (err) { console.debug('Tab not remembered', err); }
    if (location.hash !== `#${tabName(target)}`) {
      history.replaceState(null, '', `${location.pathname}${location.search}#${tabName(target)}`);
    }
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => show(tabName(tab)));
    tab.addEventListener('keydown', (e) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (e.key !== 'Home' && e.key !== 'End' && !step) return;
      e.preventDefault();
      const next = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : (index + step + tabs.length) % tabs.length;
      show(tabName(tabs[next]), { focus: true });
    });
  });

  // Links elsewhere on the page (e.g. a checklist) can open a tab.
  document.querySelectorAll(`[data-${name}-tab-link]`).forEach((link) => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      show(link.getAttribute(`data-${name}-tab-link`), { focus: true });
    });
  });

  let initial = location.hash.slice(1);
  if (!initial) {
    try { initial = sessionStorage.getItem(storageKey) || ''; } catch (err) { console.debug('No remembered tab', err); }
  }
  tabList.hidden = false;
  show(initial);
}
