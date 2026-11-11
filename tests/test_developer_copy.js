"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/**
 * Loads developer.js with controlled DOM, clipboard, and timer behavior.
 */
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
  /** Creates a copy control with inspectable feedback state. */
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
        remove: (...names) => names.forEach((name) => wrapperClasses.delete(name)),
      },
      querySelector: () => tooltip,
      wrapperClasses,
      tooltip,
    };
    wrappers.push(wrapper);
    return wrapper;
  }
  const wrapper = createWrapper("secret-value");
  /** Applies the dashboard's active overlay visibility rule to a fixture. */
  function isOverlayVisible(modal) {
    const styles = fs.readFileSync(
      path.join(__dirname, "../portal/static/css/admin.css"),
      "utf8",
    );
    const developerStyles = fs.readFileSync(
      path.join(__dirname, "../portal/static/css/developer.css"),
      "utf8",
    );
    const hiddenRule = styles.match(
      /\.custom-modal-overlay:not\(\[open\]\)\s*\{([^}]+)\}/,
    )?.[1];
    const visibleRule = developerStyles.match(
      /\.custom-modal-overlay\.active:not\(\[open\]\)\s*\{([^}]+)\}/,
    )?.[1];
    assert.ok(hiddenRule, "admin.css hides unopened custom modal overlays");
    assert.ok(visibleRule, "developer.css overrides unopened active overlays");
    const hiddenDisplay = hiddenRule.match(/display:\s*([^;]+)/)?.[1];
    const visibleDisplay = visibleRule.match(/display:\s*([^;]+)/)?.[1];
    assert.equal(hiddenDisplay, "none");
    assert.equal(visibleDisplay, "flex");
    return modal.modalClasses.has("active")
      ? visibleDisplay !== "none"
      : hiddenDisplay !== "none";
  }
  /** Creates a minimal event target for modal behavior tests. */
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
  /** Creates a button that dispatches click listeners. */
  function createButton() {
    return {
      ...createEventTarget(),
      click() {
        this.dispatch("click");
      },
    };
  }
  /** Creates a modal fixture and its close or confirmation button. */
  function createModal(confirmLabel) {
    const modalClasses = new Set();
    const confirmButton = createButton();
    confirmButton.textContent = confirmLabel;
    const modal = {
      ...createEventTarget(),
      classList: {
        add: (name) => modalClasses.add(name),
        remove: (name) => modalClasses.delete(name),
      },
      querySelector: (selector) =>
        selector === 'button[type="button"]' ? confirmButton : null,
      dispatchClick(target) {
        this.dispatch("click", { target });
      },
      modalClasses,
    };
    return { modal, confirmButton };
  }
  const createAppModal = withModals ? createModal("Cancel") : null;
  const newSecretModal =
    withModals && includeSecretModal
      ? createModal("I have copied my secret")
      : null;
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
    isOverlayVisible,
    runTimer(timer) {
      timer.callback();
    },
  };
}

/** Runs copy feedback, clipboard ordering, and modal behavior assertions. */
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
  assert.equal(
    modalHandlers.isOverlayVisible(modalHandlers.modalFixtures.createAppModal.modal),
    true,
  );
  modalHandlers.modalFixtures.createAppModal.confirmButton.click();
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
  assert.equal(
    modalHandlers.isOverlayVisible(modalHandlers.modalFixtures.newSecretModal.modal),
    true,
  );
  assert.equal(
    modalHandlers.modalFixtures.newSecretModal.confirmButton.textContent,
    "I have copied my secret",
  );
  modalHandlers.modalFixtures.newSecretModal.confirmButton.click();
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

  let pendingWriteCalls = 0;
  const clipboardValue = { value: "" };
  const synchronousCopy = loadCopyHandler({
    clipboard: {
      writeText() {
        pendingWriteCalls += 1;
        return new Promise(() => {});
      },
    },
    execCommand(command, elements) {
      assert.equal(command, "copy");
      clipboardValue.value = elements[0].value;
      return true;
    },
  });
  const firstCredential = synchronousCopy.createWrapper("first-secret");
  const newestCredential = synchronousCopy.createWrapper("newest-secret");
  await synchronousCopy.click(firstCredential);
  assert.ok(firstCredential.wrapperClasses.has("copied"));
  await synchronousCopy.click(newestCredential);
  assert.ok(newestCredential.wrapperClasses.has("copied"));
  assert.equal(clipboardValue.value, "newest-secret");
  assert.equal(pendingWriteCalls, 0);

  const failedCopy = loadCopyHandler({
    clipboard: undefined,
    execCommand: () => false,
  });
  assert.equal(failedCopy.click({}), undefined);
  await failedCopy.click();
  assert.ok(failedCopy.wrapperClasses.has("copy-failed"));
  assert.equal(failedCopy.tooltip.textContent, "Copy failed");

  let failureThenSuccessCount = 0;
  const failureThenSuccess = loadCopyHandler({
    clipboard: undefined,
    execCommand: () => ++failureThenSuccessCount > 1,
  });
  await failureThenSuccess.click();
  assert.ok(failureThenSuccess.wrapperClasses.has("copy-failed"));
  assert.equal(failureThenSuccess.wrapperClasses.has("copied"), false);
  await failureThenSuccess.click();
  assert.ok(failureThenSuccess.wrapperClasses.has("copied"));
  assert.equal(failureThenSuccess.wrapperClasses.has("copy-failed"), false);

  let successThenFailureCount = 0;
  const successThenFailure = loadCopyHandler({
    clipboard: undefined,
    execCommand: () => ++successThenFailureCount === 1,
  });
  await successThenFailure.click();
  assert.ok(successThenFailure.wrapperClasses.has("copied"));
  assert.equal(successThenFailure.wrapperClasses.has("copy-failed"), false);
  await successThenFailure.click();
  assert.ok(successThenFailure.wrapperClasses.has("copy-failed"));
  assert.equal(successThenFailure.wrapperClasses.has("copied"), false);

  const unexpectedClipboardError = loadCopyHandler({
    clipboard: undefined,
    execCommand: () => false,
  });
  const clickTarget = unexpectedClipboardError.createWrapper("secret-value");
  const removeTooltipClass = clickTarget.tooltip.classList.remove;
  let throwOnTooltipClassRemoval = true;
  clickTarget.tooltip.classList.remove = (...names) => {
    if (throwOnTooltipClassRemoval) {
      throwOnTooltipClassRemoval = false;
      throw new Error("tooltip class update failed");
    }
    removeTooltipClass(...names);
  };
  await unexpectedClipboardError.click(clickTarget);
  assert.ok(clickTarget.wrapperClasses.has("copy-failed"));
  assert.equal(
    clickTarget.tooltip.textContent,
    "Copy failed",
  );
  assert.ok(
    unexpectedClipboardError.errors.some((args) =>
      args[0].includes("Unexpected error while copying developer credential"),
    ),
  );
  const unexpectedErrorReset = unexpectedClipboardError.timers.at(-1);
  assert.equal(unexpectedErrorReset.delay, 2000);
  unexpectedClipboardError.runTimer(unexpectedErrorReset);
  assert.equal(
    clickTarget.wrapperClasses.has("copy-failed"),
    false,
  );
  assert.equal(
    clickTarget.tooltip.textContent,
    "Copy to clipboard",
  );

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
