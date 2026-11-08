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
    // Detection and placement read A; the pre-write check sees B, so the run
    // must start over and build the update from B.
    bodies: ['A', 'A', 'A', 'B', 'B', 'B'],
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

// --- CodeRabbit regressions ---

test('detectAgentDetails keeps model and harness from the same trailer', () => {
  const detected = detectAgentDetails({
    commitMessages: [
      'feat: a\n\nCo-authored-by: Cursor Agent <cursoragent@cursor.com>',
      'feat: b\n\nCo-authored-by: Claude Opus 5.5 <noreply@anthropic.com>',
    ],
  });
  assert.equal(formatAttributionLine(detected), 'Created with Claude Opus 5.5 in Claude Code.');

  const harnessOnly = detectAgentDetails({
    commitMessages: [
      'feat: a\n\nCo-authored-by: Cursor Agent <cursoragent@cursor.com>',
      'feat: b\n\nCo-authored-by: Copilot <copilot@github.com>',
    ],
  });
  assert.equal(formatAttributionLine(harnessOnly), 'Created with Cursor.');
});

test('detectAgentDetails ignores a human co-author named Claude', () => {
  assert.equal(
    detectAgentDetails({ commitMessages: ['feat\n\nCo-authored-by: Claude Martin <claude.martin@example.com>'] }),
    null,
  );
});

test('parseExistingAttribution accepts slashes in names', () => {
  const line = 'Created with Claude Sonnet 4.5 in VS Code, run from WSL2/Ubuntu.';
  assert.equal(parseExistingAttribution(`Body\n\n${line}`).environment, 'WSL2/Ubuntu');
});

test('runAgentAttribution labels a PR whose only evidence has a slash environment', async () => {
  const { github, calls } = makeGithub({
    body: '## Change\nX\n\nCreated with Claude Sonnet 4.5 in VS Code, run from WSL2/Ubuntu.',
    commits: ['feat: x'],
  });
  await run(github);
  assert.deepEqual(types(calls), ['addLabels']);
});

test('runAgentAttribution leaves a fenced example in a human PR untouched', async () => {
  const body = '## Change\nDocs now show:\n\n```\nCreated with ChatGPT.\n```\n\n## Testing\nRan docs build.';
  const { github, calls } = makeGithub({ body, commits: ['docs: add attribution example'] });
  await run(github);
  assert.deepEqual(calls, []);
});

test('runAgentAttribution does not turn a review line in a commit into a creation claim', async () => {
  const { github, calls } = makeGithub({
    body: '## Change\nX',
    commits: ['fix: y\n\nReviewed with Gemini in Antigravity.'],
  });
  await run(github);
  assert.deepEqual(calls, []);
});

test('appendAttributionToBody keeps review records when moving creation attribution', () => {
  const created = 'Created with Claude in Claude Code.';
  const reviewed = 'Reviewed with Gemini in Antigravity.';
  const body = `## Change\nX\n\n${created}\n${reviewed}\n\n## Testing\nOK`;
  assert.equal(appendAttributionToBody(body, created), `## Change\nX\n\n${reviewed}\n\n## Testing\nOK\n\n${created}\n`);
});

// --- CodeRabbit follow-up regressions ---

test('detectModel keeps the full GPT variant', () => {
  assert.equal(detectModel('gpt-4o-mini'), 'GPT-4o-mini');
  assert.equal(detectModel('GPT-4.1-nano'), 'GPT-4.1-nano');
  const detected = detectAgentDetails({
    commitMessages: ['feat\n\nCo-authored-by: aider (gpt-4o-mini) <noreply@aider.chat>'],
  });
  assert.equal(formatAttributionLine(detected), 'Created with GPT-4o-mini in Aider.');
});

test('indented code blocks are not attribution', () => {
  const body = '## Change\nExample:\n\n    Created with ChatGPT.\n\n## Testing\nOK';
  assert.equal(detectAgentDetails({ body }), null);
  const line = 'Created with Claude in Claude Code.';
  assert.equal(appendAttributionToBody(body, line), `${body}\n\n${line}\n`);
});

test('a fence line with an info string does not close a fenced block', () => {
  const body = '```md\nSee:\n```javascript\nCreated with ChatGPT.\n```\n\n```\nmore\n```';
  assert.equal(detectAgentDetails({ body }), null);
});

test('human co-authors who share a tool name are not agents', () => {
  for (const trailer of [
    'Aider Smith <aider.smith@example.com>',
    'Cursor Jones <cj@example.org>',
    'Claude Martin <claude@example.com>',
  ]) {
    assert.equal(detectAgentDetails({ commitMessages: [`feat\n\nCo-authored-by: ${trailer}`] }), null, trailer);
  }
  assert.equal(
    formatAttributionLine(detectAgentDetails({ commitMessages: ['feat\n\nCo-authored-by: Aider <noreply@aider.chat>'] })),
    'Created with Aider.',
  );
});

test('fenced Co-authored-by examples in commit messages are ignored', () => {
  const message = 'docs: explain trailers\n\n```\nCo-authored-by: Claude Opus 5.5 <noreply@anthropic.com>\n```';
  assert.equal(detectAgentDetails({ commitMessages: [message] }), null);
});

test('a legacy model wins over a harness-only trailer', () => {
  const detected = detectAgentDetails({
    body: 'This PR was created with the assistance of **Claude Sonnet 4.6**.',
    commitMessages: ['feat\n\nCo-authored-by: Claude <noreply@anthropic.com>'],
  });
  assert.equal(formatAttributionLine(detected), 'Created with Claude Sonnet 4.6.');
});

