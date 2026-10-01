/*
 * motion.js (from strands-labs/robots docs/assets/motion.js, via the neon-the-g1 docs): the docs site's small motions. Each one carries a state or an arrival:
 *   - the content column fades in after an instant-navigation swap (never on the first paint),
 *   - the "On this page" bar travels to the active entry,
 *   - the theme toggle cross-fades through the View Transitions API where the browser has it,
 *   - and, not a motion, the instant-navigation progress bar gets an accessible name.
 * Everything here is skipped under prefers-reduced-motion, and every hook re-runs on Material's
 * document$ so it survives instant navigation. No fetches, no layout reads in a loop, no timers
 * that outlive their element.
 */
(() => {
  const reduced = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  let paints = 0;

  function enter() {
    paints += 1;
    const inner = document.querySelector(".md-content__inner");
    if (!inner || paints === 1 || reduced()) return;
    inner.classList.remove("sr-enter");
    void inner.offsetWidth;
    inner.classList.add("sr-enter");
  }

  function tocBar() {
    const nav = document.querySelector(".md-sidebar--secondary .md-nav--secondary");
    if (!nav || nav.dataset.srBar) return;
    nav.dataset.srBar = "1";
    const place = () => {
      const active = nav.querySelector(".md-nav__link--active");
      if (!active) { nav.style.setProperty("--sr-toc-h", "0px"); return; }
      const navBox = nav.getBoundingClientRect(), box = active.getBoundingClientRect();
      nav.style.setProperty("--sr-toc-y", `${box.top - navBox.top}px`);
      nav.style.setProperty("--sr-toc-h", `${box.height}px`);
    };
    new MutationObserver(place).observe(nav, { attributes: true, subtree: true, attributeFilter: ["class"] });
    place();
  }

  function themeFade() {
    if (document.documentElement.dataset.srTheme) return;
    document.documentElement.dataset.srTheme = "1";
    document.addEventListener("click", (e) => {
      const label = e.target.closest?.(".md-header__button[for^='__palette']");
      if (!label || reduced() || typeof document.startViewTransition !== "function") return;
      const input = document.getElementById(label.htmlFor);
      if (!input) return;
      e.preventDefault();
      document.startViewTransition(() => input.click());
    }, true);
  }

  function names() {
    // Material's instant-navigation progress bar is a nameless progressbar (axe aria-progressbar-name).
    for (const bar of document.querySelectorAll(".md-progress:not([aria-label])")) bar.setAttribute("aria-label", "Loading");
  }

  function run() { enter(); tocBar(); themeFade(); names(); }
  if (window.document$?.subscribe) window.document$.subscribe(run);
  else if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
  else run();
})();
