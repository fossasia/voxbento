const mobileMenu = document.querySelector('#local-mobile-menu');
const mobileMenuDetails = mobileMenu?.closest('details');

mobileMenu?.addEventListener('click', (event) => {
  if (!(event.target instanceof Element) || !event.target.closest('a[href^="#"]')) return;
  if (mobileMenuDetails) mobileMenuDetails.open = false;
});
