/* Mermaid fences follow the site (ported from strands-labs/robots docs/assets/mermaid.js): mono labels, ink on the page
   surface, the green and the warn amber from the live tokens, 1px, 8px. Rendered on document$ (navigation.instant) and
   again when data-md-color-scheme toggles. No animation. */
(function () {
  var counter = 0;

  function token(name) {
    return getComputedStyle(document.body).getPropertyValue(name).trim();
  }

  function themeVariables() {
    var bg = token("--md-default-bg-color") || "#ffffff";
    var fg = token("--sr-fg") || "#000000";
    var muted = token("--sr-muted") || "#767373";
    var chip = token("--sr-chip-bg") || "#f4f4f4";
    var dark = document.body.getAttribute("data-md-color-scheme") === "slate";
    return {
      fontFamily: token("--sr-mono") || "JetBrains Mono, monospace",
      fontSize: "13px",
      background: bg, mainBkg: bg,
      primaryColor: bg, primaryTextColor: fg, primaryBorderColor: fg,
      secondaryColor: bg, secondaryTextColor: fg, secondaryBorderColor: fg,
      tertiaryColor: bg, tertiaryTextColor: fg, tertiaryBorderColor: muted,
      lineColor: fg, textColor: fg, nodeBorder: fg, nodeTextColor: fg,
      clusterBkg: bg, clusterBorder: muted, titleColor: fg, edgeLabelBackground: bg,
      noteBkgColor: chip, noteTextColor: fg, noteBorderColor: muted,
      actorBkg: bg, actorBorder: fg, actorTextColor: fg, actorLineColor: muted,
      signalColor: fg, signalTextColor: fg,
      labelBoxBkgColor: bg, labelBoxBorderColor: fg, labelTextColor: fg, loopTextColor: fg,
      activationBkgColor: chip, activationBorderColor: fg,
      sequenceNumberColor: dark ? "#000000" : "#ffffff",
      darkMode: dark
    };
  }

  function accentClass() {
    /* The one colour a fence may ask for: a node tagged :::accent. Everything else is the theme. */
    var accent = token("--sr-accent") || "#007a3d";
    var soft = /^#[0-9a-f]{6}$/i.test(accent) ? accent + "1f" : "#007a3d1f"; /* 8-digit hex: mermaid's classDef parser rejects rgba(...) */
    return "\nclassDef accent fill:" + soft + ",stroke:" + accent + ",color:" + accent + ",stroke-width:1.5px\n";
  }

  function backLabels(holder) {
    /* State and flow edge labels come with an unsized rect behind the text; size it so the label sits on the page, not on its wire. */
    holder.querySelectorAll(".edgeLabel .label").forEach(function (g) {
      var text = g.querySelector("text"), rect = g.querySelector("rect");
      if (!text || !rect) return;
      var box = text.getBBox();
      if (!box.width) return;
      rect.setAttribute("x", box.x - 4); rect.setAttribute("y", box.y - 1);
      rect.setAttribute("width", box.width + 8); rect.setAttribute("height", box.height + 2);
      rect.style.opacity = "1";
    });
    holder.querySelectorAll("g.edgeLabels").forEach(function (g) { g.parentNode.appendChild(g); }); /* labels above their wires */
  }

  function holders() {
    /* First pass: each fence becomes a holder that keeps its source so a palette toggle can re-render it. */
    document.querySelectorAll("pre.sr-diagram").forEach(function (pre) {
      var code = pre.querySelector("code");
      var holder = document.createElement("div");
      holder.className = "sr-mermaid";
      holder.setAttribute("data-src", (code || pre).textContent);
      pre.replaceWith(holder);
    });
    return document.querySelectorAll(".sr-mermaid");
  }

  function render() {
    if (typeof mermaid === "undefined") return;
    var found = holders();
    if (!found.length) return;
    mermaid.initialize({ startOnLoad: false, theme: "base", securityLevel: "strict", themeVariables: themeVariables(),
      flowchart: { curve: "linear", htmlLabels: false }, sequence: { mirrorActors: false, useMaxWidth: true },
      state: { useMaxWidth: true } });
    found.forEach(function (holder) {
      var id = "sr-d" + (counter++);
      var src = holder.getAttribute("data-src");
      /* Fences spell the paper tokens literally in classDef lines; swap them for the live scheme's so dark mode gets its brighter green and amber. */
      var accent = token("--sr-accent") || "#007a3d", warn = token("--sr-warn") || "#946e00", muted = token("--sr-muted") || "#767373";
      var themed = src.replace(/#007a3d/gi, accent).replace(/#946e00/gi, warn).replace(/#666464/gi, muted);
      var withAccent = /^\s*(flowchart|graph|stateDiagram(-v2)?)\b/.test(themed) ? themed + accentClass() : themed;
      mermaid.render(id, withAccent).then(function (out) {
        holder.innerHTML = out.svg;
        backLabels(holder);
        holder.setAttribute("data-rendered", "true");
        if (out.bindFunctions) out.bindFunctions(holder);
      }).catch(function (err) {
        holder.setAttribute("data-rendered", "error");
        holder.textContent = "diagram failed to render: " + err.message;
      });
    });
  }

  if (typeof document$ !== "undefined") { document$.subscribe(render); } else { document.addEventListener("DOMContentLoaded", render); }

  new MutationObserver(function (list) {
    if (list.some(function (m) { return m.attributeName === "data-md-color-scheme"; })) render();
  }).observe(document.body, { attributes: true });
})();
