// SPDX-License-Identifier: GPL-3.0-or-later
// Small progressive enhancements. Loaded from our own origin (CSP: script-src 'self').
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  // Setup: the one-time token arrives in the URL fragment (never sent to the
  // server); move it into the form and remove it from the address bar.
  const token = document.getElementById("setup-token");
  if (token) {
    const match = location.hash.match(/token=([A-Za-z0-9_-]+)/);
    if (match) {
      token.value = match[1];
      history.replaceState(null, "", location.pathname);
    } else {
      document.getElementById("token-missing").hidden = false;
    }
    const url = document.getElementById("public-url");
    if (url && !url.value) url.value = location.origin;
  }

  // Generate: show the preset fields or the permalink field.
  const generate = document.getElementById("generate");
  if (generate) {
    const sync = () => {
      const source = generate.querySelector("input[name=source]:checked").value;
      generate.querySelectorAll("[data-source]").forEach((el) => {
        el.hidden = el.dataset.source !== source;
      });
    };
    generate.addEventListener("change", sync);
    sync();
  }

  // Presets: upload the chosen .rdvpreset as JSON to the API.
  const importButton = document.getElementById("import-preset");
  if (importButton) {
    importButton.addEventListener("click", async () => {
      const file = document.getElementById("preset-file").files[0];
      if (!file) return say("Choose a .rdvpreset file first.");
      if (file.size > 1024 * 1024) return say("Presets can be at most 1 MB.");
      const response = await fetch("/api/v1/presets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: await file.text(),
        credentials: "same-origin",
      });
      if (response.ok) return location.reload();
      const problem = await response.json().catch(() => ({}));
      say(problem.detail || problem.title || "The preset couldn't be imported.");
    });
  }
});

function say(message) {
  const box = document.getElementById("messages");
  box.replaceChildren(Object.assign(document.createElement("p"), { className: "error", textContent: message }));
}
