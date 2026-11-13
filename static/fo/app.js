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

  /* ---- a submitted button shows it is working (not disabled: its name and value must still be sent) ---- */
  d.addEventListener("submit", function (e) {
    var b = e.submitter;
    if (b && b.classList && b.classList.contains("btn")) setTimeout(function () { b.classList.add("is-loading"); }, 0);
  });
  window.addEventListener("pageshow", function () { d.querySelectorAll(".btn.is-loading").forEach(function (b) { b.classList.remove("is-loading"); }); });
})();
