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

  // htmx doesn't show failed requests by default, so a refused action (for
  // example a cross-site check after opening the server at another address)
  // looked like nothing happening. Show the server's explanation instead.
  document.body.addEventListener("htmx:responseError", (event) => {
    const xhr = event.detail.xhr;
    let problem = {};
    try {
      problem = JSON.parse(xhr.responseText);
    } catch {
      // not a problem+json response
    }
    say(problem.detail || problem.title || `The request failed (HTTP ${xhr.status}).`);
  });
  document.body.addEventListener("htmx:sendError", () => {
    say("Couldn't reach the server. Check the connection and try again.");
  });

  // Game files: resumable, chunked upload of a RomFS archive (ADR 0013).
  const uploadBox = document.getElementById("romfs-upload");
  if (uploadBox && document.getElementById("romfs-start")) setUpRomfsUpload(uploadBox);
});

const UPLOAD_API = "/api/v1/romfs/upload";

function setUpRomfsUpload(box) {
  const input = document.getElementById("romfs-file");
  const start = document.getElementById("romfs-start");
  const discard = document.getElementById("romfs-discard");
  const bar = document.getElementById("romfs-progress");
  const status = document.getElementById("romfs-upload-status");
  const show = (text, isError = false) => {
    status.textContent = text;
    status.className = isError ? "error" : "";
  };
  const progress = (done, total) => {
    bar.hidden = false;
    bar.max = total || 1;
    bar.value = done;
  };
  const busy = (on) => {
    start.disabled = on;
    input.disabled = on;
    if (on) discard.hidden = true;
  };

  start.addEventListener("click", async () => {
    const file = input.files[0];
    if (!file) return show("Choose the RomFS archive first.", true);
    busy(true);
    const lock = await wakeLock();
    try {
      let upload = await api("POST", UPLOAD_API, {
        size: file.size,
        fingerprint: `${file.name}|${file.size}|${file.lastModified}`,
      });
      upload = await sendChunks(file, upload, (u) => {
        progress(u.offset, u.size);
        show(`Uploading: ${percent(u.offset, u.size)} of ${size(u.size)}…`);
      });
      show("Upload complete. Checking and installing…");
      await api("POST", `${UPLOAD_API}/${upload.id}/install`);
      await followInstall(show, progress);
    } catch (error) {
      show(error.message, true);
      busy(false);
    } finally {
      if (lock) lock.release().catch(() => {});
    }
  });

  discard.addEventListener("click", async () => {
    if (!confirm("Discard the unfinished upload?")) return;
    try {
      await api("DELETE", `${UPLOAD_API}/${box.dataset.id}`);
      location.reload();
    } catch (error) {
      show(error.message, true);
    }
  });

  if (box.dataset.state === "installing") {
    busy(true);
    followInstall(show, progress).catch((error) => show(error.message, true));
  }
}

async function sendChunks(file, upload, onProgress) {
  let failures = 0;
  while (upload.offset < upload.size) {
    onProgress(upload);
    const end = Math.min(upload.offset + upload.chunk_size, upload.size);
    try {
      const response = await fetch(`${UPLOAD_API}/${upload.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/offset+octet-stream",
          "Upload-Offset": String(upload.offset),
        },
        body: file.slice(upload.offset, end),
        credentials: "same-origin",
      });
      if (response.status === 409) {
        const problem = await response.json().catch(() => ({}));
        if (typeof problem.offset !== "number") throw new Error(problem.detail || "Upload refused.");
        upload = { ...upload, offset: problem.offset }; // continue where the server is
        continue;
      }
      if (!response.ok) throw await problemError(response, "The upload failed.");
      upload = await response.json();
      failures = 0;
    } catch (error) {
      if (!(error instanceof TypeError)) throw error; // not a network error
      if (++failures > 6) {
        throw new Error("The upload paused: check the connection, then press Upload again to continue.");
      }
      await sleep(1000 * 2 ** (failures - 1));
      upload = await api("GET", UPLOAD_API).catch(() => upload);
    }
  }
  onProgress(upload);
  return upload;
}

async function followInstall(show, progress) {
  for (;;) {
    const upload = await api("GET", UPLOAD_API);
    if (upload.state === "installed") {
      show(upload.message || "Installed.");
      return location.reload();
    }
    if (upload.state === "failed") throw new Error(upload.message || "The install failed.");
    if (upload.total_bytes) {
      progress(upload.installed_bytes, upload.total_bytes);
      show(`Installing: ${percent(upload.installed_bytes, upload.total_bytes)} extracted…`);
    } else {
      show(upload.message || "Installing…");
    }
    await sleep(2000);
  }
}

async function api(method, url, body) {
  const response = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  if (!response.ok) throw await problemError(response, "The request failed.");
  return response.status === 204 ? null : response.json();
}

async function problemError(response, fallback) {
  const problem = await response.json().catch(() => ({}));
  return new Error(problem.detail || problem.title || fallback);
}

async function wakeLock() {
  // Keep a phone's screen on during a long upload, where supported.
  try {
    return await navigator.wakeLock?.request("screen");
  } catch {
    return null;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const percent = (done, total) => `${Math.floor((100 * done) / (total || 1))}%`;
const size = (bytes) => `${(bytes / 1024 ** 3).toFixed(1)} GB`;

function say(message) {
  const box = document.getElementById("messages");
  box.replaceChildren(Object.assign(document.createElement("p"), { className: "error", textContent: message }));
}
