// Custom Brutalist Dropdown Logic
//
// The visible box is a select-only combobox (WAI-ARIA APG pattern): focus
// stays on the button and the highlighted option is tracked with
// aria-activedescendant, so keyboard and screen reader users can pick a value.
let brutalSelectCount = 0;

function upgradeSelects() {
  document.querySelectorAll(".brutal-select").forEach((select) => {
    if (select.dataset.upgraded) return;
    select.dataset.upgraded = "true";
    select.style.display = "none"; // Hide native select

    const wrapper = document.createElement("div");
    wrapper.className = "custom-select-wrapper relative w-full";
    select.parentNode.insertBefore(wrapper, select);
    wrapper.appendChild(select);

    const baseId = select.id || "brutal-select-" + ++brutalSelectCount;
    const listboxId = baseId + "-listbox";

    const selectedDiv = document.createElement("button");
    selectedDiv.type = "button";
    selectedDiv.className =
      "custom-selected w-full text-left flex justify-between items-center bg-white border-2 border-[#0d0f10] p-3 rounded font-extrabold text-sm cursor-pointer hover:shadow-[4px_4px_0px_#2563eb] transition-all hover:-translate-y-0.5 text-[#0d0f10] focus-visible:outline focus-visible:outline-[3px] focus-visible:outline-offset-2 focus-visible:outline-brand-600";
    selectedDiv.setAttribute("role", "combobox");
    selectedDiv.setAttribute("aria-haspopup", "listbox");
    selectedDiv.setAttribute("aria-expanded", "false");
    selectedDiv.setAttribute("aria-controls", listboxId);

    // Name the button (and the list) after the label that points at the
    // hidden native select.
    const label = select.id
      ? document.querySelector('label[for="' + CSS.escape(select.id) + '"]')
      : null;
    if (label) {
      if (!label.id) label.id = baseId + "-label";
      selectedDiv.setAttribute("aria-labelledby", label.id);
    } else if (select.getAttribute("aria-label")) {
      selectedDiv.setAttribute("aria-label", select.getAttribute("aria-label"));
    }

    const textSpan = document.createElement("span");
    selectedDiv.appendChild(textSpan);

    const arrow = document.createElement("span");
    arrow.setAttribute("aria-hidden", "true");
    arrow.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" height="1em" viewBox="0 0 512 512" class="w-4 h-4 transition-transform duration-300 fill-current"><path d="M233.4 406.6c12.5 12.5 32.8 12.5 45.3 0l192-192c12.5-12.5 12.5-32.8 0-45.3s-32.8-12.5-45.3 0L256 338.7 86.6 169.4c-12.5-12.5-32.8-12.5-45.3 0s-12.5 32.8 0 45.3l192 192z"></path></svg>`;
    selectedDiv.appendChild(arrow);

    const optionsDiv = document.createElement("div");
    optionsDiv.id = listboxId;
    optionsDiv.setAttribute("role", "listbox");
    if (label) optionsDiv.setAttribute("aria-labelledby", label.id);
    optionsDiv.className =
      "custom-options absolute left-0 right-0 top-full mt-2 bg-white border-2 border-[#0d0f10] shadow-[4px_4px_0px_#0d0f10] z-[100] opacity-0 invisible translate-y-[-10px] transition-all duration-200 block rounded max-h-[300px] overflow-y-auto overscroll-contain";

    wrapper.appendChild(selectedDiv);
    wrapper.appendChild(optionsDiv);

    // Classes for the option highlighted by the keyboard. Only shown while the
    // keyboard is in use, so the mouse look stays as it was.
    const ACTIVE_CLASSES = ["ring-[3px]", "ring-inset", "ring-[#0d0f10]"];
    let activeIndex = -1;
    let usingKeyboard = false;

    function setActive(index, scroll) {
      const opts = optionsDiv.children;
      if (opts[activeIndex]) opts[activeIndex].classList.remove(...ACTIVE_CLASSES);
      activeIndex = opts.length ? Math.max(0, Math.min(index, opts.length - 1)) : -1;
      const opt = opts[activeIndex];
      if (!opt || !isOpen) {
        selectedDiv.removeAttribute("aria-activedescendant");
        return;
      }
      if (usingKeyboard) opt.classList.add(...ACTIVE_CLASSES);
      selectedDiv.setAttribute("aria-activedescendant", opt.id);
      if (scroll) opt.scrollIntoView({ block: "nearest" });
    }

    function updateUI() {
      selectedDiv.disabled = select.disabled;
      if (select.disabled) {
        selectedDiv.classList.add(
          "opacity-50",
          "cursor-not-allowed",
          "bg-gray-100",
        );
        selectedDiv.classList.remove(
          "hover:shadow-[4px_4px_0px_#2563eb]",
          "hover:-translate-y-0.5",
          "bg-white",
        );
      } else {
        selectedDiv.classList.remove(
          "opacity-50",
          "cursor-not-allowed",
          "bg-gray-100",
        );
        selectedDiv.classList.add(
          "hover:shadow-[4px_4px_0px_#2563eb]",
          "hover:-translate-y-0.5",
          "bg-white",
        );
      }

      textSpan.textContent =
        select.options[select.selectedIndex]?.textContent ||
        select.options[0]?.textContent ||
        "";
      optionsDiv.innerHTML = "";

      Array.from(select.options).forEach((opt, index) => {
        const optDiv = document.createElement("div");
        optDiv.id = baseId + "-option-" + index;
        optDiv.setAttribute("role", "option");
        optDiv.setAttribute("aria-selected", String(index === select.selectedIndex));
        optDiv.className =
          "p-3 font-extrabold text-sm cursor-pointer hover:bg-brand-50 hover:text-brand-600 transition-colors border-b-2 border-gray-100 last:border-0 text-[#0d0f10]";
        if (index === select.selectedIndex) {
          optDiv.classList.add("bg-brand-600", "text-white");
          optDiv.classList.remove(
            "hover:bg-brand-50",
            "hover:text-brand-600",
            "text-[#0d0f10]",
          );
        }
        optDiv.textContent = opt.textContent;
        optDiv.onclick = (e) => {
          e.stopPropagation();
          if (select.disabled) return;
          select.selectedIndex = index;
          select.dispatchEvent(new Event("change"));
          closeAll();
        };
        optionsDiv.appendChild(optDiv);
      });

      // Options may have been replaced or the select disabled while open
      if (isOpen && select.disabled) {
        close();
      } else {
        setActive(activeIndex, false);
      }
    }

    let isOpen = false;
    function toggle() {
      if (select.disabled) return;
      isOpen = !isOpen;
      if (isOpen) {
        closeAll(wrapper);
        optionsDiv.classList.remove(
          "opacity-0",
          "invisible",
          "translate-y-[-10px]",
        );
        arrow.querySelector("svg").style.transform = "rotate(180deg)";
        isOpen = true; // Ensure state is correct
        selectedDiv.setAttribute("aria-expanded", "true");
        setActive(select.selectedIndex, false);
      } else {
        close();
      }
    }

    function close() {
      optionsDiv.classList.add("opacity-0", "invisible", "translate-y-[-10px]");
      arrow.querySelector("svg").style.transform = "rotate(0deg)";
      isOpen = false;
      selectedDiv.setAttribute("aria-expanded", "false");
      setActive(activeIndex, false);
    }

    selectedDiv.onclick = (e) => {
      e.stopPropagation();
      usingKeyboard = false;
      toggle();
    };

    selectedDiv.addEventListener("keydown", (e) => {
      if (select.disabled || e.altKey || e.ctrlKey || e.metaKey) return;
      const key = e.key;
      usingKeyboard = true;

      if (!isOpen) {
        if (["Enter", " ", "ArrowDown", "ArrowUp"].includes(key)) {
          e.preventDefault();
          toggle();
          setActive(activeIndex, true);
        }
        return;
      }

      const last = optionsDiv.children.length - 1;
      if (key === "ArrowDown") setActive(activeIndex + 1, true);
      else if (key === "ArrowUp") setActive(activeIndex - 1, true);
      else if (key === "Home") setActive(0, true);
      else if (key === "End") setActive(last, true);
      else if (key === "Enter" || key === " ") {
        if (activeIndex !== -1 && activeIndex !== select.selectedIndex) {
          select.selectedIndex = activeIndex;
          select.dispatchEvent(new Event("change"));
        }
        close();
      } else if (key === "Escape") close();
      else if (key === "Tab") {
        // Close and let focus move on as usual
        close();
        return;
      } else return;
      e.preventDefault();
    });

    // Keep focus on the button when an option is clicked, and close the list
    // if focus leaves it some other way (e.g. a screen reader moving focus).
    optionsDiv.addEventListener("mousedown", (e) => e.preventDefault());
    selectedDiv.addEventListener("blur", () => {
      if (isOpen) close();
    });

    // Stop Space from also firing a click on keyup, which would toggle again
    selectedDiv.addEventListener("keyup", (e) => {
      if (e.key === " ") e.preventDefault();
    });

    // Sync with native select mutations
    const observer = new MutationObserver(() => updateUI());
    observer.observe(select, {
      childList: true,
      attributes: true,
      attributeFilter: ["disabled"],
    });

    select.addEventListener("change", updateUI);
    updateUI();

    wrapper.customSelectClose = close;
  });
}

function closeAll(excludeWrapper) {
  document
    .querySelectorAll(".custom-select-wrapper")
    .forEach((w) => {
      if (w !== excludeWrapper && w.customSelectClose) {
        w.customSelectClose();
      }
    });
}

document.addEventListener("click", closeAll);

// Run initial upgrade and setup a body observer in case elements are added later
upgradeSelects();
const bodyObserver = new MutationObserver(() => upgradeSelects());
bodyObserver.observe(document.body, { childList: true, subtree: true });
