import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  AI_LABEL,
  MAX_BODY_LENGTH,
  parseExistingAttribution,
  detectAgentDetails,
  detectModel,
  formatAttributionLine,
  appendAttributionToBody,
  runAgentAttribution,
} from '../.github/scripts/agent-attribution-lib.mjs';

const TEMPLATE = readFileSync(new URL('../.github/PULL_REQUEST_TEMPLATE.md', import.meta.url), 'utf8');
const CLAUDE_TRAILER = 'feat\n\nCo-authored-by: Claude Opus 5.5 <noreply@anthropic.com>';

// Fake GitHub client. `bodies` and `heads` are the values returned by successive
// pulls.get calls (the last one repeats). `failures` lists errors thrown by
// successive pulls.update calls before they succeed.
function makeGithub({
  body,
  bodies,
  heads = ['sha1'],
  labels = [],
  commits = [],
  commitCount,
  listLimit = Infinity,
  updateFailures = [],
} = {}) {
  const calls = [];
  const bodyQueue = bodies || [body];
  let getCount = 0;
  let updateCount = 0;
  const github = {
    paginate: async () => commits.slice(0, listLimit).map((message) => ({ commit: { message } })),
    rest: {
      pulls: {
        listCommits: () => {},
        get: async () => {
          const i = getCount;
          getCount += 1;
          return {
            data: {
              state: 'open',
              body: bodyQueue[Math.min(i, bodyQueue.length - 1)],
              labels,
              commits: commitCount ?? commits.length,
              head: { sha: heads[Math.min(i, heads.length - 1)] },
              base: { sha: 'base' },
            },
          };
        },
        update: async ({ pull_number, body: newBody }) => {
          const failure = updateFailures[updateCount];
          updateCount += 1;
          if (failure) throw failure;
          calls.push({ type: 'update', pull_number, body: newBody });
        },
      },
      repos: {
        compareCommitsWithBasehead: async ({ page }) => {
          calls.push({ type: 'compare', page });
          const start = (page - 1) * 100;
          return { data: { commits: commits.slice(start, start + 100).map((message) => ({ commit: { message } })) } };
        },
      },
      issues: {
        getLabel: async () => ({ data: { name: AI_LABEL } }),
        createLabel: async () => calls.push({ type: 'createLabel' }),
        addLabels: async ({ issue_number, labels: added }) => {
          calls.push({ type: 'addLabels', issue_number, labels: added });
        },
      },
    },
  };
  return { github, calls };
}

const core = { info() {}, warning() {}, setFailed() {} };
const context = (number) => ({
  repo: { owner: 'fossasia', repo: 'voxbento' },
  payload: { pull_request: { number } },
});
const run = (github, number = 1) => runAgentAttribution({ github, context: context(number), core, retryDelayMs: 0 });
const types = (calls) => calls.filter((c) => c.type !== 'compare').map((c) => c.type);

// --- parsing ---

test('parseExistingAttribution parses full attribution line', () => {
  const line = 'Created with Claude Opus 5.5 in Claude Code, run from T3 Code.';
  assert.deepEqual(parseExistingAttribution(`Some text\n\n${line}`), {
    raw: line,
    action: 'Created',
    model: 'Claude Opus 5.5',
    harness: 'Claude Code',
    environment: 'T3 Code',
  });
});

test('parseExistingAttribution ignores review attribution', () => {
  assert.equal(parseExistingAttribution('Review\n\nReviewed with Gemini 2.0 Flash in Antigravity.'), null);
});

test('parseExistingAttribution ignores HTML comments, code fences and prose', () => {
  assert.equal(parseExistingAttribution('Just normal text here.'), null);
  assert.equal(parseExistingAttribution(TEMPLATE), null);
  assert.equal(parseExistingAttribution('<!--\nCreated with Claude Opus 5.5 in Claude Code.\n-->'), null);
  assert.equal(parseExistingAttribution('Example:\n```\nCreated with Claude Opus 5.5 in Claude Code.\n```'), null);
  assert.equal(parseExistingAttribution('~~~md\nCreated with Gemini in Antigravity.\n~~~'), null);
});

