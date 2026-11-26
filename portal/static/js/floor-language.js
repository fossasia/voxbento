/**
 * Mission Control's "Floor language" picker, one per room (#1397).
 *
 * Tells the server which language is spoken on the floor right now, so floor captions and AI
 * translation follow a panel that switches language. A two-way booth outputting into the
 * spoken language overrides it on the server while it does.
 */

/**
 * @param {{id:number, floor_language:{value:string, choices:{code:string, name:string}[]}}} room
 * @param {{doc?:Document, fetchImpl?:typeof fetch, notify?:(message:string) => void}} [deps]
 * @returns {HTMLLabelElement}
 */
export function renderFloorLanguage(room, { doc = document, fetchImpl = fetch, notify = () => {} } = {}) {
  const label = doc.createElement('label');
  label.className = 'mc-captions-label';
  label.append(doc.createTextNode('Floor language '));
  const select = doc.createElement('select');
  select.className = 'role-select';
  select.id = `mc-floor-language-${room.id}`;
  room.floor_language.choices.forEach(({ code, name }) => {
    const option = doc.createElement('option');
    option.value = code;
    option.textContent = name;
    select.appendChild(option);
  });
  select.value = room.floor_language.value;
  label.appendChild(select);

  let saved = select.value;
  select.addEventListener('change', async () => {
    const wanted = select.value;
    select.disabled = true;
    try {
      const res = await fetchImpl(`/api/rooms/${room.id}/floor-language`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': doc.querySelector('meta[name="csrf-token"]')?.content || '',
        },
        body: JSON.stringify({ language_code: wanted }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`);
      }
      saved = wanted;
    } catch (err) {
      select.value = saved;
      notify(`Couldn't change the floor language. ${err.message}`);
      console.debug('Floor language request failed', err);
    } finally {
      select.disabled = false;
    }
  });
  return label;
}