test('a fenced block after the attribution counts as later content', () => {
  const line = 'Created with ChatGPT.';
  const body = `## Change\n\n${line}\n\n\`\`\`\nlog output\n\`\`\``;
  assert.equal(appendAttributionToBody(body, line), `## Change\n\n\`\`\`\nlog output\n\`\`\`\n\n${line}\n`);
});

test('bot markers inside a fenced example do not split the description', () => {
  const line = 'Created with ChatGPT.';
  const body = '## Change\n```\n## Summary by Sourcery\n```\n\n## Testing\nOK';
  assert.equal(appendAttributionToBody(body, line), `${body}\n\n${line}\n`);
});

test('earlier duplicate attribution lines are removed even when the last line matches', () => {
  const line = 'Created with ChatGPT.';
  const body = `## Change\n${line}\n\n## Testing\nOK\n\n${line}\n`;
  assert.equal(appendAttributionToBody(body, line), `## Change\n\n## Testing\nOK\n\n${line}\n`);
});

test('blank lines inside code blocks and comments are preserved', () => {
  const line = 'Created with ChatGPT.';
  const body = '## Repro\n```\na\n\n\n\nb\n```\n<!--\nx\n\n\n\ny\n-->\n\n\n\nText';
  const updated = appendAttributionToBody(body, line);
  assert.ok(updated.includes('```\na\n\n\n\nb\n```'));
  assert.ok(updated.includes('<!--\nx\n\n\n\ny\n-->'));
  assert.ok(updated.includes('-->\n\nText'));
});

test('attribution is placed outside an unclosed fence or comment', () => {
  const line = 'Created with ChatGPT.';
  assert.equal(appendAttributionToBody('## Log\n```\nerror', line), `## Log\n\`\`\`\nerror\n\`\`\`\n\n${line}\n`);
  const fixed = appendAttributionToBody('Notes\n<!-- todo', line);
  assert.equal(fixed, `Notes\n<!-- todo\n-->\n\n${line}\n`);
  assert.equal(appendAttributionToBody(fixed, line), fixed);
});

test('runAgentAttribution does not restore attribution the author removed', async () => {
  const { github, calls } = makeGithub({
    bodies: ['## Change\nX\n\nCreated with ChatGPT.\n\nmore', '## Change\nX\n\nCreated with ChatGPT.\n\nmore', '## Change\nX'],
    commits: ['feat: human'],
  });
  await run(github);
  assert.deepEqual(types(calls), ['addLabels']);
});

test('runAgentAttribution retries transient read failures', async () => {
  const { github, calls } = makeGithub({ body: '## Problem', commits: [CLAUDE_TRAILER] });
  const paginate = github.paginate;
  let failures = 1;
  github.paginate = async (...args) => {
    if (failures-- > 0) throw Object.assign(new Error('bad gateway'), { status: 502 });
    return paginate(...args);
  };
  await run(github);
  assert.deepEqual(types(calls), ['addLabels', 'update']);
});

test('bot summaries are not read as attribution evidence', async () => {
  const body = '## Change\nX\n\n## Summary by Sourcery\n\nCreated with ChatGPT.\n';
  assert.equal(detectAgentDetails({ body }), null);
  const { github, calls } = makeGithub({ body, commits: ['feat: human'] });
  await run(github);
  assert.deepEqual(calls, []);
});

// --- CodeRabbit third-round regressions ---

test('detectModel keeps dated GPT identifiers', () => {
  assert.equal(detectModel('gpt-4o-2024-08-06'), 'GPT-4o-2024-08-06');
  const detected = detectAgentDetails({
    commitMessages: ['feat\n\nCo-authored-by: aider (gpt-4o-2024-08-06) <noreply@aider.chat>'],
  });
  assert.equal(formatAttributionLine(detected), 'Created with GPT-4o-2024-08-06 in Aider.');
});

test('employees at agent vendors are not treated as agents', () => {
  for (const trailer of [
    'Jane Doe <jane@cursor.com>',
    'Sam Lee <sam@windsurf.com>',
    'Ana Ruiz <ana@codeium.com>',
  ]) {
    assert.equal(detectAgentDetails({ commitMessages: [`feat\n\nCo-authored-by: ${trailer}`] }), null, trailer);
  }
  const cursor = detectAgentDetails({ commitMessages: ['feat\n\nCo-authored-by: Cursor Agent <cursoragent@cursor.com>'] });
  assert.equal(cursor.harness, 'Cursor');
});

test('fenced examples inside list items are code', () => {
  const body = '## Steps\n1. Add this footer:\n   ```\n   Created with ChatGPT.\n   ```\n2. Done';
  assert.equal(detectAgentDetails({ body }), null);
  const line = 'Created with Claude in Claude Code.';
  assert.equal(appendAttributionToBody(body, line), `${body}\n\n${line}\n`);
  const bulleted = '- ```\n  Created with ChatGPT.\n  ```';
  assert.equal(detectAgentDetails({ body: bulleted }), null);
});

test('blank lines inside an indented code block are preserved', () => {
  const line = 'Created with ChatGPT.';
  const body = 'Repro:\n\n    step one\n\n\n    step two\n\nDone';
  const updated = appendAttributionToBody(body, line);
  assert.ok(updated.includes('    step one\n\n\n    step two'), updated);
});

test('comment markers inside inline code do not hide later text', () => {
  const line = 'Created with ChatGPT.';
  const body = 'Use `<!--` to start a comment.\n\nCreated with ChatGPT.';
  assert.equal(parseExistingAttribution(body).raw, line);
  assert.equal(appendAttributionToBody(body, line), body);
  assert.equal(appendAttributionToBody(`${body}\n`, line), `${body}\n`);
});
