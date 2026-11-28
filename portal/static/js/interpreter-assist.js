/**
 * Interpreter Assist (#1375): the booth's glossary and the room's live floor transcript,
 * with each glossary term marked and its translation shown next to it.
 *
 * The server sends both over the booth socket:
 *   booth:glossary       { terms: [{ id, source_term, target_term, description }] }
 *   booth:floor-caption  { status: 'partial' | 'final' | 'clear', text, hits: [{ start, end, term_id, target_term }] }
 * Hit offsets are UTF-16 code units, the way JavaScript strings are indexed.
 *
 * Everything is written with textContent: captions and glossary entries are never parsed as HTML.
 */

/** Final floor lines kept on screen. */
export const MAX_FLOOR_LINES = 8

/**
 * Cut caption text into plain and highlighted pieces.
 * Hits that are out of range, empty or overlap an earlier one are ignored, so a malformed
 * message can never duplicate or drop text.
 * @param {string} text
 * @param {Array<{start: number, end: number, target_term?: string, term_id?: number}>} hits
 * @returns {Array<{text: string, hit: object|null}>}
 */
export function splitCaption(text, hits) {
  const source = typeof text === 'string' ? text : ''
  const valid = (Array.isArray(hits) ? hits : [])
    .filter((hit) => hit && Number.isInteger(hit.start) && Number.isInteger(hit.end)
      && hit.start >= 0 && hit.end <= source.length && hit.start < hit.end)
    .sort((a, b) => a.start - b.start || b.end - a.end)
  const pieces = []
  let cursor = 0
  for (const hit of valid) {
    if (hit.start < cursor) continue
    if (hit.start > cursor) pieces.push({ text: source.slice(cursor, hit.start), hit: null })
    pieces.push({ text: source.slice(hit.start, hit.end), hit })
    cursor = hit.end
  }
  if (cursor < source.length) pieces.push({ text: source.slice(cursor), hit: null })
  return pieces
}

/**
 * Glossary entries whose source term, target term or note contains every word of the query.
 * @param {Array<{source_term: string, target_term: string, description?: string|null}>} terms
 * @param {string} query
 */
export function filterTerms(terms, query) {
  const list = Array.isArray(terms) ? terms : []
  const words = String(query || '').toLocaleLowerCase().split(/\s+/).filter(Boolean)
  if (!words.length) return list
  return list.filter((term) => {
    const haystack = [term.source_term, term.target_term, term.description || '']
      .join('\n')
      .toLocaleLowerCase()
    return words.every((word) => haystack.includes(word))
  })
}

/**
 * The floor transcript as data: finished lines plus the line still being spoken.
 * @param {number} maxLines
 */
export function createFloorTranscript(maxLines = MAX_FLOOR_LINES) {
  let lines = []
  let partial = null
  const seen = new Set()

  return {
    /** Apply one booth:floor-caption message. Returns true when the transcript changed. */
    apply(message) {
      if (!message || typeof message !== 'object') return false
      if (message.status === 'clear') {
        if (!partial) return false
        partial = null
        return true
      }
      if (typeof message.text !== 'string' || !message.text.trim()) return false
      const line = { text: message.text, hits: Array.isArray(message.hits) ? message.hits : [] }
      const key = typeof message.segment_id === 'string' ? message.segment_id : null
      if (message.status === 'partial') {
        // A partial that arrives after its own final would linger under it.
        if (key && seen.has(key)) return false
        partial = line
        return true
      }
      if (message.status !== 'final') return false
      // A reconnect can deliver the same final twice.
      if (key && seen.has(key)) return false
      if (key) seen.add(key)
      lines = [...lines, line].slice(-maxLines)
      if (seen.size > maxLines * 4) {
        // Only recent segments can arrive again.
        const recent = [...seen].slice(-maxLines * 2)
        seen.clear()
        recent.forEach((id) => seen.add(id))
      }
      partial = null
      return true
    },
    lines: () => lines,
    partial: () => partial,
    reset() {
      lines = []
      partial = null
      seen.clear()
    },
  }
}

function renderLine(doc, line, className) {
  const p = doc.createElement('p')
  p.className = className
  for (const piece of splitCaption(line.text, line.hits)) {
    if (!piece.hit) {
      p.appendChild(doc.createTextNode(piece.text))
      continue
    }
    const mark = doc.createElement('mark')
    mark.className = 'assist-hit'
    mark.appendChild(doc.createTextNode(piece.text))
    if (piece.hit.target_term) {
      const target = doc.createElement('span')
      target.className = 'assist-hit-target'
      target.textContent = piece.hit.target_term
      mark.appendChild(target)
      mark.title = `${piece.text} → ${piece.hit.target_term}`
    }
    p.appendChild(mark)
  }
  return p
}

/**
 * Bind the panel to its elements. Missing elements turn the panel off without breaking the booth.
 * @param {Document} doc
 */
export function createInterpreterAssist(doc = document) {
  const el = {
    count: doc.getElementById('assist-term-count'),
    floorLog: doc.getElementById('assist-floor-log'),
    floorEmpty: doc.getElementById('assist-floor-empty'),
    search: doc.getElementById('assist-search'),
    glossary: doc.getElementById('assist-glossary'),
    glossaryEmpty: doc.getElementById('assist-glossary-empty'),
  }
  const enabled = Boolean(el.floorLog && el.glossary)
  const transcript = createFloorTranscript()
  let terms = []

  function renderGlossary() {
    if (!enabled) return
    const query = el.search ? el.search.value : ''
    const visible = filterTerms(terms, query)
    el.glossary.replaceChildren(...visible.map((term) => {
      const item = doc.createElement('li')
      item.className = 'assist-term'
      const source = doc.createElement('span')
      source.className = 'assist-term-source'
      source.textContent = term.source_term
      const target = doc.createElement('span')
      target.className = 'assist-term-target'
      target.textContent = term.target_term
      item.append(source, target)
      if (term.description) {
        const note = doc.createElement('span')
        note.className = 'assist-term-note'
        note.textContent = term.description
        item.appendChild(note)
      }
      return item
    }))
    if (el.count) el.count.textContent = String(terms.length)
    if (el.glossaryEmpty) {
      el.glossaryEmpty.textContent = terms.length ? 'No term matches your search.' : 'No glossary for this booth yet.'
      el.glossaryEmpty.classList.toggle('hidden', visible.length > 0)
    }
  }

  function renderFloor() {
    if (!enabled) return
    // Follow the newest line unless the interpreter scrolled up to read something.
    const atBottom = el.floorLog.scrollHeight - el.floorLog.scrollTop - el.floorLog.clientHeight < 24
    const nodes = transcript.lines().map((line) => renderLine(doc, line, 'assist-line'))
    const partial = transcript.partial()
    if (partial) nodes.push(renderLine(doc, partial, 'assist-line assist-line--partial'))
    el.floorLog.replaceChildren(...nodes)
    if (el.floorEmpty) el.floorEmpty.classList.toggle('hidden', nodes.length > 0)
    if (atBottom) el.floorLog.scrollTop = el.floorLog.scrollHeight
  }

  if (el.search) el.search.addEventListener('input', renderGlossary)
  renderGlossary()

  return {
    setGlossary(message) {
      terms = Array.isArray(message?.terms) ? message.terms : []
      renderGlossary()
    },
    handleFloorCaption(message) {
      if (transcript.apply(message)) renderFloor()
    },
  }
}
