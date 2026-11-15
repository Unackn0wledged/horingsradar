/* Høringsradar static site. Vanilla JS, no dependencies.
 * All data from data.json is rendered with createElement/textContent only. */
(function () {
  "use strict";

  // ---------------------------------------------------------------- constants

  const REL_ORDER = { hoy: 0, middels: 1, lav: 2, ingen: 3, feilet: 4, ikke_vurdert: 5 };
  const REL_FALLBACK_LABEL = { hoy: "Høy", middels: "Middels", lav: "Lav", ingen: "Ingen", feilet: "Feilet", ikke_vurdert: "Ikke vurdert" };
  const TAB_TITLES = {
    aktuelle: "På høring",
    nye: "Nye og endrede",
    alle: "Alle høringer",
    folger: "Høringer du følger",
  };
  const PERIODS = ["1", "2", "3", "4", "8", "all"];
  const SORTS = ["relevans", "frist", "publisert"];
  const THEMES = ["system", "light", "dark"];
  const SOON_DAYS = 21;
  const URGENT_DAYS = 7;
  const SESSION_GAP_MS = 30 * 60 * 1000;
  const FIRST_VISIT_DAYS = 7;

  // ------------------------------------------------------------------ storage

  const memStore = {};

  function storeGet(key) {
    let value = null;
    try {
      value = window.localStorage.getItem("hr." + key);
    } catch (e) {
      value = null;
    }
    if (value === null && Object.prototype.hasOwnProperty.call(memStore, key)) value = memStore[key];
    return value;
  }

  function storeSet(key, value) {
    memStore[key] = value;
    try {
      window.localStorage.setItem("hr." + key, value);
    } catch (e) {
      /* storage unavailable: the in-memory copy keeps the session working */
    }
  }

  function storeGetJSON(key, fallback) {
    const raw = storeGet(key);
    if (raw === null) return fallback;
    try {
      const parsed = JSON.parse(raw);
      return parsed === null || parsed === undefined ? fallback : parsed;
    } catch (e) {
      return fallback;
    }
  }

  function storeSetJSON(key, value) {
    storeSet(key, JSON.stringify(value));
  }

  // -------------------------------------------------------------------- dates

  function pad(n) {
    return n < 10 ? "0" + n : String(n);
  }

  function todayIso() {
    const d = new Date();
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function dayNumber(iso) {
    const p = String(iso).slice(0, 10).split("-").map(Number);
    return Math.round(Date.UTC(p[0], p[1] - 1, p[2]) / 86400000);
  }

  function formatDate(iso) {
    if (!iso) return "–";
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso));
    return m ? m[3] + "." + m[2] + "." + m[1] : String(iso);
  }

  function daysText(n) {
    if (n < 0) return "utløpt";
    if (n === 0) return "i dag";
    if (n === 1) return "1 dag igjen";
    return n + " dager igjen";
  }

  // ---------------------------------------------------------------------- DOM

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const key of Object.keys(attrs)) {
        const val = attrs[key];
        if (val === null || val === undefined || val === false) continue;
        if (key === "class") node.className = val;
        else if (key === "text") node.textContent = val;
        else node.setAttribute(key, val === true ? "" : String(val));
      }
    }
    if (children) append(node, children);
    return node;
  }

  function append(node, children) {
    const list = Array.isArray(children) ? children : [children];
    for (const c of list) {
      if (c === null || c === undefined || c === false) continue;
      if (Array.isArray(c)) append(node, c);
      else node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    }
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function $(id) {
    return document.getElementById(id);
  }

  /** Returns a safe href: http(s) absolute URL or relative pdf/<digits>.pdf, otherwise null. */
  function safeHref(u) {
    if (typeof u !== "string" || !u) return null;
    if (/^pdf\/\d+\.pdf$/.test(u)) return u;
    try {
      const url = new URL(u);
      if (url.protocol === "https:" || url.protocol === "http:") return url.href;
    } catch (e) {
      /* not an absolute URL */
    }
    return null;
  }

  function extLink(href, text, cls) {
    const safe = safeHref(href);
    if (!safe) return el("span", { class: cls, text: text });
    return el("a", { href: safe, target: "_blank", rel: "noopener noreferrer", class: cls, text: text });
  }

  function announce(msg) {
    const live = $("live");
    live.textContent = "";
    window.setTimeout(function () {
      live.textContent = msg;
    }, 30);
  }

  // -------------------------------------------------------------------- state

  const state = {
    data: null,
    hearings: [],
    byId: new Map(),
    today: todayIso(),
    refMs: 0,
    firstVisit: false,
    tab: "aktuelle",
    period: "all",
    sort: "relevans",
    search: "",
    filters: { rel: new Set(), sector: "", dept: "", addr: "", due: false },
    open: { low: false, unassessed: false },
    follow: new Set(),
    read: {},
    drawerId: null,
    drawerPushed: false,
    drawerOpener: null,
    menu: { open: false, button: null },
    facetsOpen: false,
    theme: "system",
    pager: [],
  };

  function relRank(h) {
    return Object.prototype.hasOwnProperty.call(REL_ORDER, h.relevance) ? REL_ORDER[h.relevance] : 5;
  }

  function relGroup(h) {
    if (h.relevance === "hoy" || h.relevance === "middels") return "main";
    if (h.relevance === "lav" || h.relevance === "ingen") return "low";
    return "unassessed";
  }

  function relLabel(h) {
    return h.relevance_label || REL_FALLBACK_LABEL[h.relevance] || "Ikke vurdert";
  }

  function daysLeft(h) {
    return h.deadline ? dayNumber(h.deadline) - dayNumber(state.today) : null;
  }

  function isUnread(h) {
    const readAt = state.read[h.id];
    if (!readAt) return true;
    if (h.changed_at && Date.parse(readAt) < Date.parse(h.changed_at)) return true;
    return false;
  }

  // ------------------------------------------------------------ data + visits

  function setupVisit(generatedAt) {
    const now = Date.now();
    const last = Date.parse(storeGet("lastVisit") || "");
    let prev = Date.parse(storeGet("prevVisit") || "");
    if (!isNaN(last) && now - last > SESSION_GAP_MS) {
      prev = last;
      storeSet("prevVisit", new Date(last).toISOString());
    }
    storeSet("lastVisit", new Date(now).toISOString());
    if (!isNaN(prev)) {
      state.refMs = prev;
      state.firstVisit = false;
    } else {
      const gen = Date.parse(generatedAt || "");
      state.refMs = (isNaN(gen) ? now : gen) - FIRST_VISIT_DAYS * 86400000;
      state.firstVisit = true;
    }
  }

  function prepare(data) {
    state.data = data;
    state.hearings = Array.isArray(data.hearings) ? data.hearings : [];
    state.byId = new Map();
    for (const h of state.hearings) {
      h.findings = h.findings || [];
      h.documents = h.documents || [];
      h.sectors = h.sectors || [];
      h.changes = h.changes || [];
      h.notes = h.notes || [];
      h.reading_guide = h.reading_guide || [];
      h._new = !!h.first_seen && Date.parse(h.first_seen) > state.refMs;
      h._changed = !h._new && !!h.changed_at && Date.parse(h.changed_at) > state.refMs;
      h._search = [h.title, h.department, h.summary, h.relevance_reason, h.sectors.join(" ")]
        .filter(Boolean)
        .join(" ")
        .toLocaleLowerCase("nb");
      state.byId.set(h.id, h);
    }
  }

  // ---------------------------------------------------------------- filtering

  function inPeriod(h) {
    if (state.period === "all") return true;
    const weeks = Number(state.period);
    const ref = state.data.generated_at ? dayNumber(state.data.generated_at) : dayNumber(state.today);
    return !!h.published && dayNumber(h.published) >= ref - weeks * 7;
  }

  function periodList() {
    return state.hearings.filter(inPeriod);
  }

  function tabList() {
    if (state.tab === "folger") return state.hearings.filter((h) => state.follow.has(h.id));
    const base = periodList();
    if (state.tab === "aktuelle") return base.filter((h) => h.status === "open");
    if (state.tab === "nye") return base.filter((h) => h._new || h._changed);
    return base;
  }

  /** True if h passes search and filters. `skip` names one facet to ignore (for facet counts). */
  function matchesFilters(h, skip) {
    const f = state.filters;
    if (skip !== "rel" && f.rel.size && !f.rel.has(h.relevance)) return false;
    if (f.sector && h.sectors.indexOf(f.sector) === -1) return false;
    if (f.dept && h.department !== f.dept) return false;
    if (f.addr === "yes" && h.on_address_list !== true) return false;
    if (f.addr === "no" && h.on_address_list !== false) return false;
    if (f.due) {
      const d = daysLeft(h);
      if (h.status !== "open" || d === null || d < 0 || d > SOON_DAYS) return false;
    }
    if (state.search && h._search.indexOf(state.search) === -1) return false;
    return true;
  }

  function cmpDeadline(a, b) {
    if (a.deadline && b.deadline) return a.deadline < b.deadline ? -1 : a.deadline > b.deadline ? 1 : 0;
    if (a.deadline) return -1;
    if (b.deadline) return 1;
    return 0;
  }
  function cmpPublished(a, b) {
    const pa = a.published || "";
    const pb = b.published || "";
    return pa < pb ? 1 : pa > pb ? -1 : 0;
  }
  function cmpRel(a, b) {
    return relRank(a) - relRank(b);
  }
  function cmpTitle(a, b) {
    return String(a.title).localeCompare(String(b.title), "nb");
  }

  function sortList(list) {
    let chain;
    if (state.sort === "frist") chain = [cmpDeadline, cmpRel, cmpPublished, cmpTitle];
    else if (state.sort === "publisert") chain = [cmpPublished, cmpRel, cmpDeadline, cmpTitle];
    else chain = [cmpRel, cmpDeadline, cmpPublished, cmpTitle];
    return list.slice().sort(function (a, b) {
      for (const fn of chain) {
        const r = fn(a, b);
        if (r !== 0) return r;
      }
      return 0;
    });
  }

  // ---------------------------------------------------------------- rendering

  const REL_LEVELS = ["hoy", "middels", "lav", "ingen"];
  const GROUP_TITLES = { hoy: "Høy relevans", middels: "Middels relevans" };
  const PERIOD_TEXT = { 1: "siste uke", 2: "siste 2 uker", 3: "siste 3 uker", 4: "siste 4 uker", 8: "siste 8 uker" };
  const ICON_BOOKMARK = "M6 4h12v17l-6-4-6 4z";

  function relKey(h) {
    return REL_ORDER[h.relevance] !== undefined ? h.relevance : "ikke_vurdert";
  }

  function svgIcon(d) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "20");
    svg.setAttribute("height", "20");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.8");
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path);
    return svg;
  }

  function currentItems() {
    return sortList(tabList().filter((h) => matchesFilters(h)));
  }

  /** The hearings in the order they appear on the page (main group, then low, then not assessed). */
  function displayOrder(items) {
    const list = items || currentItems();
    return list
      .filter((h) => relGroup(h) === "main")
      .concat(list.filter((h) => relGroup(h) === "low"))
      .concat(list.filter((h) => relGroup(h) === "unassessed"));
  }

  function activeFilterCount() {
    const f = state.filters;
    return (f.rel.size ? 1 : 0) + (f.sector ? 1 : 0) + (f.dept ? 1 : 0) + (f.addr ? 1 : 0) + (f.due ? 1 : 0) + (state.period !== "all" ? 1 : 0);
  }

  function renderAll() {
    renderNav();
    renderFacetCounts();
    renderHead();
    renderChips();
    renderList();
    syncControls();
  }

  function setPressed(selector, attr, value) {
    for (const b of document.querySelectorAll(selector)) {
      b.setAttribute("aria-pressed", b.getAttribute(attr) === value ? "true" : "false");
    }
  }

  function syncControls() {
    setPressed("#nav .tab", "data-tab", state.tab);
    setPressed("#seg-sort button", "data-sort", state.sort);
    setPressed("#seg-due button", "data-due", state.filters.due ? "1" : "");
    setPressed("#seg-addr button", "data-addr", state.filters.addr);
    setPressed("#seg-theme button", "data-theme-choice", state.theme);
    for (const b of document.querySelectorAll("#rel-facet .rel-opt")) {
      b.setAttribute("aria-pressed", state.filters.rel.has(b.getAttribute("data-rel")) ? "true" : "false");
    }
    $("period-select").value = state.period;
    $("f-sector").value = state.filters.sector;
    $("f-dept").value = state.filters.dept;
    const n = activeFilterCount();
    $("filter-toggle-text").textContent = n ? "Filter (" + n + ")" : "Filter";
    $("filter-toggle").setAttribute("aria-expanded", state.facetsOpen ? "true" : "false");
    $("facet-body").classList.toggle("is-open", state.facetsOpen);
    $("filter-reset").hidden = n === 0 && !state.search;
  }

  function renderNav() {
    const base = periodList();
    const counts = {
      aktuelle: base.filter((h) => h.status === "open").length,
      nye: base.filter((h) => h._new || h._changed).length,
      alle: base.length,
      folger: state.hearings.filter((h) => state.follow.has(h.id)).length,
    };
    for (const node of document.querySelectorAll("[data-count]")) {
      node.textContent = String(counts[node.getAttribute("data-count")]);
    }
  }

  function renderFacetCounts() {
    const list = tabList().filter((h) => matchesFilters(h, "rel"));
    for (const lvl of REL_LEVELS) {
      const n = list.filter((h) => h.relevance === lvl).length;
      const node = document.querySelector('[data-rel-count="' + lvl + '"]');
      node.textContent = String(n);
      node.parentNode.setAttribute("aria-label", REL_FALLBACK_LABEL[lvl] + " relevans, " + n + " høringer");
    }
  }

  function renderHead() {
    $("view-title").textContent = TAB_TITLES[state.tab];
    const shown = tabList().filter((h) => matchesFilters(h)).length;
    const total = tabList().length;
    let text = shown === 1 ? "1 høring" : shown + " høringer";
    if (shown !== total) text += " av " + total;
    if (state.tab === "nye") {
      text += state.firstVisit ? " · siste 7 dager" : " · siden forrige besøk " + formatDate(new Date(state.refMs).toISOString());
    }
    if (state.period !== "all" && state.tab !== "folger") text += " · publisert " + PERIOD_TEXT[state.period];
    $("view-sub").textContent = text;
  }

  function chipSpecs() {
    const f = state.filters;
    const specs = [];
    if (f.rel.size) {
      const labels = REL_LEVELS.filter((r) => f.rel.has(r)).map((r) => REL_FALLBACK_LABEL[r]);
      specs.push({ text: "Relevans: " + labels.join(", "), clear: () => f.rel.clear() });
    }
    if (f.due) specs.push({ text: "Frist innen 3 uker", clear: () => (f.due = false) });
    if (f.addr) specs.push({ text: "På adresselisten: " + (f.addr === "yes" ? "Ja" : "Nei"), clear: () => (f.addr = "") });
    if (f.sector) specs.push({ text: "Sektor: " + f.sector, clear: () => (f.sector = "") });
    if (f.dept) specs.push({ text: f.dept, clear: () => (f.dept = "") });
    if (state.period !== "all") {
      specs.push({
        text: "Publisert " + PERIOD_TEXT[state.period],
        clear: function () {
          state.period = "all";
          storeSet("period", state.period);
        },
      });
    }
    return specs;
  }

  function renderChips() {
    const box = $("chips");
    clear(box);
    for (const s of chipSpecs()) {
      const b = el("button", { type: "button", class: "chip", "aria-label": "Fjern filter: " + s.text }, [
        el("span", { text: s.text }),
        el("span", { class: "chip-x", "aria-hidden": "true", text: "×" }),
      ]);
      b.addEventListener("click", function () {
        s.clear();
        renderAll();
      });
      box.appendChild(b);
    }
  }

  function relBadge(h, long) {
    let text = relLabel(h);
    if (long && REL_LEVELS.indexOf(h.relevance) !== -1) text += " relevans";
    return el("span", { class: "rel-badge rel-" + relKey(h), text: text });
  }

  function statusBadge(h) {
    return el("span", { class: "status-badge" + (h.status === "open" ? " is-open" : ""), text: h.status_label || "Ukjent" });
  }

  function newTag(h) {
    if (h._new) return el("span", { class: "tag tag-new", text: "Ny" });
    if (h._changed) return el("span", { class: "tag tag-changed", text: "Endret" });
    return null;
  }

  function urgency(h, d) {
    if (h.status !== "open" || d === null || d < 0) return "";
    if (d <= URGENT_DAYS) return "urgent";
    if (d <= SOON_DAYS) return "soon";
    return "";
  }

  function deadlineBox(h) {
    const d = daysLeft(h);
    if (d === null) return el("div", { class: "deadline" }, el("span", { class: "deadline-none", text: "Ingen frist" }));
    const left = el("div", { class: "deadline-left", text: daysText(d) });
    const u = urgency(h, d);
    if (u) left.classList.add(u);
    return el("div", { class: "deadline" }, [el("div", { class: "deadline-date", text: formatDate(h.deadline) }), left]);
  }

  function addrText(h) {
    return h.on_address_list === true ? "Ja" : h.on_address_list === false ? "Nei" : "–";
  }

  function reasonText(h) {
    if (h.relevance === "feilet") return "Vurderingen feilet og prøves på nytt ved neste kjøring.";
    if (relKey(h) === "ikke_vurdert") return "Venter på vurdering.";
    return h.relevance_reason || "";
  }

  function followButton(h) {
    const on = state.follow.has(h.id);
    const btn = el("button", {
      type: "button",
      class: "icon-btn follow-btn",
      "aria-pressed": on ? "true" : "false",
      "aria-label": "Følg " + h.title,
      title: on ? "Du følger denne. Klikk for å slutte å følge." : "Følg",
      "data-follow": h.id,
    });
    btn.appendChild(svgIcon(ICON_BOOKMARK));
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      toggleFollow(h);
      const again = document.querySelector('[data-follow="' + h.id + '"]');
      if (again) again.focus();
    });
    return btn;
  }

  function menuButton(h) {
    const btn = el("button", {
      type: "button",
      class: "icon-btn row-menu-btn",
      "aria-label": "Lenker for " + h.title,
      "aria-haspopup": "menu",
      "aria-expanded": "false",
      title: "Regjeringen.no og PDF",
      text: "⋯",
    });
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (state.menu.open && state.menu.button === btn) closeMenu(true);
      else openMenu(h, btn);
    });
    return btn;
  }

  function buildRow(h) {
    const unread = isUnread(h);
    const li = el("li", { class: "row rel-" + relKey(h) + (unread ? " is-unread" : "") });

    const main = el("div", { class: "row-main" });
    main.appendChild(el("a", { class: "title-link", href: "#h=" + h.id, "data-open": h.id, text: h.title }));
    if (unread) main.appendChild(el("span", { class: "sr-only", text: " (ikke åpnet)" }));
    const tag = newTag(h);
    if (tag) main.appendChild(tag);
    const reason = reasonText(h);
    if (reason) main.appendChild(el("p", { class: "reason", text: reason }));

    const meta = el("div", { class: "row-meta" }, relBadge(h, false));
    const parts = [h.department || "Ukjent departement", "publisert " + formatDate(h.published)];
    if (h.sectors.length) parts.push(h.sectors.join(", "));
    parts.forEach(function (p, i) {
      if (i > 0) meta.appendChild(el("span", { class: "sep", "aria-hidden": "true", text: "·" }));
      meta.appendChild(el("span", { text: p }));
    });
    if (h.on_address_list === true) meta.appendChild(el("span", { class: "addr-tag", title: "Datatilsynet står på adresselisten", text: "Adresseliste" }));
    if (h.status !== "open") meta.appendChild(statusBadge(h));
    main.appendChild(meta);

    li.appendChild(main);
    li.appendChild(deadlineBox(h));
    li.appendChild(el("div", { class: "row-actions" }, [followButton(h), menuButton(h)]));
    return li;
  }

  function buildRows(items) {
    const ul = el("ul", { class: "rows" });
    for (const h of items) ul.appendChild(buildRow(h));
    return ul;
  }

  function groupHeader(title, n, cls) {
    return el("h2", { class: "group-h" + (cls ? " " + cls : "") }, [title + " ", el("span", { class: "n", text: "(" + n + ")" })]);
  }

  function buildCollapsible(key, title, items, panelId) {
    const toggle = el("button", {
      type: "button",
      class: "section-toggle",
      "aria-expanded": state.open[key] ? "true" : "false",
      "aria-controls": panelId,
    });
    append(toggle, [el("span", { text: title + " (" + items.length + ")" }), el("span", { class: "hint", "aria-hidden": "true", text: state.open[key] ? "Skjul" : "Vis" })]);
    const panel = el("div", { id: panelId }, buildRows(items));
    panel.hidden = !state.open[key];
    toggle.addEventListener("click", function () {
      state.open[key] = !state.open[key];
      toggle.setAttribute("aria-expanded", state.open[key] ? "true" : "false");
      toggle.querySelector(".hint").textContent = state.open[key] ? "Skjul" : "Vis";
      panel.hidden = !state.open[key];
    });
    return el("section", { class: "group collapsible" }, [el("h2", { class: "sr-only", text: title }), toggle, panel]);
  }

  function emptyState() {
    if (state.tab === "folger" && state.follow.size === 0) {
      return el("div", { class: "empty" }, [
        el("strong", { text: "Du følger ingen høringer ennå" }),
        el("span", { text: "Trykk på bokmerket ved en høring i listen, eller «Følg» på høringssiden. Valget lagres bare i din nettleser." }),
      ]);
    }
    return el("div", { class: "empty" }, [el("strong", { text: "Ingen høringer i valgt periode og filter" }), el("span", { text: "Prøv en lengre periode, eller nullstill filter og søk." })]);
  }

  function renderList() {
    const box = $("list");
    clear(box);
    const items = currentItems();
    if (!items.length) {
      box.appendChild(emptyState());
      return;
    }
    const main = items.filter((h) => relGroup(h) === "main");
    const low = items.filter((h) => relGroup(h) === "low");
    const unassessed = items.filter((h) => relGroup(h) === "unassessed");
    if (main.length && state.sort === "relevans") {
      for (const lvl of ["hoy", "middels"]) {
        const sub = main.filter((h) => h.relevance === lvl);
        if (sub.length) box.appendChild(el("section", { class: "group" }, [groupHeader(GROUP_TITLES[lvl], sub.length, "rel-" + lvl), buildRows(sub)]));
      }
    } else if (main.length) {
      box.appendChild(el("section", { class: "group" }, [groupHeader("Høy og middels relevans", main.length, ""), buildRows(main)]));
    } else {
      box.appendChild(el("p", { class: "group-note", text: "Ingen høringer med høy eller middels relevans i dette utvalget." }));
    }
    if (low.length) box.appendChild(buildCollapsible("low", "Lav og ingen relevans", low, "panel-low"));
    if (unassessed.length) box.appendChild(buildCollapsible("unassessed", "Ikke vurdert ennå", unassessed, "panel-unassessed"));
  }

  // -------------------------------------------------------------------- follow

  function persistFollow() {
    storeSetJSON("follow", Array.from(state.follow));
  }

  function toggleFollow(h) {
    if (state.follow.has(h.id)) state.follow.delete(h.id);
    else state.follow.add(h.id);
    persistFollow();
    announce(state.follow.has(h.id) ? "Du følger nå denne høringen" : "Du følger ikke lenger denne høringen");
    renderAll();
  }

  // ---------------------------------------------------------------- row menu

  function openMenu(h, btn) {
    closeMenu(false);
    const menu = $("row-menu");
    clear(menu);
    const src = safeHref(h.url);
    if (src) menu.appendChild(el("a", { class: "menu-item", role: "menuitem", href: src, target: "_blank", rel: "noopener noreferrer", text: "Åpne på regjeringen.no" }));
    const pdf = safeHref(h.pdf);
    if (pdf) menu.appendChild(el("a", { class: "menu-item", role: "menuitem", href: pdf, download: "", text: "Last ned PDF" }));
    const items = menu.querySelectorAll(".menu-item");
    if (!items.length) return;
    for (const a of items) a.addEventListener("click", () => closeMenu(false));

    menu.hidden = false;
    const rect = btn.getBoundingClientRect();
    const w = menu.offsetWidth;
    const hgt = menu.offsetHeight;
    const left = Math.min(Math.max(8, rect.right - w), window.innerWidth - w - 8);
    let top = rect.bottom + 4;
    if (top + hgt > window.innerHeight - 8) top = Math.max(8, rect.top - hgt - 4);
    menu.style.left = left + "px";
    menu.style.top = top + "px";
    btn.setAttribute("aria-expanded", "true");
    state.menu = { open: true, button: btn };
    items[0].focus();
  }

  function closeMenu(returnFocus) {
    if (!state.menu.open) return;
    const btn = state.menu.button;
    $("row-menu").hidden = true;
    if (btn) {
      btn.setAttribute("aria-expanded", "false");
      if (returnFocus && document.contains(btn)) btn.focus();
    }
    state.menu = { open: false, button: null };
  }

  function onMenuKey(ev) {
    const items = Array.from($("row-menu").querySelectorAll(".menu-item"));
    const idx = items.indexOf(document.activeElement);
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      items[(idx + 1) % items.length].focus();
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      items[(idx - 1 + items.length) % items.length].focus();
    } else if (ev.key === "Home") {
      ev.preventDefault();
      items[0].focus();
    } else if (ev.key === "End") {
      ev.preventDefault();
      items[items.length - 1].focus();
    } else if (ev.key === "Tab") {
      closeMenu(false);
    }
  }

  // ------------------------------------------------------------------ facets

  function fillSelect(select, values) {
    for (const v of values) select.appendChild(el("option", { value: v, text: v }));
  }

  function setupFilterOptions() {
    const sectors = new Set();
    const depts = new Set();
    for (const h of state.hearings) {
      for (const s of h.sectors) sectors.add(s);
      if (h.department) depts.add(h.department);
    }
    const sort = (set) => Array.from(set).sort((a, b) => a.localeCompare(b, "nb"));
    fillSelect($("f-sector"), sort(sectors));
    fillSelect($("f-dept"), sort(depts));
  }

  function resetFilters() {
    state.filters = { rel: new Set(), sector: "", dept: "", addr: "", due: false };
    state.search = "";
    $("search").value = "";
    state.period = "all";
    storeSet("period", state.period);
  }

  // ------------------------------------------------------------- detail page

  function section(title, children) {
    return el("section", { class: "dsec" }, [el("h3", { text: title }), children]);
  }

  function factRow(dl, label, value) {
    dl.appendChild(el("dt", { text: label }));
    dl.appendChild(el("dd", null, value));
  }

  function rangeLinks(ranges) {
    const out = [];
    ranges.forEach(function (r, i) {
      if (i > 0) out.push(i === ranges.length - 1 ? " og " : ", ");
      out.push(extLink(r.href, r.label));
    });
    return out;
  }

  function buildFacts(h) {
    const dl = el("dl", { class: "facts" });
    const dept = [h.department || "–"];
    if (h.original_department && h.original_department !== h.department) {
      dept.push(el("div", { class: "muted small", text: "opprinnelig utgitt av " + h.original_department }));
    }
    factRow(dl, "Departement", dept);
    factRow(dl, "Publisert", formatDate(h.published));
    const d = daysLeft(h);
    factRow(dl, "Frist", d === null ? "–" : formatDate(h.deadline) + " (" + daysText(d) + ")");
    factRow(dl, "Status", h.status_label || "Ukjent");
    factRow(dl, "Datatilsynet på adresselisten", addrText(h));
    factRow(dl, "Sektor", h.sectors.length ? h.sectors.join(", ") : "–");

    const ps = h.privacy_section;
    let psVal;
    if (!ps) psVal = "–";
    else if (!ps.exists) psVal = "Nei";
    else {
      psVal = ["Ja, kapittel «" + (ps.chapter || "") + "»"];
      if (ps.pages && ps.pages.length) {
        psVal.push(", s. ");
        psVal.push(rangeLinks(ps.pages));
      }
      if (ps.document_title) psVal.push(", i " + ps.document_title);
    }
    factRow(dl, "Eget personvernkapittel", psVal);

    if (h.assessed_at) {
      const v = [formatDate(h.assessed_at)];
      const tech = [h.model, h.prompt_version].filter(Boolean).join(" / ");
      if (tech) v.push(el("div", { class: "muted small", text: tech }));
      factRow(dl, "Vurdert", v);
    } else {
      factRow(dl, "Vurdert", "–");
    }
    return dl;
  }

  function buildFinding(f) {
    const verified = f.verified === true;
    const head = el("div", { class: "finding-head" }, [el("strong", { text: f.category_label || "Funn" })]);
    if (f.document_title) head.appendChild(document.createTextNode(" · " + f.document_title));
    if (f.chapter) head.appendChild(document.createTextNode(", " + f.chapter));
    if (f.page !== null && f.page !== undefined) {
      head.appendChild(document.createTextNode(", "));
      head.appendChild(extLink(f.page_href, "s. " + f.page));
    }
    const box = el("div", { class: "finding" + (verified ? "" : " is-unverified") }, head);
    if (f.description) box.appendChild(el("p", { text: f.description }));
    if (f.quote) box.appendChild(el("blockquote", { class: "quote", text: "«" + f.quote + "»" }));
    if (verified) box.appendChild(el("div", { class: "verified", text: "✓ Sitat kontrollert mot dokumentet" }));
    else {
      box.appendChild(
        el("div", { class: "unverified" }, [
          el("span", { class: "label", text: "UVERIFISERT" }),
          "Sitatet ble ikke funnet i dokumentteksten – kontroller mot kilden før bruk.",
        ])
      );
    }
    return box;
  }

  function buildHead(h) {
    const badges = el("div", { class: "badges" }, relBadge(h, true));
    const tag = newTag(h);
    if (tag) badges.appendChild(tag);
    badges.appendChild(statusBadge(h));

    const meta = el("div", { class: "d-meta" });
    const d = daysLeft(h);
    if (d === null) meta.appendChild(el("span", { text: "Ingen frist oppgitt" }));
    else {
      const u = urgency(h, d);
      meta.appendChild(el("span", null, ["Frist ", el("strong", { class: "num", text: formatDate(h.deadline) }), " ", el("span", { class: u, text: "(" + daysText(d) + ")" })]));
    }
    meta.appendChild(el("span", { text: h.department || "Ukjent departement" }));
    if (h.on_address_list === true) meta.appendChild(el("span", { text: "Datatilsynet på adresselisten" }));

    return el("header", { class: "d-head" }, [badges, el("h2", { class: "drawer-title", id: "drawer-title", text: h.title }), meta]);
  }

  function buildActions(h) {
    const actions = el("div", { class: "actions" });
    const src = safeHref(h.url);
    if (src) actions.appendChild(el("a", { class: "btn btn-primary", href: src, target: "_blank", rel: "noopener noreferrer", text: "Åpne på regjeringen.no" }));
    const row = el("div", { class: "actions-row" });
    const followBtn = el("button", { type: "button", class: "btn", id: "drawer-follow" });
    const setFollowLabel = function () {
      const on = state.follow.has(h.id);
      followBtn.textContent = on ? "Følger" : "Følg";
      followBtn.setAttribute("aria-pressed", on ? "true" : "false");
    };
    setFollowLabel();
    followBtn.addEventListener("click", function () {
      toggleFollow(h);
      setFollowLabel();
    });
    row.appendChild(followBtn);
    const copyBtn = el("button", { type: "button", class: "btn", text: "Kopier lenke" });
    copyBtn.addEventListener("click", function () {
      copyText(window.location.href).then(function (ok) {
        copyBtn.textContent = ok ? "Kopiert" : "Kunne ikke kopiere";
        announce(copyBtn.textContent);
        window.setTimeout(function () {
          copyBtn.textContent = "Kopier lenke";
        }, 2000);
      });
    });
    row.appendChild(copyBtn);
    actions.appendChild(row);
    const pdf = safeHref(h.pdf);
    if (pdf) actions.appendChild(el("a", { class: "btn", href: pdf, download: "", text: "Last ned PDF" }));
    return actions;
  }

  function buildMain(h) {
    const main = el("div", { class: "d-main" });
    if (h.relevance === "feilet") {
      main.appendChild(el("p", { class: "state-note", text: "Vurderingen feilet ved siste forsøk og prøves på nytt ved neste kjøring." }));
    } else if (relKey(h) === "ikke_vurdert") {
      main.appendChild(el("p", { class: "state-note", text: "Venter på vurdering." }));
    } else {
      main.appendChild(section("Begrunnelse for relevansnivå", el("p", { class: "lead", text: h.relevance_reason || "–" })));
      main.appendChild(section("Hva foreslås", el("p", { text: h.summary || "–" })));

      const guide = el("ul", { class: "guide" });
      for (const g of h.reading_guide) {
        if (!g.ranges || !g.ranges.length) continue;
        const li = el("li");
        append(li, [el("strong", { text: g.title }), ": s. ", rangeLinks(g.ranges)]);
        if (g.page_count) li.appendChild(document.createTextNode(" av " + g.page_count));
        guide.appendChild(li);
      }
      const reading = el("div", { class: "reading" });
      if (guide.childNodes.length) reading.appendChild(guide);
      else reading.appendChild(el("p", { text: "Ingen sider pekt ut." }));
      if (h.reading_share) reading.appendChild(el("p", { class: "share", text: h.reading_share }));
      main.appendChild(section("Sider å lese", reading));

      if (h.findings.length) {
        const wrap = el("div", { class: "findings" });
        for (const f of h.findings) wrap.appendChild(buildFinding(f));
        main.appendChild(section("Funn", wrap));
      } else {
        main.appendChild(section("Funn", el("p", { text: "Ingen konkrete funn oppgitt." })));
      }
    }

    if (h.changes.length) {
      const list = el("ul", { class: "changes" });
      for (const c of h.changes) list.appendChild(el("li", null, [el("span", { class: "cdate", text: formatDate(c.date) }), el("span", { text: c.text })]));
      main.appendChild(section("Endringer", list));
    }

    if (h.documents.length) {
      const list = el("ul");
      for (const d of h.documents) {
        const li = el("li", null, extLink(d.url, d.title || "Dokument"));
        if (d.read === false) li.appendChild(el("span", { class: "muted", text: " – ikke lest" + (d.note ? ": " + d.note : "") }));
        list.appendChild(li);
      }
      main.appendChild(section("Dokumenter", list));
    }

    if (h.notes.length) {
      const list = el("ul");
      for (const n of h.notes) list.appendChild(el("li", { text: n }));
      main.appendChild(section("Merknader", list));
    }

    main.appendChild(el("p", { class: "ai-note", text: "KI-generert sortering – kontroller mot kilden." }));
    return main;
  }

  function buildDrawer(h) {
    const body = $("drawer-body");
    clear(body);
    body.className = "drawer-body rel-" + relKey(h);
    body.appendChild(buildHead(h));
    body.appendChild(buildMain(h));
    body.appendChild(
      el("aside", { class: "d-side", "aria-label": "Handlinger og fakta" }, [
        el("div", { class: "side-card" }, buildActions(h)),
        el("div", { class: "side-card" }, buildFacts(h)),
      ])
    );
  }

  function updatePager(id) {
    const list = displayOrder();
    const idx = list.findIndex((x) => x.id === id);
    const pager = $("pager");
    if (idx === -1 || list.length < 2) {
      pager.hidden = true;
      state.pager = [];
      return;
    }
    pager.hidden = false;
    state.pager = list.map((x) => x.id);
    $("pager-pos").textContent = idx + 1 + " av " + list.length;
    $("pager-prev").disabled = idx === 0;
    $("pager-next").disabled = idx === list.length - 1;
  }

  function step(delta) {
    const idx = state.pager.indexOf(state.drawerId);
    const next = state.pager[idx + delta];
    if (idx === -1 || next === undefined) return;
    try {
      window.history.replaceState(null, "", "#h=" + next);
    } catch (e) {
      /* ignore */
    }
    state.drawerOpener = String(next);
    openDrawer(next, false);
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(
        () => true,
        () => legacyCopy(text)
      );
    }
    return Promise.resolve(legacyCopy(text));
  }

  function legacyCopy(text) {
    try {
      const ta = el("textarea", { class: "sr-only", "aria-hidden": "true", readonly: "" });
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (e) {
      return false;
    }
  }

  function openDrawer(id, byUser) {
    const h = state.byId.get(id);
    if (!h) {
      closeDrawerUi(false);
      try {
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
      } catch (e) {
        /* ignore */
      }
      return;
    }
    const wasOpen = state.drawerId !== null;
    if (!wasOpen) {
      const active = document.activeElement;
      state.drawerOpener = active && active.getAttribute && active.getAttribute("data-open") ? active.getAttribute("data-open") : String(id);
      state.drawerPushed = !!byUser;
    }
    closeMenu(false);
    updatePager(id);
    state.drawerId = id;
    state.read[id] = new Date().toISOString();
    storeSetJSON("read", state.read);
    buildDrawer(h);
    $("drawer").hidden = false;
    $("app").inert = true;
    document.body.classList.add("no-scroll");
    $("drawer").scrollTop = 0;
    $("drawer").focus();
    renderAll();
  }

  function closeDrawerUi(restoreFocus) {
    if (state.drawerId === null && $("drawer").hidden) return;
    const opener = state.drawerOpener;
    state.drawerId = null;
    state.drawerOpener = null;
    $("drawer").hidden = true;
    $("app").inert = false;
    document.body.classList.remove("no-scroll");
    if (restoreFocus) {
      const target = opener ? document.querySelector('[data-open="' + opener.replace(/[^0-9]/g, "") + '"]') : null;
      if (target && target.offsetParent !== null) target.focus();
      else $("view-title").focus();
    }
  }

  function requestCloseDrawer() {
    if (state.drawerPushed) {
      window.history.back();
      return;
    }
    try {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    } catch (e) {
      /* ignore */
    }
    closeDrawerUi(true);
  }

  function route(byUser) {
    const m = /^#h=(\d+)$/.exec(window.location.hash);
    if (m) openDrawer(Number(m[1]), byUser);
    else closeDrawerUi(true);
  }

  // -------------------------------------------------------------------- init

  function bindEvents() {
    for (const b of document.querySelectorAll("#nav .tab")) {
      b.addEventListener("click", function () {
        state.tab = b.getAttribute("data-tab");
        storeSet("tab", state.tab);
        renderAll();
      });
    }
    for (const b of document.querySelectorAll("#rel-facet .rel-opt")) {
      b.addEventListener("click", function () {
        const r = b.getAttribute("data-rel");
        if (state.filters.rel.has(r)) state.filters.rel.delete(r);
        else state.filters.rel.add(r);
        renderAll();
      });
    }
    for (const b of document.querySelectorAll("#seg-due button")) {
      b.addEventListener("click", function () {
        state.filters.due = b.getAttribute("data-due") === "1";
        renderAll();
      });
    }
    for (const b of document.querySelectorAll("#seg-addr button")) {
      b.addEventListener("click", function () {
        state.filters.addr = b.getAttribute("data-addr");
        renderAll();
      });
    }
    for (const b of document.querySelectorAll("#seg-sort button")) {
      b.addEventListener("click", function () {
        state.sort = b.getAttribute("data-sort");
        storeSet("sort", state.sort);
        renderAll();
      });
    }
    $("period-select").addEventListener("change", function (ev) {
      state.period = ev.target.value;
      storeSet("period", state.period);
      renderAll();
    });
    $("search").addEventListener("input", function (ev) {
      state.search = ev.target.value.trim().toLocaleLowerCase("nb");
      renderAll();
    });
    $("f-sector").addEventListener("change", function (ev) {
      state.filters.sector = ev.target.value;
      renderAll();
    });
    $("f-dept").addEventListener("change", function (ev) {
      state.filters.dept = ev.target.value;
      renderAll();
    });
    $("filter-reset").addEventListener("click", function () {
      resetFilters();
      renderAll();
      $("search").focus();
    });
    for (const b of document.querySelectorAll("#seg-theme button")) {
      b.addEventListener("click", function () {
        setTheme(b.getAttribute("data-theme-choice"));
      });
    }
    $("filter-toggle").addEventListener("click", function () {
      state.facetsOpen = !state.facetsOpen;
      syncControls();
    });

    $("row-menu").addEventListener("keydown", onMenuKey);
    $("row-menu").addEventListener("click", function (ev) {
      ev.stopPropagation();
    });
    document.addEventListener("click", function () {
      closeMenu(false);
    });
    window.addEventListener("resize", function () {
      closeMenu(false);
    });
    window.addEventListener(
      "scroll",
      function () {
        closeMenu(false);
      },
      { passive: true }
    );

    document.addEventListener("keydown", function (ev) {
      if (ev.key !== "Escape") return;
      if (state.menu.open) closeMenu(true);
      else if (state.drawerId !== null) requestCloseDrawer();
    });

    $("drawer-close").addEventListener("click", requestCloseDrawer);
    $("pager-prev").addEventListener("click", function () {
      step(-1);
    });
    $("pager-next").addEventListener("click", function () {
      step(1);
    });
    window.addEventListener("hashchange", function () {
      route(true);
    });

    const about = $("about-dialog");
    const openAbout = function () {
      if (typeof about.showModal === "function") about.showModal();
      else about.setAttribute("open", "");
    };
    $("about-btn").addEventListener("click", openAbout);
    $("about-btn-2").addEventListener("click", openAbout);
    $("about-close").addEventListener("click", function () {
      about.close();
    });
    about.addEventListener("click", function (ev) {
      if (ev.target === about) about.close();
    });
  }

  /** "system" follows prefers-color-scheme; "light"/"dark" override it (theme.js applies it on load). */
  function setTheme(theme) {
    state.theme = THEMES.indexOf(theme) !== -1 ? theme : "system";
    const root = document.documentElement;
    if (state.theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", state.theme);
    storeSet("theme", state.theme);
    syncControls();
  }

  function restorePreferences() {
    const theme = storeGet("theme");
    if (THEMES.indexOf(theme) !== -1) state.theme = theme;
    const period = storeGet("period");
    if (PERIODS.indexOf(period) !== -1) state.period = period;
    const tab = storeGet("tab");
    if (Object.prototype.hasOwnProperty.call(TAB_TITLES, tab)) state.tab = tab;
    const sort = storeGet("sort");
    if (SORTS.indexOf(sort) !== -1) state.sort = sort;
    const follow = storeGetJSON("follow", []);
    state.follow = new Set(Array.isArray(follow) ? follow.filter((x) => typeof x === "number") : []);
    const read = storeGetJSON("read", {});
    state.read = read && typeof read === "object" && !Array.isArray(read) ? read : {};
  }

  function showError() {
    const box = $("list");
    clear(box);
    box.appendChild(el("div", { class: "error", role: "alert", text: "Kunne ikke laste høringene (data.json). Last siden på nytt, eller prøv igjen senere." }));
  }

  function init() {
    bindEvents();
    restorePreferences();
    syncControls();
    fetch("data.json", { cache: "no-cache" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        setupVisit(data.generated_at);
        prepare(data);
        setupFilterOptions();
        renderAll();
        route(false);
      })
      .catch(function () {
        showError();
      });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
