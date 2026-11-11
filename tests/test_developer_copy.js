"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function loadCopyHandler({ clipboard, execCommand }) {
  const timers = [];
  let nextTimerId = 0;
  const activeTimers = new Map();
  const appendedElements = [];
  const wrapperClasses = new Set();
  const tooltipClasses = new Set();
  const tooltip = {
    textContent: "Copy to clipboard",
    classList: {
      add: (name) => tooltipClasses.add(name),
      remove: (name) => tooltipClasses.delete(name),
    },
  };
  const wrapper = {
    dataset: { copyText: "secret-value" },
    classList: {
      add: (name) => wrapperClasses.add(name),
      remove: (name) => wrapperClasses.delete(name),
    },
    querySelector: () => tooltip,
  };
  let clickHandler;
  const document = {
    addEventListener(type, handler) {
      if (type === "DOMContentLoaded") handler();
      if (type === "click") clickHandler = handler;
    },
    getElementById: () => null,
    querySelectorAll: () => [],
    createElement: () => ({
      style: {},
      value: "",
      focus() {},
      select() {},
      remove() {
        appendedElements.splice(appendedElements.indexOf(this), 1);
      },
    }),
    body: {
      appendChild(element) {
        appendedElements.push(element);
      },
    },
    execCommand: execCommand
      ? (command) => execCommand(command, appendedElements)
      : undefined,
  };
  const context = {
    document,
    navigator: clipboard === undefined ? {} : { clipboard },
    console: { error() {} },
    setTimeout(callback, delay) {
      const timer = { id: ++nextTimerId, callback, delay, cleared: false };
      timers.push(timer);
      activeTimers.set(timer.id, timer);
      return timer.id;
    },
    clearTimeout(id) {
      const timer = activeTimers.get(id);
      if (timer) {
        timer.cleared = true;
        activeTimers.delete(id);
      }
    },
  };
  const source = fs.readFileSync(
    path.join(__dirname, "../portal/static/js/developer.js"),
    "utf8"
  );
  vm.runInNewContext(source, context);

  return {
    click() {
      return clickHandler({ target: { closest: () => wrapper } });
    },
    wrapperClasses,
    tooltip,
    timers,
    appendedElements,
    runTimer(timer) {
      timer.callback();
    },
  };
}

async function main() {
  let rejectedWriteCalls = 0;
  const rejectedClipboard = {
    writeText: async () => {
      rejectedWriteCalls += 1;
      throw new Error("clipboard denied");
    },
  };
  const rejectedClipboardCopy = loadCopyHandler({
    clipboard: rejectedClipboard,
    execCommand(command, elements) {
      assert.equal(command, "copy");
      assert.equal(elements[0].value, "secret-value");
      return false;
    },
  });
  await rejectedClipboardCopy.click();
  assert.equal(rejectedWriteCalls, 1);
  assert.ok(rejectedClipboardCopy.wrapperClasses.has("copy-failed"));
  assert.equal(rejectedClipboardCopy.tooltip.textContent, "Copy failed");
  assert.equal(rejectedClipboardCopy.appendedElements.length, 0);

  let successfulWriteCalls = 0;
  const asyncClipboardCopy = loadCopyHandler({
    clipboard: {
      async writeText(text) {
        successfulWriteCalls += 1;
        assert.equal(text, "secret-value");
      },
    },
    execCommand: () => false,
  });
  await asyncClipboardCopy.click();
  assert.equal(successfulWriteCalls, 1);
  assert.ok(asyncClipboardCopy.wrapperClasses.has("copied"));
  assert.equal(asyncClipboardCopy.tooltip.textContent, "Copied!");

  let unexpectedWriteCalls = 0;
  const synchronousFallback = loadCopyHandler({
    clipboard: {
      async writeText() {
        unexpectedWriteCalls += 1;
      },
    },
    execCommand: () => true,
  });
  await synchronousFallback.click();
  assert.equal(unexpectedWriteCalls, 0);
  assert.ok(synchronousFallback.wrapperClasses.has("copied"));

  const failedCopy = loadCopyHandler({
    clipboard: undefined,
    execCommand: () => false,
  });
  await failedCopy.click();
  assert.ok(failedCopy.wrapperClasses.has("copy-failed"));
  assert.equal(failedCopy.tooltip.textContent, "Copy failed");

  let resolveTimedOutWrite;
  const timedOutCopy = loadCopyHandler({
    clipboard: {
      writeText: () =>
        new Promise((resolve) => {
          resolveTimedOutWrite = resolve;
        }),
    },
    execCommand: () => false,
  });
  const timedOutAttempt = timedOutCopy.click();
  const clipboardTimer = timedOutCopy.timers.find(
    (timer) => timer.delay === 2000,
  );
  assert.ok(clipboardTimer);
  timedOutCopy.runTimer(clipboardTimer);
  await timedOutAttempt;
  assert.ok(timedOutCopy.wrapperClasses.has("copy-failed"));
  resolveTimedOutWrite();
  await Promise.resolve();
  assert.ok(timedOutCopy.wrapperClasses.has("copy-failed"));

  let resolveOlderWrite;
  let execCommandCalls = 0;
  const latestAttemptWins = loadCopyHandler({
    clipboard: {
      writeText: () =>
        new Promise((resolve) => {
          resolveOlderWrite = resolve;
        }),
    },
    execCommand: () => {
      execCommandCalls += 1;
      return execCommandCalls > 1;
    },
  });
  const olderAttempt = latestAttemptWins.click();
  await Promise.resolve();
  await latestAttemptWins.click();
  assert.ok(latestAttemptWins.wrapperClasses.has("copied"));
  resolveOlderWrite();
  await olderAttempt;
  assert.ok(latestAttemptWins.wrapperClasses.has("copied"));

  const repeatedCopy = loadCopyHandler({ execCommand: () => true });
  await repeatedCopy.click();
  const previousFeedbackTimer = repeatedCopy.timers[0];
  await repeatedCopy.click();
  const latestFeedbackTimer = repeatedCopy.timers[1];
  assert.equal(previousFeedbackTimer.cleared, true);
  repeatedCopy.runTimer(previousFeedbackTimer);
  assert.ok(repeatedCopy.wrapperClasses.has("copied"));
  repeatedCopy.runTimer(latestFeedbackTimer);
  assert.equal(repeatedCopy.wrapperClasses.has("copied"), false);
  assert.equal(repeatedCopy.tooltip.textContent, "Copy to clipboard");

  console.log("Developer clipboard fallback tests passed");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
