import "./fonts.css";
import "./styles.css";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { DEMO, LINK } from "./params";
import { refresh, scrollToEl } from "./store";

if (LINK.theme) document.documentElement.dataset.theme = LINK.theme;
const root = document.getElementById("root");
if (root) createRoot(root).render(<App />);

async function scrollToLinked() {
  if (LINK.conv == null && LINK.item == null) return;
  await document.fonts.ready; // until the fonts load, the text above isn't its final height
  if (LINK.conv != null) scrollToEl(`.conv[data-id="${LINK.conv}"]`);
  if (LINK.item != null) scrollToEl(`.item[data-id="${LINK.item}"]`);
}

(async function start() {
  const demo = DEMO ? await import("./linkStates") : null;
  if (demo && !demo.openEarlyState()) return;
  await refresh();
  await scrollToLinked();
  demo?.openLinkState();
})();
