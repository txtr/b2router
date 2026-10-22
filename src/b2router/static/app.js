(() => {
  "use strict";

  const onReady = (fn) => {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn, { once: true });
    } else {
      fn();
    }
  };

  const initDropzones = (root = document) => {
    root.querySelectorAll("[data-dropzone]").forEach((zone) => {
      if (zone.dataset.ready === "1") return;
      zone.dataset.ready = "1";
      const input = zone.querySelector('input[type="file"]');
      const list = zone.querySelector("[data-file-list]");
      if (!input) return;

      const render = () => {
        if (!list) return;
        const names = Array.from(input.files || []).map((file) => file.name);
        list.textContent = names.length
          ? names.join(" · ")
          : input.multiple
            ? "No files selected yet"
            : "No file selected yet";
      };

      input.addEventListener("change", render);
      render();

      ["dragenter", "dragover"].forEach((eventName) => {
        zone.addEventListener(eventName, (event) => {
          event.preventDefault();
          zone.classList.add("dragover");
        });
      });
      ["dragleave", "drop"].forEach((eventName) => {
        zone.addEventListener(eventName, (event) => {
          event.preventDefault();
          zone.classList.remove("dragover");
        });
      });
      zone.addEventListener("drop", (event) => {
        if (!event.dataTransfer || !event.dataTransfer.files.length) return;
        const transfer = new DataTransfer();
        Array.from(event.dataTransfer.files).forEach((file) => transfer.items.add(file));
        input.files = transfer.files;
        render();
      });
      zone.addEventListener("click", (event) => {
        if (event.target !== input) input.click();
      });
    });
  };

  const initActionButtons = (root = document) => {
    root.querySelectorAll("[data-back]").forEach((button) => {
      if (button.dataset.actionReady === "1") return;
      button.dataset.actionReady = "1";
      button.addEventListener("click", () => window.history.back());
    });
  };

  const initCopyButtons = (root = document) => {
    root.querySelectorAll("[data-copy]").forEach((button) => {
      if (button.dataset.copyReady === "1") return;
      button.dataset.copyReady = "1";
      button.addEventListener("click", async () => {
        const value = button.getAttribute("data-copy") || "";
        try {
          await navigator.clipboard.writeText(value);
          const original = button.innerHTML;
          button.innerHTML = '<i class="bi bi-check2"></i>';
          window.setTimeout(() => {
            button.innerHTML = original;
          }, 1200);
        } catch {
          window.prompt("Copy this value", value);
        }
      });
    });
  };

  document.addEventListener("htmx:afterSwap", (event) => {
    initDropzones(event.target);
    initCopyButtons(event.target);
    initActionButtons(event.target);
  });

  document.addEventListener("htmx:beforeSwap", (event) => {
    const xhr = event.detail.xhr;
    const contentType = xhr.getResponseHeader("Content-Type") || "";
    if (xhr.status >= 400 && contentType.includes("text/html")) {
      // Problem fragments from the UI carry HX-Retarget/HX-Reswap. Allow the
      // real status to reach HTMX without replacing the mutation's target.
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  const showError = (message) => {
    const flash = document.getElementById("flash");
    if (!flash) return;
    const alert = document.createElement("div");
    alert.className = "alert alert-danger d-flex gap-2 align-items-start";
    alert.setAttribute("role", "alert");
    const icon = document.createElement("i");
    icon.className = "bi bi-exclamation-octagon mt-1";
    const text = document.createElement("div");
    // Never interpolate server-provided text into innerHTML: problem details
    // can contain a filename, which is attacker-controlled.
    text.textContent = message;
    alert.append(icon, text);
    flash.replaceChildren(alert);
  };

  document.addEventListener("htmx:responseError", (event) => {
    const xhr = event.detail.xhr;
    let message = "The request failed.";
    try {
      const problem = JSON.parse(xhr.responseText);
      message = problem.detail || problem.title || message;
      if (problem.request_id) message += ` (request ${problem.request_id})`;
    } catch {
      /* not JSON */
    }
    showError(message);
  });

  onReady(() => {
    initDropzones();
    initCopyButtons();
    initActionButtons();

    document.addEventListener("keydown", (event) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target;
      if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
      const search = document.querySelector("[data-quick-search]");
      if (!search) return;
      event.preventDefault();
      search.focus();
    });

    document.addEventListener("click", (event) => {
      document.querySelectorAll("details.filters[open]").forEach((details) => {
        if (!details.contains(event.target)) details.open = false;
      });
    });

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      document.querySelectorAll("details.filters[open]").forEach((details) => {
        details.open = false;
      });
    });
  });
})();
