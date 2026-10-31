// Shortlist web app behavior: theme, toasts, the job drawer, keyboard shortcuts, tracker drag and drop, and the
// tag inputs on Settings. Plain JavaScript on top of htmx; no build step.
(() => {
  const root = document.documentElement;
  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];

  // --- theme: follows the system until toggled; the choice is remembered in this browser ---------------------------
  const darkNow = () => (root.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark";
  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-theme-toggle]")) return;
    const next = darkNow() ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("shortlist-theme", next); } catch (e) { /* private window: not remembered */ }
  });

  // --- toasts ------------------------------------------------------------------------------------------------------
  function toast(message, kind = "") {
    const box = $("#toasts");
    if (!box || !message) return;
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    el.textContent = message;
    box.append(el);
    setTimeout(() => { el.classList.add("leaving"); setTimeout(() => el.remove(), 320); }, kind === "error" ? 6500 : 2400);
  }
  document.body.addEventListener("toast", (event) => toast(event.detail.value));
  document.body.addEventListener("htmx:responseError", (event) => {
    let message = event.detail.xhr.responseText;
    try { message = JSON.parse(message).detail || message; } catch (e) { /* plain text */ }
    toast(message || `Error ${event.detail.xhr.status}`, "error");
  });
  // Confirmations (hx-confirm) in the app's own dialog instead of the browser's. htmx 2.0.4 skips its native
  // confirm() when the request is re-issued with issueRequest(true).
  document.body.addEventListener("htmx:confirm", (event) => {
    if (!event.detail.question) return;
    event.preventDefault();
    const dialog = $("#confirm-dialog");
    $("#confirm-text").textContent = event.detail.question;
    $("#confirm-ok").textContent = event.detail.elt.dataset.confirmOk || "Continue";
    dialog.returnValue = "";
    dialog.addEventListener("close", () => { if (dialog.returnValue === "ok") event.detail.issueRequest(true); },
                            { once: true });
    dialog.showModal();
  });
  document.body.addEventListener("htmx:sendError", () => toast("Could not reach Shortlist. Is it still running?", "error"));
  document.body.addEventListener("notesSaved", () => $$("[data-saved-hint]").forEach((el) => {
    el.classList.add("show");
    clearTimeout(el.hideTimer);
    el.hideTimer = setTimeout(() => el.classList.remove("show"), 1600);
  }));

  // --- job drawer ----------------------------------------------------------------------------------------------------
  function openDrawer() { $("#drawer").classList.add("open"); $("#backdrop").classList.add("open"); }
  function closeDrawer() {
    const drawer = $("#drawer");
    if (!drawer.classList.contains("open")) return;
    drawer.classList.remove("open");
    $("#backdrop").classList.remove("open");
    setTimeout(() => { if (!drawer.classList.contains("open")) drawer.innerHTML = ""; }, 320);  // or it reopens on refresh
  }
  document.body.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target.id === "drawer") openDrawer();
    if (event.detail.target.id === "rows" && selected >= 0) select(selected);
    const log = $("#activity pre");
    if (log) log.scrollTop = log.scrollHeight;
  });
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-close-drawer]") || event.target.id === "backdrop") closeDrawer();
    const expand = event.target.closest("[data-expand]");
    if (expand) {
      const text = document.getElementById(expand.dataset.expand);
      text.classList.toggle("clamped");
      expand.textContent = text.classList.contains("clamped") ? "Show the full description" : "Show less";
    }
    if (event.target.closest("[data-shortcuts]")) $("#shortcuts")?.toggleAttribute("hidden");
  });

  // --- keyboard: j/k to move through matches, letters to act on the selected one -----------------------------------
  let selected = -1;
  const items = () => $$(".match");
  function select(index) {
    const list = items();
    if (!list.length) { selected = -1; return; }
    selected = Math.max(0, Math.min(index, list.length - 1));
    list.forEach((el, i) => el.classList.toggle("selected", i === selected));
    list[selected].scrollIntoView({ block: "nearest" });
  }
  const act = (name) => items()[selected]?.querySelector(`[data-action="${name}"]`)?.click();
  const keys = {
    j: () => select(selected + 1), ArrowDown: () => select(selected + 1),
    k: () => select(selected - 1), ArrowUp: () => select(selected - 1),
    o: () => act("open"), Enter: () => act("open"), s: () => act("save"), a: () => act("apply"),
    x: () => act("hide"), g: () => act("good"), b: () => act("bad"),
  };
  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select, [contenteditable]")) {
      if (event.key === "Escape") event.target.blur();
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === "Escape") { closeDrawer(); $("#shortcuts")?.setAttribute("hidden", ""); return; }
    if (event.key === "/" && $("[data-search]")) { event.preventDefault(); $("[data-search]").focus(); return; }
    if (event.key === "?") { $("#shortcuts")?.toggleAttribute("hidden"); return; }
    if ($("#drawer").classList.contains("open") || !items().length || !keys[event.key]) return;
    event.preventDefault();
    keys[event.key]();
  });
  document.addEventListener("click", (event) => {
    const row = event.target.closest(".match");
    if (row) select(items().indexOf(row));
  });

  // --- tracker: drag a card onto another column to change its stage ------------------------------------------------
  document.addEventListener("dragstart", (event) => {
    const card = event.target.closest?.("[data-job]");
    if (!card) return;
    event.dataTransfer.setData("text/plain", card.dataset.job);
    event.dataTransfer.effectAllowed = "move";
    card.classList.add("dragging");
  });
  document.addEventListener("dragend", () => $$(".dragging, .column.over").forEach((el) => el.classList.remove("dragging", "over")));
  document.addEventListener("dragover", (event) => {
    const column = event.target.closest?.("[data-stage]");
    if (!column) return;
    event.preventDefault();
    $$(".column.over").forEach((el) => el !== column && el.classList.remove("over"));
    column.classList.add("over");
  });
  document.addEventListener("drop", (event) => {
    const column = event.target.closest?.("[data-stage]");
    const id = event.dataTransfer.getData("text/plain");
    if (!column || !id) return;
    event.preventDefault();
    column.classList.remove("over");
    htmx.ajax("POST", `/jobs/${id}/track`, { source: column, target: column, swap: "none", values: { stage: column.dataset.stage } });
  });

  // --- settings: tag inputs that keep a comma-separated list in the form's (hidden) textarea -----------------------
  function tagInput(box) {
    const field = $("textarea", box), input = $("input", box), list = $(".tag-list", box), count = $("[data-count]", box.parentElement);
    let values = field.value.split(/[,\n]/).map((s) => s.trim()).filter(Boolean);
    function render() {
      field.value = values.join(", ");
      list.replaceChildren(...values.map((value, i) => {
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.append(value);
        const remove = document.createElement("button");
        remove.type = "button";
        remove.setAttribute("aria-label", `Remove ${value}`);
        remove.textContent = "×";
        remove.addEventListener("click", () => { values.splice(i, 1); render(); input.focus(); });
        tag.append(remove);
        return tag;
      }));
      if (count) count.textContent = `${values.length} term${values.length === 1 ? "" : "s"}`;
    }
    function add(text) {
      for (const term of text.split(/[,\n]/).map((s) => s.trim()).filter(Boolean)) {
        if (!values.some((v) => v.toLowerCase() === term.toLowerCase())) values.push(term);
      }
      input.value = "";
      render();
    }
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === ",") { event.preventDefault(); add(input.value); }
      else if (event.key === "Backspace" && !input.value && values.length) { values.pop(); render(); }
    });
    input.addEventListener("blur", () => input.value.trim() && add(input.value));
    input.addEventListener("paste", (event) => {
      const text = event.clipboardData.getData("text");
      if (/[,\n]/.test(text)) { event.preventDefault(); add(text); }
    });
    box.addEventListener("click", (event) => { if (event.target === box) input.focus(); });
    render();
  }
  $$("[data-tags]").forEach(tagInput);

  // --- tables: filter rows as you type --------------------------------------------------------------------------------
  document.addEventListener("input", (event) => {
    const filter = event.target.closest("[data-filter]");
    if (!filter) return;
    const query = filter.value.trim().toLowerCase();
    $$(`${filter.dataset.filter} tbody tr`).forEach((tr) => { tr.hidden = query && !tr.textContent.toLowerCase().includes(query); });
  });
})();
