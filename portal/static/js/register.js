const password = document.getElementById('password')
const passwordConfirm = document.getElementById('password_confirm')

function syncPasswordConfirm() {
  // Only require a confirmation when the user actually chose a password;
  // a blank password means passwordless (magic link) sign-in.
  passwordConfirm.required = password.value !== ''
  passwordConfirm.setCustomValidity(
    password.value !== passwordConfirm.value ? 'Passwords do not match.' : '',
  )
}

if (password && passwordConfirm) {
  password.addEventListener('input', syncPasswordConfirm)
  passwordConfirm.addEventListener('input', syncPasswordConfirm)
  syncPasswordConfirm()
}
