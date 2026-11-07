// Unit tests for Jules automated reviewer script
import assert from 'node:assert/strict';
import {
  added,
  parse,
  isDeterministic,
  prompt
} from '../.github/scripts/jules-review.mjs';

console.log('Running Jules Reviewer tests...');

// 1. Test added() hunk parser
{
  const patch = [
    '@@ -10,6 +10,8 @@ function test() {',
    '   const a = 1;',
    '+  const b = 2;',
    '+++i;', // line starting with ++
    '-  const c = 3;',
    '   return a;',
    '@@ -25,3 +27,4 @@',
    '   const x = 10;',
    '+  const y = 20;'
  ].join('\n');

  const lines = added(patch);
  assert.equal(lines.has(11), true, 'Line 11 (const b = 2;) should be recognized as added');
  assert.equal(lines.has(12), true, 'Line 12 (++i;) starting with ++ should be recognized as added');
  assert.equal(lines.has(10), false, 'Line 10 is context, should not be in added set');
  assert.equal(lines.has(28), true, 'Line 28 in second hunk should be recognized as added');
  console.log('✓ added() properly parses added lines including ++ lines without off-by-one errors');
}

// 2. Test parse() with raw JSON
{
  const diff = {
    valid: new Map([['test.js', new Set([10, 11])]])
  };

  const rawJson = JSON.stringify({
    summary: 'Looks good overall',
    verdict: 'comment',
    newComments: [
      {
        file: 'test.js',
        line: 10,
        severity: 'Warning',
        message: 'Consider refactoring this line'
      }
    ]
  });

  const parsed = parse(rawJson, diff);
  assert.equal(parsed.verdict, 'comment');
  assert.equal(parsed.comments.length, 1);
  assert.equal(parsed.comments[0].path, 'test.js');
  assert.equal(parsed.comments[0].line, 10);
  console.log('✓ parse() handles raw JSON payload');
}

// 3. Test parse() with markdown code fences and inner fences in message
{
  const diff = {
    valid: new Map([['portal/auth.py', new Set([42])]])
  };

  const fencedWithInnerFences = `
Here is my review:

\`\`\`json
{
  "summary": "Security considerations in auth",
  "verdict": "comment",
  "newComments": [
    {
      "file": "portal/auth.py",
      "line": 42,
      "severity": "Warning",
      "message": "Use a secure comparison like this:\\n\`\`\`python\\ndef check(a, b):\\n    return hmac.compare_digest(a, b)\\n\`\`\`\\nThis prevents timing attacks."
    }
  ]
}
\`\`\`

Hope this helps!
`;

  const parsed = parse(fencedWithInnerFences, diff);
  assert.equal(parsed.verdict, 'comment');
  assert.equal(parsed.comments.length, 1);
  assert.match(parsed.comments[0].body, /hmac\.compare_digest/);
  console.log('✓ parse() preserves inner code fences inside finding messages without truncation');
}

// 4. Test parse() blocking verdict validation
{
  const diff = {
    valid: new Map([['app.py', new Set([5])]])
  };

  const blockWithoutHigh = JSON.stringify({
    summary: 'Minor style issues',
    verdict: 'block',
    newComments: [
      {
        file: 'app.py',
        line: 5,
        severity: 'Warning',
        message: 'Style nit'
      }
    ]
  });

  assert.throws(
    () => parse(blockWithoutHigh, diff),
    /Blocking verdict without validated High finding/,
    'Should throw if blocking verdict has no High severity findings'
  );

  const blockWithHigh = JSON.stringify({
    summary: 'Critical vulnerability found',
    verdict: 'block',
    newComments: [
      {
        file: 'app.py',
        line: 5,
        severity: 'High',
        message: 'SQL injection vulnerability'
      }
    ]
  });

  const parsed = parse(blockWithHigh, diff);
  assert.equal(parsed.verdict, 'block');
  assert.equal(parsed.comments.length, 1);
  console.log('✓ parse() enforces High finding for blocking verdicts');
}

// 5. Test isDeterministic()
{
  assert.equal(isDeterministic(new Error('PR has no reviewable text diff')), true);
  assert.equal(isDeterministic(new Error('Invalid Jules JSON response')), true);
  assert.equal(isDeterministic(new Error('Invalid Jules response schema')), true);
  assert.equal(isDeterministic(new Error('Blocking verdict without validated High finding')), true);
  assert.equal(isDeterministic(new Error('Jules needs manual input')), true);
  assert.equal(isDeterministic(new Error('Jules session failed')), true);

  // Transient errors
  assert.equal(isDeterministic(new Error('GitHub API 500: Internal Server Error')), false);
  assert.equal(isDeterministic(new Error('Jules API 503: Service Unavailable')), false);
  assert.equal(isDeterministic(new Error('GitHub API 429: rate limit exceeded')), false);
  assert.equal(isDeterministic(new Error('Jules review timed out')), false);
  assert.equal(isDeterministic(new Error('fetch failed')), false);
  console.log('✓ isDeterministic() accurately separates deterministic from transient failures');
}

// 6. Test prompt() format
{
  const pr = {
    number: 996,
    head: { sha: 'abcdef1234567890abcdef1234567890abcdef12' },
    title: 'Test PR',
    body: 'PR description'
  };
  const diff = {
    covered: 1,
    total: 1,
    text: 'FILE test.js (modified)\n+const a = 1;'
  };
  const formattedPrompt = prompt(pr, diff);
  assert.match(formattedPrompt, /UNTRUSTED title: Test PR/);
  assert.match(formattedPrompt, /commit abcdef1234567890abcdef1234567890abcdef12/);
  console.log('✓ prompt() formats untrusted data boundaries correctly');
}

console.log('\nAll Jules Reviewer tests passed successfully!');
