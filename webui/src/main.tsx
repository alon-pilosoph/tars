import "./fonts.css";
import "./styles.css";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { openEarlyState, openLinkState } from "./linkStates";
import { LINK } from "./params";
import { refresh } from "./store";

if (LINK.theme) document.documentElement.dataset.theme = LINK.theme;
createRoot(document.getElementById("root")!).render(<App />);

(async function start() {
  if (!openEarlyState()) return;
  await refresh();
  await openLinkState();
})();
