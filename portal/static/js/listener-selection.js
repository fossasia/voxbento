// Pure helpers behind the listener page's room/language deep links, kept apart
// from the DOM so they can be unit tested with node (see tests/test_listener_selection.js).

/**
 * The room to open with: the one named by `wantedRoom` when it exists, else the
 * event's only room, else none. Unknown ids fall back rather than erroring, so a
 * stale link still lands on the normal dropdowns.
 *
 * @param {Array<{id: number|string}>} roomsData
 * @param {string|null} wantedRoom
 * @returns {string|null} the room id as a string, or null to leave unselected
 */
export function resolveRoomId(roomsData, wantedRoom) {
  if (!Array.isArray(roomsData) || roomsData.length === 0) return null;
  if (wantedRoom) {
    var match = roomsData.find(function (r) {
      return String(r.id) === String(wantedRoom);
    });
    if (match) return String(match.id);
  }
  if (roomsData.length === 1) return String(roomsData[0].id);
  return null;
}

/**
 * `href` with `room`/`lang` set to the current selection. Every other parameter
 * is preserved, notably the `code` join code.
 *
 * @param {string} href
 * @param {string|number|null} roomId
 * @param {string|null} langCode
 * @returns {string}
 */
export function buildSelectionUrl(href, roomId, langCode) {
  var url = new URL(href);
  if (roomId) {
    url.searchParams.set("room", String(roomId));
  } else {
    url.searchParams.delete("room");
  }
  if (roomId && langCode) {
    url.searchParams.set("lang", String(langCode));
  } else {
    url.searchParams.delete("lang");
  }
  return url.toString();
}
