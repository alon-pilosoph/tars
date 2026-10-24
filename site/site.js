"use strict";
/* The pages with Basic and Detailed (How it works, Features). The detail level: remembered on this device. */
{
  const root = document.documentElement;
  const lb = document.querySelectorAll("[data-level-pick]");
  const show = () => {
    lb.forEach(b => b.setAttribute("aria-pressed", String(b.dataset.levelPick === root.dataset.level)));
  };
  lb.forEach(b =>
    b.addEventListener("click", () => {
      root.dataset.level = b.dataset.levelPick;
      try {
        localStorage.setItem("tars-about-level", b.dataset.levelPick);
      } catch (e) {}
      show();
    }),
  );
  show();
}

/* App panels: filled at build time inside a shadow root. Until then, say what goes there. */
document.querySelectorAll("[data-app-panel]").forEach(f => {
  if (!f.shadowRoot && !f.childElementCount) {
    f.classList.add("placeholder");
    f.dataset.placeholder = `The ${f.dataset.appPanel[0].toUpperCase() + f.dataset.appPanel.slice(1)} page goes here at build time`;
  }
});
