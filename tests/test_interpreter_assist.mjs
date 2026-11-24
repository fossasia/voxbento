/**
 * Unit tests for the Interpreter Assist panel (portal/static/js/interpreter-assist.js).
 *
 * Run with: node tests/test_interpreter_assist.mjs
 */
import {
  MAX_FLOOR_LINES,
  createFloorTranscript,
  createInterpreterAssist,
  filterTerms,
  splitCaption,
} from '../portal/static/js/interpreter-assist.js'

let passed = 0
let failed = 0

function assert(condition, message) {
  if (condition) {
    console.log('  PASS: ' + message)
    passed++
  } else {
    console.error('  FAIL: ' + message)
    failed++
  }
}

const join = (pieces) => pieces.map((p) => p.text).join('')

// ── splitCaption ──────────────────────────────────────────────────────────
{
  const text = 'After the keynote we deploy Kubernetes.'
  const pieces = splitCaption(text, [
    { start: 28, end: 38, target_term: 'K8s' },
    { start: 10, end: 17, target_term: 'discours' },
  ])
  assert(join(pieces) === text, 'pieces put back together give the original text')
  assert(pieces.filter((p) => p.hit).map((p) => p.text).join('|') === 'keynote|Kubernetes', 'hits are cut in text order')

  const bad = splitCaption('Hello world', [
    { start: 0, end: 5 },
    { start: 3, end: 8 }, // overlaps the first
    { start: 6, end: 99 }, // past the end
    { start: 4, end: 4 }, // empty
    { start: 1.5, end: 3 }, // not an integer
    null,
  ])
  assert(join(bad) === 'Hello world', 'malformed hits never duplicate or drop text')
  assert(bad.filter((p) => p.hit).length === 1, 'only the valid, non-overlapping hit is marked')

  const emoji = '🎤 PyCon'
  const emojiPieces = splitCaption(emoji, [{ start: 3, end: 8 }])
  assert(emojiPieces[1].text === 'PyCon', 'UTF-16 offsets from the server slice text after an emoji correctly')
  assert(splitCaption(undefined, undefined).length === 0, 'missing text gives no pieces')
}

// ── filterTerms ───────────────────────────────────────────────────────────
{
  const terms = [
    { source_term: 'Keynote', target_term: 'Hauptvortrag', description: 'Opening talk' },
    { source_term: 'Cloud', target_term: 'Wolke', description: null },
    { source_term: 'Machine learning', target_term: 'Maschinelles Lernen' },
  ]
  assert(filterTerms(terms, '').length === 3, 'an empty search shows every term')
  assert(filterTerms(terms, 'WOLKE')[0].source_term === 'Cloud', 'search matches the target term, ignoring case')
  assert(filterTerms(terms, 'opening')[0].source_term === 'Keynote', 'search matches the note')
  assert(filterTerms(terms, 'machine lernen').length === 1, 'every word of the search must match')
  assert(filterTerms(terms, 'nothing').length === 0, 'no match gives no terms')
  assert(filterTerms(null, 'x').length === 0, 'a missing list is treated as empty')
}

// ── createFloorTranscript ─────────────────────────────────────────────────
{
  const t = createFloorTranscript(3)
  assert(t.apply({ status: 'partial', text: 'Hel' }) && t.partial().text === 'Hel', 'a partial is shown')
  assert(t.apply({ status: 'clear' }) && t.partial() === null, 'clear removes the partial')
  assert(!t.apply({ status: 'clear' }), 'a second clear changes nothing')

  t.apply({ status: 'partial', text: 'One' })
  t.apply({ status: 'final', text: 'One.', segment_id: 'a' })
  assert(t.partial() === null && t.lines().length === 1, 'a final replaces the partial')
  assert(!t.apply({ status: 'final', text: 'One.', segment_id: 'a' }), 'the same final segment is not added twice')

  for (const id of ['b', 'c', 'd']) t.apply({ status: 'final', text: id, segment_id: id })
  assert(t.lines().map((l) => l.text).join(',') === 'b,c,d', 'only the newest lines are kept')
  assert(!t.apply({ status: 'final', text: '  ' }), 'an empty final is ignored')
  assert(!t.apply({ status: 'translated', text: 'x' }), 'an unknown status is ignored')
  assert(!t.apply(null), 'a missing message is ignored')
  const defaults = createFloorTranscript()
  for (let i = 0; i < MAX_FLOOR_LINES + 5; i++) defaults.apply({ status: 'final', text: `line ${i}`, segment_id: `s${i}` })
  assert(defaults.lines().length === MAX_FLOOR_LINES, 'the default transcript keeps only MAX_FLOOR_LINES lines')
  assert(defaults.lines()[0].text === 'line 5', 'the default transcript drops the oldest lines')

  const late = createFloorTranscript()
  late.apply({ status: 'final', text: 'Done.', segment_id: 'seg-1' })
  assert(!late.apply({ status: 'partial', text: 'Do', segment_id: 'seg-1' }), 'a partial arriving after its own final is ignored')
  assert(late.partial() === null, 'no stale partial is left under the final')
  assert(late.apply({ status: 'partial', text: 'Next', segment_id: 'seg-2' }), 'a partial of the next segment is still shown')
}