// --- detection ---

test('detectAgentDetails reads the model from a Claude Code trailer', () => {
  assert.deepEqual(detectAgentDetails({ commitMessages: [CLAUDE_TRAILER] }), {
    model: 'Claude Opus 5.5',
    harness: 'Claude Code',
    environment: null,
    explicit: false,
  });
});

test('detectAgentDetails does not treat a co-author identity as a model', () => {
  for (const trailer of [
    'Co-authored-by: Claude <noreply@anthropic.com>',
    'Co-authored-by: Claude Code <claude@example.com>',
  ]) {
    const detected = detectAgentDetails({ commitMessages: [`feat\n\n${trailer}`] });
    assert.equal(detected.model, null, trailer);
    assert.equal(detected.harness, 'Claude Code', trailer);
    assert.equal(formatAttributionLine(detected), 'Created with Claude Code.');
  }
});

test('detectAgentDetails does not invent a model for harness-only trailers', () => {
  const detected = detectAgentDetails({
    commitMessages: ['fix: button\n\nCo-authored-by: Copilot <copilot@github.com>'],
  });
  assert.equal(formatAttributionLine(detected), 'Created with GitHub Copilot.');
});

test('detectAgentDetails detects ChatGPT from a trailer', () => {
  const detected = detectAgentDetails({
    commitMessages: ['feat: y\n\nCo-authored-by: ChatGPT <noreply@openai.com>'],
  });
  assert.equal(formatAttributionLine(detected), 'Created with ChatGPT.');
});

test('detectAgentDetails ignores trailers from non-agent co-authors', () => {
  assert.equal(
    detectAgentDetails({ commitMessages: ['feat\n\nCo-authored-by: Claude Gemini Smith <claude@example.org>'] }),
    null,
  );
});

test('detectModel reports versions exactly as written', () => {
  assert.equal(detectModel('Claude 3.5 Sonnet'), 'Claude Sonnet 3.5');
  assert.equal(detectModel('claude sonnet 3.5'), 'Claude Sonnet 3.5');
  assert.equal(detectModel('Claude Opus 4.7'), 'Claude Opus 4.7');
  assert.equal(detectModel('GPT-4.1'), 'GPT-4.1');
  assert.equal(detectModel('gpt-4o'), 'GPT-4o');
  assert.equal(detectModel('used GPT-4.1.'), 'GPT-4.1');
  assert.equal(detectModel('Gemini Pro'), 'Gemini Pro');
  assert.equal(detectModel('Gemini 2.5 Pro'), 'Gemini 2.5 Pro');
  assert.equal(detectModel('deepseek-r1'), 'DeepSeek-R1');
});

test('detectAgentDetails uses an unlisted model version from a trailer verbatim', () => {
  const detected = detectAgentDetails({
    commitMessages: ['feat\n\nCo-authored-by: aider (gpt-4.1) <noreply@aider.chat>'],
  });
  assert.equal(formatAttributionLine(detected), 'Created with GPT-4.1 in Aider.');
});

test('detectAgentDetails uses explicit attribution from a commit message', () => {
  assert.deepEqual(
    detectAgentDetails({ commitMessages: ['feat: caching\n\nCreated with Gemini 2.0 Flash in Antigravity.'] }),
    { model: 'Gemini 2.0 Flash', harness: 'Antigravity', environment: null, explicit: true },
  );
});

test('detectAgentDetails ignores review-only attribution', () => {
  assert.equal(
    detectAgentDetails({
      body: '## Change\nStuff\n\nReviewed with Claude Opus 5.5 in Claude Code.',
      commitMessages: ['fix: human work\n\nReviewed with Gemini in Antigravity.'],
    }),
    null,
  );
});

