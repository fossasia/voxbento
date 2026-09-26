const password = document.getElementById('password')
const passwordConfirm = document.getElementById('password_confirm')

function syncPasswordConfirm() {
  // Only require a confirmation when the user actually chose a password;
  // a blank password means passwordless (magic link) sign-in.
  const hasPassword = password.value !== ''
  passwordConfirm.required = hasPassword
  if (hasPassword) {
    passwordConfirm.setAttribute('minlength', '8')
  } else {
    passwordConfirm.removeAttribute('minlength')
  }
  passwordConfirm.setCustomValidity(
    hasPassword && password.value !== passwordConfirm.value ? 'Passwords do not match.' : '',
  )
}

if (password && passwordConfirm) {
  password.addEventListener('input', syncPasswordConfirm)
  passwordConfirm.addEventListener('input', syncPasswordConfirm)
  syncPasswordConfirm()
}
