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
}

export function bindModelFilter(providerSelect, modelSelect, providerKey) {
  if (!providerSelect || !modelSelect) return;
  const options = [...modelSelect.options];
  const sync = () => syncModelOptions(options, providerSelect.value, providerKey);
  providerSelect.addEventListener('change', sync);
  sync();
}
