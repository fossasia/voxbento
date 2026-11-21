// "From a YouTube stream" on the room page (#1087, plan 007).
//
// A guide over controls that already exist, never a second copy of them: while this choice is open it
// moves the Sign in with YouTube block (#youtube-connect) and the streaming-software card
// (#program-ingest) into its own slots, and puts both back where they were when another way of hearing
// the room is chosen. Every request is still made by those controls' own scripts (CSRF token, one-time
// stream key, relay outage confirmation, write-only destination); this file makes none.

const guide = document.getElementById('youtube-source');
const ingest = document.getElementById('program-ingest');
const connect = document.getElementById('youtube-connect');
const ingestSlot = guide?.querySelector('[data-youtube-ingest-slot]');
const connectSlot = guide?.querySelector('[data-youtube-account-slot]');
const routes = guide ? [...guide.querySelectorAll('input[name="youtube-route"]')] : [];
const audioChoices = [...document.querySelectorAll('input[name="audio_choice"]')];

// The program-ingest mode each route stands for.
const routeModes = {multi: 'rtmps', forward: 'rtmps_relay'};

// Comment markers remember where each moved block lives when this guide is closed.
function placeholder(node, name) {
  if (!node) return null;
  const marker = document.createComment(`${name} home`);
  node.parentNode.insertBefore(marker, node);
  return marker;
}

const ingestHome = placeholder(ingest, 'program-ingest');
const connectHome = placeholder(connect, 'youtube-connect');
const modePicker = ingest?.querySelector('.program-mode-picker');

function guideOpen() {
  return audioChoices.find(choice => choice.checked)?.value === 'youtube';
}

function selectedRoute() {
  return routes.find(route => route.checked)?.value || '';
}

function modeRadio(mode) {
  return ingest?.querySelector(`input[name="program-mode"][value="${mode}"]`) || null;
}

function showRoutePanels(route) {
  guide.querySelectorAll('[data-route-panel]').forEach(panel => {
    panel.hidden = !panel.dataset.routePanel.split(' ').includes(route);
  });
}

// Moves the shared blocks in or out. Inside the guide the route question replaces the card's own mode
// picker; outside it the card is exactly as before.
function place() {
  const open = guideOpen();
  if (connect && connectSlot && connectHome) {
    if (open && connect.parentNode !== connectSlot) connectSlot.appendChild(connect);
    if (!open && connect.parentNode === connectSlot) connectHome.parentNode.insertBefore(connect, connectHome.nextSibling);
  }
  if (ingest && ingestSlot && ingestHome) {
    if (open && ingest.parentNode !== ingestSlot) ingestSlot.appendChild(ingest);
    if (!open && ingest.parentNode === ingestSlot) ingestHome.parentNode.insertBefore(ingest, ingestHome.nextSibling);
  }
  if (modePicker) modePicker.hidden = open;
}

// Picking a route selects the matching mode in the streaming-software card, the same as clicking it
// there: nothing changes on the server until the owner creates a stream key in that card.
function applyRoute(route) {
  showRoutePanels(route);
  const radio = modeRadio(routeModes[route]);
  if (radio && !radio.disabled && !radio.checked) {
    radio.checked = true;
    radio.dispatchEvent(new Event('change'));
  }
}

// A route the server cannot offer (operator has not enabled RTMPS ingest or forwarding) is shown but
// cannot be picked, mirroring the card's own radios, which its status poll keeps up to date.
function syncAvailability() {
  routes.forEach(route => {
    const mode = routeModes[route.value];
    if (!mode) return;
    const unavailable = Boolean(modeRadio(mode)?.disabled);
    route.disabled = unavailable;
    const note = guide.querySelector(`[data-route-unavailable="${route.value}"]`);
    if (note) note.hidden = !unavailable;
  });
}

if (guide) {
  const initial = guide.dataset.initialRoute;
  const start = routes.find(route => route.value === initial);
  if (start) start.checked = true;
  showRoutePanels(selectedRoute());

  routes.forEach(route => route.addEventListener('change', () => applyRoute(route.value)));
  audioChoices.forEach(choice => choice.addEventListener('change', place));
  place();
  syncAvailability();
  if (typeof MutationObserver === 'function') {
    const observer = new MutationObserver(syncAvailability);
    Object.values(routeModes).forEach(mode => {
      const radio = modeRadio(mode);
      if (radio) observer.observe(radio, {attributes: true, attributeFilter: ['disabled']});
    });
  }
}
