"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function loadCopyHandler({
  clipboard,
  execCommand,
  withModals = false,
  includeSecretModal = true,
}) {
  const timers = [];
  let nextTimerId = 0;
  const activeTimers = new Map();
  const appendedElements = [];
  const wrappers = [];
  function createWrapper(copyText) {
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
      dataset: { copyText },
      classList: {
        add: (name) => wrapperClasses.add(name),
        remove: (name) => wrapperClasses.delete(name),
      },
      querySelector: () => tooltip,
      wrapperClasses,
      tooltip,
    };
    wrappers.push(wrapper);
    return wrapper;
  }
  const wrapper = createWrapper("secret-value");
  function createEventTarget() {
    const listeners = new Map();
    return {
      addEventListener(type, callback) {
        const callbacks = listeners.get(type) ?? [];
        callbacks.push(callback);
        listeners.set(type, callbacks);
      },
      dispatch(type, event = {}) {
        for (const callback of listeners.get(type) ?? []) {
          callback(event);
        }
      },
    };
  }
  function createButton() {
    return {
      ...createEventTarget(),
      click() {
        this.dispatch("click");
      },
    };
  }
  function createModal() {
    const modalClasses = new Set();
    const modal = {
      ...createEventTarget(),
      classList: {
        add: (name) => modalClasses.add(name),
        remove: (name) => modalClasses.delete(name),
      },
      querySelector: () => cancelButton,
      dispatchClick(target) {
        this.dispatch("click", { target });
      },
      modalClasses,
    };
    const cancelButton = createButton();
    return { modal, cancelButton };
  }
  const createAppModal = withModals ? createModal() : null;
  const newSecretModal =
    withModals && includeSecretModal ? createModal() : null;
  const modalFixtures = withModals
    ? {
        createButton: createButton(),
        createAppModal,
        newSecretModal,
      }
    : null;
  const elementsById = new Map();
  if (modalFixtures) {
    elementsById.set("btn-create-app", modalFixtures.createButton);
    elementsById.set("modal-create-app", createAppModal.modal);
    if (newSecretModal) {
      elementsById.set("modal-new-secret", newSecretModal.modal);
    }
  }
  let clickHandler;
  const document = {
    addEventListener(type, handler) {
      if (type === "DOMContentLoaded") handler();
      if (type === "click") clickHandler = handler;
    },
    getElementById: (id) => elementsById.get(id) ?? null,
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
    console: {
      errors: [],
      error(...args) {
        this.errors.push(args);
      },
    },
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
    click(target = wrapper) {
      return clickHandler({
        target: {
          closest(selector) {
            return selector === ".copy-wrapper[data-copy-text]" &&
              wrappers.includes(target)
              ? target
              : null;
          },
        },
      });
    },
    createWrapper,
    wrapperClasses: wrapper.wrapperClasses,
    tooltip: wrapper.tooltip,
    timers,
    appendedElements,
    errors: context.console.errors,
    modalFixtures,
    runTimer(timer) {
      timer.callback();
    },
  };
}

async function main() {
  const modalHandlers = loadCopyHandler({
    clipboard: undefined,
    execCommand: () => false,
    withModals: true,
  });
  modalHandlers.modalFixtures.createButton.click();
  assert.ok(
    modalHandlers.modalFixtures.createAppModal.modal.modalClasses.has("active"),
  );
  modalHandlers.modalFixtures.createAppModal.cancelButton.click();
  assert.equal(
    modalHandlers.modalFixtures.createAppModal.modal.modalClasses.has("active"),
    false,
  );
  modalHandlers.modalFixtures.createButton.click();
  modalHandlers.modalFixtures.createAppModal.modal.dispatchClick(
    modalHandlers.modalFixtures.createAppModal.modal,
  );
  assert.equal(
    modalHandlers.modalFixtures.createAppModal.modal.modalClasses.has("active"),
    false,
  );
  modalHandlers.modalFixtures.newSecretModal.modal.classList.add("active");
  modalHandlers.modalFixtures.newSecretModal.cancelButton.click();
  assert.equal(
    modalHandlers.modalFixtures.newSecretModal.modal.modalClasses.has("active"),
    false,
  );

  const missingSecretModal = loadCopyHandler({
    clipboard: undefined,
    execCommand: () => false,
    withModals: true,
    includeSecretModal: false,
  });
  missingSecretModal.modalFixtures.createButton.click();
  assert.ok(
    missingSecretModal.modalFixtures.createAppModal.modal.modalClasses.has(
      "active",
    ),
  );

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
  assert.equal(failedCopy.click({}), undefined);
  await failedCopy.click();
  assert.ok(failedCopy.wrapperClasses.has("copy-failed"));
  assert.equal(failedCopy.tooltip.textContent, "Copy failed");

  const unexpectedClipboardError = loadCopyHandler({
    clipboard: {
      get writeText() {
        throw new Error("clipboard access failed unexpectedly");
      },
    },
    execCommand: () => false,
  });
  await unexpectedClipboardError.click();
  assert.ok(unexpectedClipboardError.wrapperClasses.has("copy-failed"));
  assert.equal(
    unexpectedClipboardError.tooltip.textContent,
    "Copy failed",
  );
  assert.ok(
    unexpectedClipboardError.errors.some((args) =>
      args[0].includes("Unexpected error while copying developer credential"),
    ),
  );

  let resolveFirstWrite;
  const clipboardWrites = [];
  let clipboardValue = "";
  const serializedCopy = loadCopyHandler({
    clipboard: {
      writeText(text) {
        clipboardWrites.push(text);
        if (text === "secret-value") {
          return new Promise((resolve) => {
            resolveFirstWrite = () => {
              clipboardValue = text;
              resolve();
            };
          });
        }
        clipboardValue = text;
        return Promise.resolve();
      },
    },
    execCommand: () => false,
  });
  const firstWrapper = serializedCopy.createWrapper("secret-value");
  const secondWrapper = serializedCopy.createWrapper("new-secret");
  const firstWrite = serializedCopy.click(firstWrapper);
  const secondWrite = serializedCopy.click(secondWrapper);
  assert.deepEqual(clipboardWrites, ["secret-value"]);
  assert.equal(secondWrapper.tooltip.textContent, "Copying…");
  assert.equal(secondWrapper.wrapperClasses.has("copy-failed"), false);
  resolveFirstWrite();
  await firstWrite;
  await secondWrite;
  assert.deepEqual(clipboardWrites, ["secret-value", "new-secret"]);
  assert.equal(clipboardValue, "new-secret");
  assert.ok(firstWrapper.wrapperClasses.has("copied"));
  assert.ok(secondWrapper.wrapperClasses.has("copied"));

  let resolveRetry;
  let retryWriteCount = 0;
  const retryCopy = loadCopyHandler({
    clipboard: {
      writeText() {
        retryWriteCount += 1;
        if (retryWriteCount === 1) {
          return Promise.resolve();
        }
        return new Promise((resolve) => {
          resolveRetry = resolve;
        });
      },
    },
    execCommand: () => false,
  });
  await retryCopy.click();
  assert.equal(retryCopy.tooltip.textContent, "Copied!");
  const retry = retryCopy.click();
  assert.equal(retryCopy.tooltip.textContent, "Copying…");
  assert.equal(retryCopy.wrapperClasses.has("copied"), false);
  resolveRetry();
  await retry;
  assert.equal(retryCopy.tooltip.textContent, "Copied!");

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
