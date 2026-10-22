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

  const initTooltips = (root = document) => {
    if (!window.bootstrap) return;
    root.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((element) => {
      if (element.dataset.tooltipReady === "1") return;
      element.dataset.tooltipReady = "1";
      new window.bootstrap.Tooltip(element);
    });
  };

  const initActionButtons = (root = document) => {
    root.querySelectorAll("[data-reload]").forEach((button) => {
      if (button.dataset.actionReady === "1") return;
      button.dataset.actionReady = "1";
      button.addEventListener("click", () => window.location.reload());
    });
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

  const focusFlash = () => {
    const flash = document.querySelector("#flash .alert");
    if (flash) flash.setAttribute("tabindex", "-1");
  };

  document.addEventListener("htmx:afterSwap", (event) => {
    initDropzones(event.target);
    initTooltips(event.target);
    initCopyButtons(event.target);
    initActionButtons(event.target);
    focusFlash();
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
    const flash = document.getElementById("flash");
    if (flash) {
      flash.innerHTML = `<div class="alert alert-danger d-flex gap-2 align-items-start" role="alert"><i class="bi bi-exclamation-octagon mt-1"></i><div>${message}</div></div>`;
    }
  });

  onReady(() => {
    initDropzones();
    initTooltips();
    initCopyButtons();
    initActionButtons();
    focusFlash();

    document.addEventListener("keydown", (event) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target;
      if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
      const search = document.querySelector("[data-quick-search]");
      if (!search) return;
      event.preventDefault();
      search.focus();
    });
  });
})();