// ── createInterpreterAssist with a minimal DOM ────────────────────────────

class FakeNode {
  constructor(tag, text = '') {
    this.tagName = tag
    this.children = []
    this._text = text
    this.className = ''
    this.title = ''
    this.listeners = {}
    this.scrollTop = 0
    this.scrollHeight = 0
    this.clientHeight = 0
    const classes = new Set()
    this.classList = {
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
      contains: (name) => classes.has(name),
    }
  }
  get textContent() {
    return this.tagName === '#text' ? this._text : this._text + this.children.map((c) => c.textContent).join('')
  }
  set textContent(value) {
    this._text = String(value)
    this.children = []
  }
  appendChild(node) { this.children.push(node); return node }
  append(...nodes) { nodes.forEach((n) => this.appendChild(n)) }
  replaceChildren(...nodes) { this.children = [...nodes] }
  addEventListener(type, fn) { this.listeners[type] = fn }
}

function fakeDocument() {
  const ids = [
    'assist-term-count', 'assist-floor-log', 'assist-floor-empty', 'assist-search',
    'assist-glossary', 'assist-glossary-empty', 'assist-glossary-note',
  ]
  const nodes = Object.fromEntries(ids.map((id) => [id, new FakeNode('div')]))
  nodes['assist-search'].value = ''
  return {
    nodes,
    getElementById: (id) => nodes[id] || null,
    createElement: (tag) => new FakeNode(tag),
    createTextNode: (text) => new FakeNode('#text', text),
  }
}

function findAll(node, tag, out = []) {
  if (node.tagName === tag) out.push(node)
  node.children.forEach((child) => findAll(child, tag, out))
  return out
}

{
  const doc = fakeDocument()
  const assist = createInterpreterAssist(doc)
  const { nodes } = doc
  assert(!nodes['assist-glossary-empty'].classList.contains('hidden'), 'an empty glossary says so')

  assist.setGlossary({
    terms: [
      { id: 1, source_term: 'keynote', target_term: 'discours', description: 'Opening talk' },
      { id: 2, source_term: '<img src=x onerror=alert(1)>', target_term: 'safe' },
    ],
    truncated: true,
  })
  assert(nodes['assist-term-count'].textContent === '2', 'the count shows the number of terms')
  assert(nodes['assist-glossary'].children.length === 2, 'every term is listed')
  assert(!nodes['assist-glossary-note'].classList.contains('hidden'), 'a capped glossary says so')
  assert(
    nodes['assist-glossary'].children[1].children[0].textContent === '<img src=x onerror=alert(1)>',
    'glossary text is shown as text, never parsed as HTML',
  )

  nodes['assist-search'].value = 'opening'
  nodes['assist-search'].listeners.input()
  assert(nodes['assist-glossary'].children.length === 1, 'typing in the search box filters the list')
  const row = nodes['assist-glossary'].children[0]
  assert(
    row.children[0].textContent === 'keynote' && row.children[1].textContent === 'discours',
    'the search shows the matching term and its translation',
  )
  nodes['assist-search'].value = 'zzz'
  nodes['assist-search'].listeners.input()
  assert(nodes['assist-glossary-empty'].textContent === 'No term matches your search.', 'a search with no match says so')

  assist.handleFloorCaption({
    type: 'booth:floor-caption',
    status: 'final',
    segment_id: 's1',
    text: 'The keynote starts now.',
    hits: [{ start: 4, end: 11, term_id: 1, target_term: 'discours' }],
  })
  const log = nodes['assist-floor-log']
  const marks = findAll(log, 'mark')
  assert(log.children.length === 1, 'a final caption adds a line')
  assert(marks.length === 1 && marks[0].children[0].textContent === 'keynote', 'the glossary term is marked')
  assert(marks[0].children[1].textContent === 'discours', 'the translation is shown next to the term')
  assert(nodes['assist-floor-empty'].classList.contains('hidden'), 'the empty note hides once captions arrive')

  assist.handleFloorCaption({ status: 'partial', text: 'And now', hits: [] })
  assert(log.children.length === 2 && log.children[1].className.includes('partial'), 'a partial is shown after the finals')
}

{
  const assist = createInterpreterAssist({ getElementById: () => null })
  assist.setGlossary({ terms: [{ id: 1, source_term: 'a', target_term: 'b' }] })
  assist.handleFloorCaption({ status: 'final', text: 'a' })
  assert(true, 'a page without the panel does not break')
}

console.log(`\n${passed} passed, ${failed} failed`)
if (failed > 0) process.exit(1)
