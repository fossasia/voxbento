/**
 * Mission Control's "Floor language" picker, one per room (#1397).
 *
 * Tells the server which language is spoken on the floor right now, so floor captions and AI
 * translation follow a panel that switches language. A two-way booth outputting into the
 * spoken language overrides it on the server while it does; the picker then shows that booth's
 * language and is disabled until the booth switches back (``label.setHeldBy``).
 */

/**
 * @param {{id:number, floor_language:{value:string, choices:{code:string, name:string}[],
 *   manual:string, held_by:{code:string, name:string}|null}}} room
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
  const hint = doc.createElement('span');
  hint.className = 'mc-muted';
  label.append(select, hint);

  let saved = select.value;
  let held = null;
  /** A two-way booth took the floor ({code, name}) or gave it back (null). */
  label.setHeldBy = (holder) => {
    held = holder && holder.code ? holder : null;
    if (held && !Array.from(select.children).some((option) => option.value === held.code)) {
      const option = doc.createElement('option');
      option.value = held.code;
      option.textContent = held.name;
      select.appendChild(option);
    }
    select.value = held ? held.code : saved;
    select.disabled = Boolean(held);
    hint.textContent = held ? ` Set by the ${held.name} booth while it interprets into the spoken language` : '';
  };
  if (room.floor_language.held_by) {
    // The server's value is the booth's language; the Mission Control choice comes back after.
    saved = room.floor_language.manual;
    label.setHeldBy(room.floor_language.held_by);
  }
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
      select.disabled = Boolean(held);
    }
  });
  return label;
}
