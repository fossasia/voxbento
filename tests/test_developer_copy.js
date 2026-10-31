"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function loadCopyHandler({ clipboard, execCommand }) {
  const timers = [];
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
    setTimeout(callback) {
      timers.push(callback);
      return timers.length;
    },
  };
  const source = fs.readFileSync(
    path.join(__dirname, "../portal/static/js/developer.js"),
    "utf8"
  );
  vm.runInNewContext(source, context);

  return {
    click() {
      clickHandler({ target: { closest: () => wrapper } });
    },
    wrapperClasses,
    tooltip,
    timers,
    appendedElements,
  };
}

async function flushAsyncHandlers() {
  await new Promise((resolve) => setImmediate(resolve));
}

async function main() {
  const rejectedClipboard = {
    writeText: async () => {
      throw new Error("clipboard denied");
    },
  };
  const rejectedClipboardFallback = loadCopyHandler({
    clipboard: rejectedClipboard,
    execCommand(command, elements) {
      assert.equal(command, "copy");
      assert.equal(elements[0].value, "secret-value");
      return true;
    },
  });
  rejectedClipboardFallback.click();
  await flushAsyncHandlers();
  assert.ok(rejectedClipboardFallback.wrapperClasses.has("copied"));
  assert.equal(rejectedClipboardFallback.tooltip.textContent, "Copied!");
  assert.equal(rejectedClipboardFallback.appendedElements.length, 0);

  const unavailableClipboardFallback = loadCopyHandler({
    clipboard: undefined,
    execCommand: () => true,
  });
  unavailableClipboardFallback.click();
  await flushAsyncHandlers();
  assert.ok(unavailableClipboardFallback.wrapperClasses.has("copied"));

  const failedCopy = loadCopyHandler({
    clipboard: rejectedClipboard,
    execCommand: () => false,
  });
  failedCopy.click();
  await flushAsyncHandlers();
  assert.ok(failedCopy.wrapperClasses.has("copy-failed"));
  assert.equal(failedCopy.tooltip.textContent, "Copy failed");
  assert.equal(failedCopy.timers.length, 1);

  const unavailableCopy = loadCopyHandler({
    clipboard: undefined,
  });
  unavailableCopy.click();
  await flushAsyncHandlers();
  assert.ok(unavailableCopy.wrapperClasses.has("copy-failed"));
  assert.equal(unavailableCopy.tooltip.textContent, "Copy failed");

  console.log("Developer clipboard fallback tests passed");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
