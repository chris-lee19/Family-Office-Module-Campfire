/* Family Office: page behaviour. Search (Ctrl/Cmd+K or "/"), menus, the mobile drawer, tabs, sortable and
   filterable tables. No dependencies; everything degrades to plain links and full pages without it. */
(function () {
  "use strict";
  var d = document;
  function store(key, value) {
    try { if (value === undefined) return sessionStorage.getItem(key); sessionStorage.setItem(key, value); } catch (_) { return null; }
  }

  /* ---- menus and the mobile drawer ---- */
  d.addEventListener("click", function (e) {
    var toggle = e.target.closest("[data-menu-toggle]");
    var mine = toggle ? toggle.closest("[data-menu]") : null;
    d.querySelectorAll("[data-menu].open").forEach(function (m) { if (m !== mine) m.classList.remove("open"); });
    if (mine) mine.classList.toggle("open");
    if (e.target.closest("[data-drawer-toggle]")) d.body.classList.toggle("drawer-open");
  });

  /* ---- page links inside Campfire: keep the current one in view. Not scrollIntoView: inside a frame that can
     also scroll Campfire's own page. ---- */
  var strip = d.querySelector("[data-frame-nav]");
  if (strip) {
    var here = strip.querySelector(".active");
    if (here) strip.scrollLeft = Math.max(0, here.offsetLeft - strip.offsetLeft - 24);
  }

  /* ---- search ---- */
  var pal = d.getElementById("fo-palette");
  if (pal) {
    var input = pal.querySelector("input"), list = pal.querySelector("[data-results]"), items = [], shown = [], active = 0;
    try { items = JSON.parse(d.getElementById("fo-index").textContent) || []; } catch (_) { items = []; }
    var token = pal.getAttribute("data-token") || "";
    var href = function (u) {
      if (!token) return u;
      var i = u.indexOf("#"), base = i < 0 ? u : u.slice(0, i), frag = i < 0 ? "" : u.slice(i);
      return base + (base.indexOf("?") < 0 ? "?" : "&") + "fo_t=" + encodeURIComponent(token) + frag;
    };
    var score = function (item, words) {
      var title = item.t.toLowerCase(), hay = title + " " + (item.s || "").toLowerCase(), total = 0;
      for (var i = 0; i < words.length; i++) {
        var w = words[i], at = title.indexOf(w);
        if (at === 0) total += 3; else if (at > 0 && /\W/.test(title[at - 1])) total += 2; else if (at > 0) total += 1.5;
        else if (hay.indexOf(w) >= 0) total += 1; else return 0;
      }
      return total;
    };
    var render = function () {
      var q = input.value.trim().toLowerCase(), words = q ? q.split(/\s+/) : [];
      if (!words.length) shown = items.filter(function (i) { return i.g === "Go to" || i.g === "Do"; });
      else {
        shown = items.map(function (i) { return [score(i, words), i]; }).filter(function (p) { return p[0] > 0; })
          .sort(function (a, b) { return b[0] - a[0]; }).slice(0, 14).map(function (p) { return p[1]; });
      }
      active = 0;
      list.textContent = "";
      if (!shown.length) {
        var none = d.createElement("div"); none.className = "palette-empty"; none.textContent = "Nothing matches “" + input.value.trim() + "”.";
        list.appendChild(none); return;
      }
      var group = null;
      shown.forEach(function (item, n) {
        if (!words.length && item.g !== group) {
          group = item.g; var h = d.createElement("div"); h.className = "palette-group"; h.textContent = group; list.appendChild(h);
        }
        var a = d.createElement("a"); a.href = href(item.u); a.className = "palette-item" + (n === 0 ? " active" : ""); a.setAttribute("data-n", n);
        var t = d.createElement("strong"); t.textContent = item.t; a.appendChild(t);
        if (item.s) { var s = d.createElement("span"); s.textContent = item.s; a.appendChild(s); }
        list.appendChild(a);
      });
    };
    var mark = function (n) {
      var rows = list.querySelectorAll(".palette-item");
      if (!rows.length) return;
      active = (n + rows.length) % rows.length;
      rows.forEach(function (r, i) { r.classList.toggle("active", i === active); });
      rows[active].scrollIntoView({ block: "nearest" });
    };
    var open = function () {
      pal.hidden = false; input.value = ""; render(); input.focus();
      if (d.body.classList.contains("framed")) pal.scrollIntoView({ block: "start" });
    };
    var close = function () { pal.hidden = true; };
    d.querySelectorAll("[data-palette-open]").forEach(function (b) { b.addEventListener("click", open); });
    pal.addEventListener("click", function (e) { if (e.target === pal) close(); });
    list.addEventListener("mousemove", function (e) { var r = e.target.closest(".palette-item"); if (r) mark(+r.getAttribute("data-n")); });
    input.addEventListener("input", render);
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); mark(active + 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); mark(active - 1); }
      else if (e.key === "Enter") { var r = list.querySelectorAll(".palette-item")[active]; if (r) { e.preventDefault(); location.href = r.href; } }
    });
    d.addEventListener("keydown", function (e) {
      var typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target.tagName || "")) || e.target.isContentEditable;
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) { e.preventDefault(); pal.hidden ? open() : close(); }
      else if (e.key === "/" && !typing && pal.hidden) { e.preventDefault(); open(); }
      else if (e.key === "Escape" && !pal.hidden) close();
    });
    if (/Mac|iPhone|iPad/.test(navigator.platform || "")) d.querySelectorAll(".search-btn kbd").forEach(function (k) { k.textContent = "⌘K"; });
  }

  /* ---- tabs: [data-tabs] with [data-tab=name] buttons and [data-panel=name] sections ---- */
  d.querySelectorAll("[data-tabs]").forEach(function (root) {
    var key = "fo-tab:" + location.pathname + ":" + (root.getAttribute("data-tabs") || "");
    var tabs = [].filter.call(root.querySelectorAll("[data-tab]"), function (t) { return t.closest("[data-tabs]") === root; });
    var panels = [].filter.call(root.querySelectorAll("[data-panel]"), function (p) { return p.closest("[data-tabs]") === root; });
    var show = function (name, remember) {
      if (!tabs.some(function (t) { return t.getAttribute("data-tab") === name; })) name = tabs[0].getAttribute("data-tab");
      tabs.forEach(function (t) { var on = t.getAttribute("data-tab") === name; t.classList.toggle("active", on); t.setAttribute("aria-selected", on ? "true" : "false"); });
      panels.forEach(function (p) { p.hidden = p.getAttribute("data-panel") !== name; });
      if (remember) store(key, name);
    };
    if (!tabs.length) return;
    d.querySelectorAll("[data-tab-go]").forEach(function (a) {
      a.addEventListener("click", function (e) {
        var name = a.getAttribute("data-tab-go");
        if (tabs.some(function (t) { return t.getAttribute("data-tab") === name; })) { e.preventDefault(); show(name, true); root.scrollIntoView({ block: "start", behavior: "smooth" }); }
      });
    });
    tabs.forEach(function (t) { t.setAttribute("role", "tab"); t.addEventListener("click", function () { show(t.getAttribute("data-tab"), true); }); });
    var wanted = (location.hash || "").slice(1);
    show(tabs.some(function (t) { return t.getAttribute("data-tab") === wanted; }) ? wanted : (store(key) || ""), false);
  });

  /* ---- tables: click a heading to sort; an input[data-filter="#table"] filters rows ---- */
  var cellValue = function (td) {
    var raw = (td.getAttribute("data-v") || td.textContent || "").trim();
    var text = raw.split("\n")[0].trim();
    if (!text || text === "—") return [0, -Infinity, ""];
    var num = text.replace(/[$,%x\s]/g, "").replace(/^\((.*)\)$/, "-$1");
    if (/^-?\d+(\.\d+)?$/.test(num)) return [0, parseFloat(num), ""];
    return [1, 0, text.toLowerCase()];
  };
  d.querySelectorAll("table[data-sort]").forEach(function (table) {
    var heads = table.tHead ? table.tHead.rows[0].cells : [];
    [].forEach.call(heads, function (th, col) {
      if (!th.textContent.trim()) return;
      th.classList.add("sortable"); th.tabIndex = 0;
      var sort = function () {
        var dir = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
        [].forEach.call(heads, function (h) { h.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", dir);
        var body = table.tBodies[0], rows = [].slice.call(body.rows);
        rows.sort(function (a, b) {
          var x = cellValue(a.cells[col] || a.cells[0]), y = cellValue(b.cells[col] || b.cells[0]);
          var c = x[0] - y[0] || x[1] - y[1] || (x[2] < y[2] ? -1 : x[2] > y[2] ? 1 : 0);
          return dir === "ascending" ? c : -c;
        });
        rows.forEach(function (r) { body.appendChild(r); });
      };
      th.addEventListener("click", sort);
      th.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); } });
    });
  });
  d.querySelectorAll("input[data-filter]").forEach(function (box) {
    var table = d.querySelector(box.getAttribute("data-filter"));
    var count = box.getAttribute("data-count") ? d.querySelector(box.getAttribute("data-count")) : null;
    if (!table || !table.tBodies[0]) return;
    box.addEventListener("input", function () {
      var words = box.value.trim().toLowerCase().split(/\s+/).filter(Boolean), n = 0, rows = table.tBodies[0].rows;
      [].forEach.call(rows, function (r) {
        var text = r.textContent.toLowerCase(), hit = words.every(function (w) { return text.indexOf(w) >= 0; });
        r.hidden = !hit; if (hit) n++;
      });
      if (count) count.textContent = words.length ? n + " of " + rows.length : rows.length + "";
    });
  });

  /* ---- filter a grid of tiles; show the chosen file's name in a drop zone ---- */
  d.querySelectorAll("input[data-filter-cards]").forEach(function (box) {
    var cards = d.querySelectorAll(box.getAttribute("data-filter-cards"));
    box.addEventListener("input", function () {
      var q = box.value.trim().toLowerCase();
      cards.forEach(function (c) { c.hidden = q !== "" && c.textContent.toLowerCase().indexOf(q) < 0; });
    });
  });
  d.querySelectorAll("[data-dropzone]").forEach(function (zone) {
    var file = zone.querySelector("input[type=file]"), name = zone.querySelector("[data-dropzone-name]");
    if (!file || !name) return;
    var initial = name.textContent;
    file.addEventListener("change", function () {
      name.textContent = file.files && file.files.length ? file.files[0].name : initial;
      zone.classList.toggle("has-file", !!(file.files && file.files.length));
    });
    ["dragenter", "dragover"].forEach(function (ev) { zone.addEventListener(ev, function () { zone.classList.add("over"); }); });
    ["dragleave", "drop"].forEach(function (ev) { zone.addEventListener(ev, function () { zone.classList.remove("over"); }); });
  });

  /* ---- a link can open a folded section (and a #hash that names one opens it on load) ---- */
  var unfold = function (sel) { var el = sel && sel.length > 1 ? d.querySelector(sel) : null; if (el && el.tagName === "DETAILS") { el.open = true; return el; } return null; };
  d.querySelectorAll("[data-open]").forEach(function (a) {
    a.addEventListener("click", function () { var el = unfold(a.getAttribute("data-open")); if (el) { var f = el.querySelector("select, input:not([type=hidden])"); if (f) f.focus(); } });
  });
  unfold(location.hash);

  /* ---- a journal entry form shows its totals and whether it balances ---- */
  d.querySelectorAll("table[data-balance]").forEach(function (table) {
    var num = function (el) { var v = parseFloat((el.value || "").replace(/[$,\s]/g, "")); return isNaN(v) ? 0 : v; };
    var fmt = function (v) { return v ? v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "–"; };
    var update = function () {
      var dr = 0, cr = 0;
      table.querySelectorAll("[data-debit]").forEach(function (i) { dr += num(i); });
      table.querySelectorAll("[data-credit]").forEach(function (i) { cr += num(i); });
      table.querySelector("[data-total-debit]").textContent = fmt(dr);
      table.querySelector("[data-total-credit]").textContent = fmt(cr);
      var note = table.querySelector("[data-balance-note]"), diff = Math.round((dr - cr) * 100) / 100;
      note.textContent = !dr && !cr ? "Debits and credits must be equal." : diff === 0 ? "Balanced." : "Out of balance by " + fmt(Math.abs(diff)) + ".";
      note.classList.toggle("err", diff !== 0);
    };
    table.addEventListener("input", update); update();
  });

  /* ---- a bank reconciliation shows what the ticked lines add up to, and what is still unexplained ---- */
  d.querySelectorAll("form[data-recon]").forEach(function (form) {
    var target = Math.round(parseFloat(form.getAttribute("data-recon")) * 100), boxes = form.querySelectorAll("input[data-amount]");
    var fmt = function (cents) { var v = Math.abs(cents) / 100, s = v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); return cents < 0 ? "(" + s + ")" : s; };
    var set = function (sel, text) { d.querySelectorAll(sel).forEach(function (el) { el.textContent = text; }); };
    var cents = function (name) { return Math.round(parseFloat(form.getAttribute(name)) * 100); };
    var update = function () {
      var sum = 0, count = 0, transit = 0, outstanding = 0;
      boxes.forEach(function (b) {
        var amount = Math.round(parseFloat(b.getAttribute("data-amount")) * 100);
        if (b.checked) { sum += amount; count++; } else if (amount > 0) transit += amount; else outstanding -= amount;
        b.closest("tr").classList.toggle("is-cleared", b.checked);
      });
      var diff = target - sum, adjusted = cents("data-ending") + transit - outstanding, proof = adjusted - cents("data-book");
      set("[data-recon-cleared]", fmt(sum)); set("[data-recon-count]", String(count)); set("[data-recon-diff]", diff ? fmt(diff) : "0.00");
      set("[data-recon-transit]", transit ? fmt(transit) : "–"); set("[data-recon-outstanding]", outstanding ? "(" + fmt(outstanding) + ")" : "–");
      set("[data-recon-adjusted]", fmt(adjusted)); if (!isNaN(proof)) set("[data-recon-proof]", proof ? fmt(proof) : "0.00");
      d.querySelectorAll("[data-recon-state]").forEach(function (el) { el.classList.toggle("is-zero", diff === 0); });
      d.querySelectorAll("[data-recon-proof-state]").forEach(function (el) { el.classList.toggle("is-zero", proof === 0); });
    };
    form.addEventListener("change", update);
    form.querySelectorAll("[data-recon-tick]").forEach(function (button) {
      button.addEventListener("click", function () {
        var which = button.getAttribute("data-recon-tick");
        boxes.forEach(function (b) { if (b.closest("tr").hidden) return; b.checked = which === "all" || (which === "feed" && (b.checked || b.hasAttribute("data-feed"))); });
        update();
      });
    });
    update();
  });

  /* ---- a filter bar applies itself when a choice changes ---- */
  d.querySelectorAll("form[data-autosubmit]").forEach(function (form) {
    form.addEventListener("change", function (e) { if (e.target.tagName === "SELECT") form.submit(); });
  });

  /* ---- a submitted button shows it is working (not disabled: its name and value must still be sent) ---- */
  d.addEventListener("submit", function (e) {
    var b = e.submitter;
    if (b && b.classList && b.classList.contains("btn")) setTimeout(function () { b.classList.add("is-loading"); }, 0);
  });
  window.addEventListener("pageshow", function () { d.querySelectorAll(".btn.is-loading").forEach(function (b) { b.classList.remove("is-loading"); }); });
})();
