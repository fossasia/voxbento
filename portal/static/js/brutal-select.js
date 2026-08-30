// Custom Brutalist Dropdown Logic
function upgradeSelects() {
  document.querySelectorAll(".brutal-select").forEach((select) => {
    if (select.dataset.upgraded) return;
    select.dataset.upgraded = "true";
    select.style.display = "none"; // Hide native select

    const wrapper = document.createElement("div");
    wrapper.className = "custom-select-wrapper relative w-full";
    select.parentNode.insertBefore(wrapper, select);
    wrapper.appendChild(select);

    const selectedDiv = document.createElement("div");
    selectedDiv.className =
      "custom-selected flex justify-between items-center bg-white border-2 border-[#0d0f10] p-3 rounded font-extrabold text-sm cursor-pointer hover:shadow-[4px_4px_0px_#2563eb] transition-all hover:-translate-y-0.5 text-[#0d0f10]";

    const textSpan = document.createElement("span");
    selectedDiv.appendChild(textSpan);

    const arrow = document.createElement("div");
    arrow.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" height="1em" viewBox="0 0 512 512" class="w-4 h-4 transition-transform duration-300 fill-current"><path d="M233.4 406.6c12.5 12.5 32.8 12.5 45.3 0l192-192c12.5-12.5 12.5-32.8 0-45.3s-32.8-12.5-45.3 0L256 338.7 86.6 169.4c-12.5-12.5-32.8-12.5-45.3 0s-12.5 32.8 0 45.3l192 192z"></path></svg>`;
    selectedDiv.appendChild(arrow);

    const optionsDiv = document.createElement("div");
    optionsDiv.className =
      "custom-options absolute left-0 right-0 top-full mt-2 bg-white border-2 border-[#0d0f10] shadow-[4px_4px_0px_#0d0f10] z-[100] opacity-0 invisible translate-y-[-10px] transition-all duration-200 block rounded max-h-[300px] overflow-y-auto overscroll-contain";

    wrapper.appendChild(selectedDiv);
    wrapper.appendChild(optionsDiv);

    function updateUI() {
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
    }

    let isOpen = false;
    function toggle() {
      if (select.disabled) return;
      isOpen = !isOpen;
      if (isOpen) {
        closeAll();
        optionsDiv.classList.remove(
          "opacity-0",
          "invisible",
          "translate-y-[-10px]",
        );
        arrow.querySelector("svg").style.transform = "rotate(180deg)";
        isOpen = true; // Ensure state is correct
      } else {
        close();
      }
    }

    function close() {
      optionsDiv.classList.add("opacity-0", "invisible", "translate-y-[-10px]");
      arrow.querySelector("svg").style.transform = "rotate(0deg)";
      isOpen = false;
    }

    selectedDiv.onclick = (e) => {
      e.stopPropagation();
      toggle();
    };

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

function closeAll() {
  document
    .querySelectorAll(".custom-select-wrapper")
    .forEach((w) => w.customSelectClose && w.customSelectClose());
}

document.addEventListener("click", closeAll);

// Run initial upgrade and setup a body observer in case elements are added later
upgradeSelects();
const bodyObserver = new MutationObserver(() => upgradeSelects());
bodyObserver.observe(document.body, { childList: true, subtree: true });
