// Offers only the translation services that can translate a room's languages.
//
// `support` maps a service to the language codes it translates; a service that is not in
// it translates anything. `needed` is the language spoken plus every language translated into.

export function translatesAll(support, provider, needed) {
  const codes = support[provider];
  return !codes || needed.every((code) => codes.includes(code));
}

// Hides the options that don't fit and moves the choice off a hidden one.
// When nothing fits, the options are left as they are: saving says which language is the problem.
export function syncTranslationOptions(select, support, needed) {
  const options = [...select.options];
  const fitting = options.filter((option) => translatesAll(support, option.value, needed));
  options.forEach((option) => {
    const offered = !fitting.length || fitting.includes(option);
    option.hidden = !offered;
    option.disabled = !offered;
  });
  if (fitting.length && !fitting.includes(select.selectedOptions[0])) select.value = fitting[0].value;
}
