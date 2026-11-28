// Shows only the model options that belong to the chosen provider.
// Each option names its provider in a data attribute, read here as dataset[providerKey].

export function syncModelOptions(options, provider, providerKey) {
  const visible = options.filter((opt) => opt.dataset[providerKey] === provider);
  options.forEach((opt) => {
    const show = visible.includes(opt);
    opt.hidden = !show;
    opt.disabled = !show;
  });
  if (!visible.some((opt) => opt.selected) && visible.length) visible[0].selected = true;
  return visible;
}

// onSync(visible) runs after every sync, for page-specific follow-ups.
export function bindModelFilter(providerSelect, modelSelect, providerKey, onSync) {
  if (!providerSelect || !modelSelect) return;
  const options = [...modelSelect.options];
  const sync = () => {
    const visible = syncModelOptions(options, providerSelect.value, providerKey);
    if (onSync) onSync(visible);
  };
  providerSelect.addEventListener('change', sync);
  sync();
}
