const modal = document.getElementById("demo-modal");
const form = document.getElementById("demo-request-form");
const step1 = document.getElementById("demo-step-1");
const step2 = document.getElementById("demo-step-2");
const submitButton = document.getElementById("demo-submit-btn");
const submitLabel = submitButton?.querySelector("span");
const spinner = document.getElementById("demo-spinner");
const successAlert = document.getElementById("demo-success");
const errorAlert = document.getElementById("demo-error");

let demoStep = 1;

function fieldValue(id) {
  return document.getElementById(id)?.value ?? "";
}

function setStepTwoRequirements() {
  ["demo-firstname", "demo-lastname", "demo-phone", "demo-company", "demo-country", "demo-language"].forEach((id) => {
    document.getElementById(id).required = true;
  });
}

function openDemoModal() {
  if (!modal) return;

  modal.classList.add("is-open");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("demo-modal-no-scroll");
  demoStep = 1;
  step1?.classList.remove("is-hidden");
  step2?.classList.add("is-hidden");
  successAlert?.classList.remove("is-visible");
  errorAlert?.classList.remove("is-visible");
  submitButton?.classList.remove("is-hidden");
  if (submitLabel) submitLabel.textContent = "Continue";
}

function closeDemoModal() {
  if (!modal) return;

  modal.classList.remove("is-open");
  modal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("demo-modal-no-scroll");
}

function showStepTwo() {
  demoStep = 2;
  step1?.classList.add("is-hidden");
  step2?.classList.remove("is-hidden");
  setStepTwoRequirements();
  if (submitLabel) submitLabel.textContent = "Submit Request";
}

async function submitDemoRequest() {
  if (!submitButton) return;

  submitButton.disabled = true;
  spinner?.classList.add("is-active");
  errorAlert?.classList.remove("is-visible");

  const data = {
    email: fieldValue("demo-email"),
    firstName: fieldValue("demo-firstname"),
    lastName: fieldValue("demo-lastname"),
    phoneNumber: `${fieldValue("demo-phonecode")} ${fieldValue("demo-phone")}`,
    companyName: fieldValue("demo-company"),
    country: fieldValue("demo-country"),
    industry: fieldValue("demo-industry"),
    companySize: fieldValue("demo-size"),
    languageSupport: fieldValue("demo-language"),
  };

  try {
    const response = await fetch("/api/request-demo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    const result = await response.json();

    if (result.ok) {
      step2?.classList.add("is-hidden");
      submitButton.classList.add("is-hidden");
      successAlert?.classList.add("is-visible");
      setTimeout(closeDemoModal, 3000);
      setTimeout(() => window.location.reload(), 3200);
      return;
    }

    if (errorAlert) {
      errorAlert.textContent = result.detail || "Oops! Something went wrong. Please try again.";
      errorAlert.classList.add("is-visible");
    }
  } catch (error) {
    console.error("[demo-modal] Demo request failed:", error);
    if (errorAlert) {
      errorAlert.textContent = "Oops! Something went wrong. Please try again.";
      errorAlert.classList.add("is-visible");
    }
  } finally {
    submitButton.disabled = false;
    spinner?.classList.remove("is-active");
  }
}

document.querySelectorAll("[data-demo-modal-open]").forEach((trigger) => {
  trigger.addEventListener("click", openDemoModal);
});
document.querySelectorAll("[data-demo-modal-close]").forEach((trigger) => {
  trigger.addEventListener("click", closeDemoModal);
});
form?.addEventListener("submit", (event) => {
  event.preventDefault();
  if (demoStep === 1) {
    showStepTwo();
    return;
  }
  void submitDemoRequest();
});
