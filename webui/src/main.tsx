import "./fonts.css";
import "./styles.css";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { DEMO, LINK, PARAMS } from "./params";
import { RecordPage } from "./record/Record";
import { refresh, scrollToEl, settled } from "./store";

// A recording session's link (?session=<token>) opens that page alone: it needs nothing else the web UI loads.
const RECORDING = PARAMS.has("session");

const root = document.getElementById("root");
if (root) createRoot(root).render(RECORDING ? <RecordPage /> : <App />);

async function scrollToLinked() {
  if (LINK.conv == null && LINK.item == null) return;
  await document.fonts.ready; // until the fonts load, the text above isn't its final height
  if (LINK.conv != null) scrollToEl(`.conv[data-id="${LINK.conv}"]`);
  if (LINK.item != null) scrollToEl(`.item[data-id="${LINK.item}"]`);
}

(async function start() {
  if (RECORDING) return; // the page loads its own session, and marks itself ready

  const demo = DEMO ? await import("./linkStates") : null;
  try {
    if (demo && !demo.openEarlyState()) return;
    await refresh();
    await scrollToLinked();
    await demo?.openLinkState();
  } finally {
    // For the screenshot tests: the page is loaded, scrolled to what the link points at, and any linked state open;
    // and a way to wait for what a click set off.
    if (DEMO) Object.assign(document.documentElement.dataset, { ready: "true" });
    if (DEMO) Object.assign(window, { tarsSettled: settled });
  }
})();
