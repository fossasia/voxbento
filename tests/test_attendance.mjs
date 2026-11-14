/**
 * Unit tests for the attendance page helpers (portal/static/js/attendance.js, #1215).
 *
 * Run with: node tests/test_attendance.mjs
 */
import assert from 'node:assert/strict'
import { compareValues, formatLocalTime, matchesSearch } from '../portal/static/js/attendance.js'

// Sorting: durations and timestamps compare as numbers, names as text.
assert.ok(compareValues('900', '10000', 'number') < 0, '900 seconds sorts before 10000')
assert.ok(compareValues('lena', 'Arnav', 'text') > 0, 'names sort alphabetically, ignoring case')

// Search: every word must appear, in any order.
assert.equal(matchesSearch('mei lin invite link · day 1', 'mei'), true)
assert.equal(matchesSearch('mei lin invite link · day 1', 'lin mei'), true)
assert.equal(matchesSearch('arnav arnav@example.com', 'example.com'), true)
assert.equal(matchesSearch('arnav arnav@example.com', 'lena'), false)
assert.equal(matchesSearch('anyone', '   '), true, 'an empty search shows everyone')

// Times include the year only when it is not the current one.
const thisYear = new Date(new Date().getFullYear(), 2, 4, 9, 5)
assert.ok(!formatLocalTime(thisYear, 'en-GB').includes(String(thisYear.getFullYear())))
assert.ok(formatLocalTime(new Date(2001, 2, 4, 9, 5), 'en-GB').includes('2001'))

console.log('Attendance page checks passed')