test('detectAgentDetails ignores attribution examples in code fences', () => {
  const body = 'Docs now show the format:\n\n```\nCreated with Claude Opus 5.5 in Claude Code.\n```';
  assert.equal(detectAgentDetails({ body, commitMessages: ['docs: explain attribution'] }), null);
});

test('detectAgentDetails recognises the older CONTRIBUTING.md section', () => {
  const body = '## AI Attribution\n\nThis PR was created with the assistance of **Claude Sonnet 4.6**.';
  const detected = detectAgentDetails({ body });
  assert.equal(formatAttributionLine(detected), 'Created with Claude Sonnet 4.6.');
});

test('detectAgentDetails ignores incidental tool names in prose', () => {
  assert.equal(
    detectAgentDetails({
      body: 'Fix cursor position in the editor and update composer.json. Copilot mentioned in docs.',
      commitMessages: [
        'fix: cursor jumps on focus',
        'chore: bump composer deps, mention claude and gemini',
        'feat: x\n\nSigned-off-by: Dev <dev@example.com>',
      ],
    }),
    null,
  );
});

test('detectAgentDetails ignores the untouched PR template', () => {
  assert.equal(detectAgentDetails({ body: TEMPLATE, commitMessages: ['feat: human work'] }), null);
});

// --- formatting and placement ---

test('formatAttributionLine formats correctly with and without environment', () => {
  assert.equal(
    formatAttributionLine({ model: 'Claude Opus 5.5', harness: 'Claude Code', environment: 'T3 Code' }),
    'Created with Claude Opus 5.5 in Claude Code, run from T3 Code.',
  );
  assert.equal(formatAttributionLine({ model: 'Composer', harness: 'Cursor' }), 'Created with Composer in Cursor.');
});

test('appendAttributionToBody appends cleanly and is idempotent', () => {
  const line = 'Created with Claude in Claude Code.';
  const appended = appendAttributionToBody('## Problem\nBug fix', line);
  assert.equal(appended, `## Problem\nBug fix\n\n${line}\n`);
  assert.equal(appendAttributionToBody(appended, line), appended);
});

test('appendAttributionToBody moves mid-description attribution to the end', () => {
  const line = 'Created with Claude in Claude Code.';
  const body = `## Problem\n\n${line}\n\n## Verification\nTests pass`;
  assert.equal(appendAttributionToBody(body, line), `## Problem\n\n## Verification\nTests pass\n\n${line}\n`);
});

test('appendAttributionToBody leaves code fences and review lines alone', () => {
  const line = 'Created with Claude in Claude Code.';
  const fenced = '```\nCreated with Gemini in Antigravity.\n```';
  const body = `## Docs\n${fenced}\n\nReviewed with Gemini in Antigravity.`;
  assert.equal(appendAttributionToBody(body, line), `${body}\n\n${line}\n`);
});

test('appendAttributionToBody keeps attribution above bot summaries', () => {
  const line = 'Created with Claude in Claude Code.';
  const bot = '## Summary by Sourcery\n\nStuff.\n';
  const updated = appendAttributionToBody(`## Problem\nFix\n\n${bot}`, line);
  assert.equal(updated, `## Problem\nFix\n\n${line}\n\n${bot}`);
  assert.equal(appendAttributionToBody(updated, line), updated);
});

// --- runAgentAttribution ---

test('runAgentAttribution labels and appends attribution from a trailer', async () => {
  const { github, calls } = makeGithub({ body: '## Problem\nFix something', commits: [CLAUDE_TRAILER] });
  await run(github, 42);
  assert.deepEqual(types(calls), ['addLabels', 'update']);
  assert.ok(calls[1].body.trimEnd().endsWith('Created with Claude Opus 5.5 in Claude Code.'));
});

test('runAgentAttribution builds the update from the latest body', async () => {
  const { github, calls } = makeGithub({
    bodies: ['## Problem\nOld', '## Problem\nEdited by author'],
    commits: [CLAUDE_TRAILER],
  });
  await run(github);
  const update = calls.find((c) => c.type === 'update');
  assert.equal(update.body, '## Problem\nEdited by author\n\nCreated with Claude Opus 5.5 in Claude Code.\n');
});

