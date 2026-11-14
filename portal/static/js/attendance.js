// Interpreter attendance page (#1215): local times, day tabs in the viewer's time zone,
// instant search, sortable columns and an auto-applied booth filter.
// The server renders a complete page in UTC, so it still reads correctly without JavaScript.

const TZ_COOKIE = 'vb_tz'

const sameYear = (date) => date.getFullYear() === new Date().getFullYear()

export function formatLocalTime(date, locale) {
  return new Intl.DateTimeFormat(locale, {
    month: 'short',
    day: 'numeric',
    ...(sameYear(date) ? {} : { year: 'numeric' }),
    hour: 'numeric',
    minute: '2-digit',
  }).format(date)
}

/** Compare two cell values for sorting: numbers numerically, text by locale. */
export function compareValues(a, b, type) {
  if (type === 'number') return Number(a) - Number(b)
  return String(a).localeCompare(String(b), undefined, { sensitivity: 'base' })
}

/** True when the row's search text contains every word of the query. */
export function matchesSearch(haystack, query) {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  return words.every((word) => haystack.includes(word))
}

function readCookie(name) {
  const match = document.cookie.split('; ').find((part) => part.startsWith(`${name}=`))
  return match ? decodeURIComponent(match.slice(name.length + 1)) : ''
}

/** Days start at the viewer's midnight: tell the server the time zone once, then reload. */
function syncTimeZone() {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone
  if (!zone || readCookie(TZ_COOKIE) === zone) return false
  document.cookie = `${TZ_COOKIE}=${encodeURIComponent(zone)}; path=/; max-age=31536000; samesite=lax`
  location.reload()
  return true
}

function localizeTimes() {
  for (const el of document.querySelectorAll('time[data-local-time]')) {
    const date = new Date(el.getAttribute('datetime'))
    if (Number.isNaN(date.getTime())) continue
    el.textContent = formatLocalTime(date)
    el.title = date.toUTCString()
  }
  const zone = document.getElementById('att-timezone')
  const name = Intl.DateTimeFormat().resolvedOptions().timeZone
  if (zone && name) zone.textContent = `your time zone (${name.replace(/_/g, ' ')})`
}

function setupFilters() {
  const form = document.querySelector('[data-attendance-filters]')
  if (!form) return
  form.querySelectorAll('[data-auto-submit]').forEach((select) => {
    select.addEventListener('change', () => form.requestSubmit())
  })

  const search = form.querySelector('[data-attendance-search]')
  const count = form.querySelector('[data-attendance-count]')
  const rows = [...document.querySelectorAll('tbody tr[data-search]')]
  const empty = document.querySelector('[data-attendance-empty]')
  if (!search) return
  // Search filters in place; Enter must not submit the form and lose it.
  search.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') event.preventDefault()
  })
  search.addEventListener('input', () => {
    let shown = 0
    for (const row of rows) {
      row.hidden = !matchesSearch(row.dataset.search, search.value)
      if (!row.hidden) shown++
    }
    if (empty) empty.hidden = shown > 0
    if (count) count.textContent = `${shown} interpreter${shown === 1 ? '' : 's'}`
  })
}

function setupSorting() {
  const table = document.querySelector('.att-table')
  if (!table) return
  const headers = [...table.querySelectorAll('th[data-sort]')]
  const body = table.tBodies[0]
  for (const th of headers) {
    const index = [...th.parentElement.children].indexOf(th)
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'att-sort'
    button.append(...th.childNodes)
    th.append(button)
    button.addEventListener('click', () => {
      const ascending = th.getAttribute('aria-sort') !== 'ascending'
      for (const other of headers) other.setAttribute('aria-sort', 'none')
      th.setAttribute('aria-sort', ascending ? 'ascending' : 'descending')
      const rows = [...body.querySelectorAll('tr[data-search]')]
      rows.sort((a, b) => {
        const order = compareValues(a.cells[index].dataset.value, b.cells[index].dataset.value, th.dataset.sort)
        return ascending ? order : -order
      })
      body.prepend(...rows)
    })
  }
}

if (typeof document !== 'undefined' && !syncTimeZone()) {
  localizeTimes()
  setupFilters()
  setupSorting()
}
