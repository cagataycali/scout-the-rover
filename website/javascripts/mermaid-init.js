/* neon docs — theme-aware mermaid.
   Paper(warm) and Sky(light-blue) are both LIGHT, so one light palette
   works for both, with accent stroke shifting per scheme.
   Robust against Material instant-nav (re-render on document$). */
(function () {
  function vars() {
    // pull live CSS custom props so mermaid matches the active scheme
    var cs = getComputedStyle(document.body);
    var g = function (n, d) { return (cs.getPropertyValue(n) || d).trim() || d; };
    var accent  = g("--accent",  "#cc5a3a");
    var surface = g("--surface", "#ffffff");
    var s2      = g("--surface-2", "#f4f2ee");
    var sunken  = g("--sunken",  "#f7f5f2");
    var text    = g("--text",    "#1c1a17");
    var muted   = g("--muted",   "#8a847a");
    var border  = g("--border",  "#e8e4dd");
    var bg      = g("--bg",      "#faf9f7");
    var subtle  = g("--accent-sub", "#f4e4dc");
    return {
      background: bg,
      primaryColor: subtle, primaryBorderColor: accent, primaryTextColor: text,
      secondaryColor: s2, secondaryBorderColor: muted, secondaryTextColor: text,
      tertiaryColor: sunken, tertiaryBorderColor: border,
      lineColor: muted, textColor: text,
      mainBkg: subtle, nodeBorder: accent,
      clusterBkg: sunken, clusterBorder: border, edgeLabelBackground: bg,
      fontSize: "14px",
    };
  }

  function renderAll() {
    if (typeof mermaid === "undefined") { return setTimeout(renderAll, 50); }
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "loose",
      theme: "base",
      fontFamily: "'Inter', ui-sans-serif, system-ui, sans-serif",
      themeVariables: vars(),
    });
    var nodes = document.querySelectorAll(".mermaid");
    nodes.forEach(function (el) {
      // restore original source so we can re-render on theme toggle
      if (!el.hasAttribute("data-mmd-src")) {
        el.setAttribute("data-mmd-src", el.textContent);
      } else {
        el.removeAttribute("data-processed");
        el.innerHTML = el.getAttribute("data-mmd-src");
      }
    });
    try { mermaid.run({ nodes: nodes }); } catch (e) { try { mermaid.init(undefined, nodes); } catch (e2) {} }
  }

  // initial render
  if (document.readyState !== "loading") renderAll();
  else document.addEventListener("DOMContentLoaded", renderAll);

  // re-render on Material palette toggle
  var obs = new MutationObserver(function (m) {
    for (var i = 0; i < m.length; i++) {
      if (m[i].attributeName === "data-md-color-scheme") { renderAll(); break; }
    }
  });
  if (document.body) obs.observe(document.body, { attributes: true });

  // re-render after Material instant navigation
  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(function () { setTimeout(renderAll, 30); });
  }
})();