test('runAgentAttribution keeps attribution the author changed after the first read', async () => {
  const authorLine = 'Created with Claude Sonnet 4.6 in Cursor.';
  const { github, calls } = makeGithub({
    bodies: ['## Problem\nX\n\nCreated with Claude in Claude Code.\n\nmore', `## Problem\nX\n\n${authorLine}\n\nmore`],
    commits: ['feat: human'],
  });
  await run(github);
  const update = calls.find((c) => c.type === 'update');
  assert.ok(update.body.trimEnd().endsWith(authorLine));
  assert.ok(!update.body.includes('Created with Claude in Claude Code.'));
});

test('runAgentAttribution re-reads when the body changes just before writing', async () => {
  const { github, calls } = makeGithub({
    bodies: ['A', 'A', 'B', 'B', 'B'],
    commits: [CLAUDE_TRAILER],
  });
  await run(github);
  const updates = calls.filter((c) => c.type === 'update');
  assert.equal(updates.length, 1);
  assert.ok(updates[0].body.startsWith('B\n'));
});

test('runAgentAttribution stops when the head moves after a force-push', async () => {
  const { github, calls } = makeGithub({
    body: '## Problem',
    heads: ['sha1', 'sha2'],
    commits: [CLAUDE_TRAILER],
  });
  await run(github);
  assert.deepEqual(calls, []);
});

test('runAgentAttribution does nothing when attribution and label are present', async () => {
  const { github, calls } = makeGithub({
    body: '## Problem\nFix\n\nCreated with Claude Opus 5.5 in Claude Code, run from T3 Code.',
    labels: [{ name: AI_LABEL }],
    commits: ['feat: add stuff'],
  });
  await run(github);
  assert.deepEqual(calls, []);
});

test('runAgentAttribution makes no changes for a human PR from the template', async () => {
  const { github, calls } = makeGithub({ body: TEMPLATE, commits: ['fix: cursor position in composer view'] });
  await run(github);
  assert.deepEqual(calls, []);
});

test('runAgentAttribution reads trailers beyond the 250-commit list limit', async () => {
  const commits = Array.from({ length: 260 }, (_, i) => `chore: step ${i}`);
  commits[255] = CLAUDE_TRAILER;
  const { github, calls } = makeGithub({ body: '## Problem', commits, listLimit: 250 });
  await run(github);
  assert.deepEqual(calls.filter((c) => c.type === 'compare').map((c) => c.page), [1, 2, 3]);
  assert.deepEqual(types(calls), ['addLabels', 'update']);
});

test('runAgentAttribution still labels when the body is too long to update', async () => {
  const { github, calls } = makeGithub({ body: 'x'.repeat(MAX_BODY_LENGTH), commits: [CLAUDE_TRAILER] });
  await run(github);
  assert.deepEqual(types(calls), ['addLabels']);
});

test('runAgentAttribution retries a transient body update failure', async () => {
  const { github, calls } = makeGithub({
    body: '## Problem',
    commits: [CLAUDE_TRAILER],
    updateFailures: [Object.assign(new Error('bad gateway'), { status: 502 })],
  });
  await run(github);
  assert.deepEqual(types(calls), ['addLabels', 'update']);
});

test('runAgentAttribution keeps the label if the body update keeps failing', async () => {
  const error = Object.assign(new Error('boom'), { status: 500 });
  const { github, calls } = makeGithub({
    body: '## Problem',
    commits: [CLAUDE_TRAILER],
    updateFailures: [error, error, error],
  });
  await assert.rejects(run(github));
  assert.deepEqual(types(calls), ['addLabels']);
});

test('a later run adds attribution to a PR that is labelled but missing it', async () => {
  const { github, calls } = makeGithub({
    body: '## Problem',
    labels: [{ name: AI_LABEL }],
    commits: [CLAUDE_TRAILER],
  });
  await run(github);
  assert.deepEqual(types(calls), ['update']);
});
