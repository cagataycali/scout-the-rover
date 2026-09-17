/* scout docs — mount <model-viewer> programmatically.
   Declaring <model-viewer> in the markdown races the custom-element upgrade against Material's
   bundle + mermaid on the assembled page (src stays undefined, nothing loads). Creating the element
   AFTER customElements.whenDefined() resolves is deterministic, and re-running on Material's
   document$ keeps it working across instant navigation. */
(function () {
  function mount() {
    var slots = document.querySelectorAll("[data-scout-3d]");
    if (!slots.length) return;
    customElements.whenDefined("model-viewer").then(function () {
      slots.forEach(function (slot) {
        if (slot.querySelector("model-viewer")) return;
        var mv = document.createElement("model-viewer");
        var base = slot.getAttribute("data-scout-3d");
        var attrs = {
          "class": "scout-3d", src: base, alt: slot.getAttribute("data-alt") || "a stylised low-poly scout rover",
          "camera-controls": "", "auto-rotate": "", "auto-rotate-delay": "0",
          "rotation-per-second": slot.getAttribute("data-rps") || "20deg",
          "shadow-intensity": "1", exposure: "1.1",
          "camera-orbit": slot.getAttribute("data-orbit") || "35deg 72deg 0.75m",
          "min-camera-orbit": "auto auto 0.4m", "max-camera-orbit": "auto auto 1.6m",
          "interaction-prompt": "none", loading: "eager"
        };
        Object.keys(attrs).forEach(function (k) { mv.setAttribute(k, attrs[k]); });
        slot.replaceChildren(mv);
      });
    });
  }
  if (typeof document$ !== "undefined" && document$.subscribe) { document$.subscribe(mount); }
  else if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", mount); }
  else { mount(); }
})();
