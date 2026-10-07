import { test, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { ORIGIN, REPO, fontsReady, serveFromDisk } from "./serve";

const SOURCE = path.join(REPO, "site/index.html");
const OUT = path.join(REPO, "site/dist");
const WIDTH = 480;

const PANELS: Record<string, { label: string; keep: string[] }> = {
  review: {
    label: "The Review page: wakes TARS wasn't sure about, each with hey TARS and Not it, and TARS's guess shaded",
    keep: [".page-head", "main > .wakes > .wake:nth-child(2)", "main > .wakes > .wake:nth-child(5)"],
  },
  voices: {
    label: "The Voices page: the people TARS has heard, and a voice still to name",
    keep: [".page-head", ".head-hint + .sec-head", ".head-hint + .sec-head + .voices > .voice:nth-child(-n+3)"],
  },
  models: {
    label: "The Models page: the pair in use, and the history, where Use this switches back",
    keep: [".page-head", ".page-head + .sec-head", "dl.kv", "main > .sec-head:has(+ .history)", ".history"],
  },
  reminders: {
    label: "The Reminders page: what needs someone now, and what's coming up",
    keep: [
      ".page-head",
      "main > .sec-head:has(+ .needs-list)",
      ".needs-list",
      ".needs-list + .sec-head",
      ".needs-list + .sec-head + .rem-rows",
    ],
  },
};

function panel(page: Page, keep: string[]) {
  return page.evaluate(keep => {
    const main = document.querySelector("main")!;
    const kept = [...new Set(keep.flatMap(sel => [...main.querySelectorAll(sel)]))];
    const prune = (el: Element) => {
      for (const child of Array.from(el.children)) {
        if (kept.includes(child)) continue;
        if (kept.some(k => child.contains(k))) prune(child);
        else child.remove();
      }
    };
    prune(main);

    const ROOT = /^(:root|html|body|\[data-theme(=light)?\])$/;
    const selectors = (list: string) => list.split(/,(?![^(]*\))/).map(s => s.trim());
    const used = (sel: string) => {
      const bare = sel
        .replace(/::?(before|after|placeholder|marker|selection|-webkit-[\w-]+)/g, "")
        .replace(/:(hover|focus|focus-visible|focus-within|active|disabled|checked)/g, "");
      try {
        return !!main.querySelector(bare);
      } catch {
        return false;
      }
    };
    const scoped = (sel: string) => (ROOT.test(sel) ? ".app" : sel === "*" ? ".app *" : `.app ${sel}`);
    const onlyTokens = (r: CSSStyleRule) => [...r.style].every(p => p.startsWith("--") || p === "color-scheme");

    const tokens: string[] = [];
    const rules: string[] = [];
    const add = (r: CSSRule, into: string[]) => {
      if (r instanceof CSSStyleRule) {
        const body = `{${r.style.cssText}}`;
        const text = r.selectorText.replace(/"/g, "");
        if (text === "[data-theme=dark]") {
          tokens.push(`[data-theme=dark] [data-app-panel]${body}`);
          tokens.push(`@media (prefers-color-scheme: dark){:root:not([data-theme=light]) [data-app-panel]${body}}`);
        } else if (selectors(text).every(s => ROOT.test(s)) && onlyTokens(r)) tokens.push(`[data-app-panel]${body}`);
        else {
          const all = selectors(text);
          const live = all.every(s => ROOT.test(s) || s === "*") ? all : all.filter(used);
          if (live.length) into.push([...new Set(live.map(scoped))].join(",") + body);
        }
      } else if (r instanceof CSSMediaRule) {
        const cond = r.conditionText;
        if (cond.includes("prefers-color-scheme")) {
          const dark = [...r.cssRules].filter(x => x instanceof CSSStyleRule) as CSSStyleRule[];
          tokens.push(
            `@media ${cond}{${dark.map(x => `:root:not([data-theme=light]) [data-app-panel]{${x.style.cssText}}`).join("")}}`,
          );
        } else if (cond.includes("prefers-reduced-motion")) {
          const inner: string[] = [];
          for (const x of r.cssRules) add(x, inner);
          if (inner.length) into.push(`@media ${cond}{${inner.join("")}}`);
        } else if (matchMedia(cond).matches) for (const x of r.cssRules) add(x, into);
      } else if (r instanceof CSSKeyframesRule) into.push(r.cssText);
    };
    for (const sheet of document.styleSheets) for (const r of sheet.cssRules) add(r, rules);
    return { html: main.innerHTML, tokens, rules };
  }, keep);
}

function swap(text: string, find: string, put: string) {
  const at = text.indexOf(find);
  if (at < 0 || text.indexOf(find, at + 1) >= 0) throw new Error(`expected one ${find} in site/index.html`);
  return text.slice(0, at) + put + text.slice(at + find.length);
}

const b64 = (file: string) => fs.readFileSync(path.join(REPO, file)).toString("base64");
const font = (family: string, style: string, weight: string, file: string) =>
  `@font-face{font-family:"${family}";font-style:${style};font-weight:${weight};font-display:swap;` +
  `src:url(data:font/woff2;base64,${b64(file)}) format("woff2")}`;

test.describe.configure({ mode: "serial" });

test("page", async ({ page }) => {
  let text = fs.readFileSync(SOURCE, "utf8");
  const tokens = new Set<string>();
  for (const [name, { label, keep }] of Object.entries(PANELS)) {
    await serveFromDisk(page);
    await page.setViewportSize({ width: WIDTH, height: 1400 });
    await page.goto(`${ORIGIN}/index.html?demo&tab=${name}&theme=light`);
    await page.locator("main h1").first().waitFor();
    const p = await panel(page, keep);
    p.tokens.forEach(r => tokens.add(r));
    const app = `<div class="app" inert>${p.html}</div>`;
    const shadow = `<template shadowrootmode="open"><style>${p.rules.join("\n")}</style>${app}</template>`;
    const host = `data-app-panel="${name}" role="img" aria-label="${label.replace(/"/g, "&quot;")}">`;
    text = swap(text, `data-app-panel="${name}"></div>`, `${host}${shadow}</div>`);
  }
  text = swap(text, "</head>", `<style>\n${[...tokens].join("\n")}\n</style>\n</head>`);
  const fonts = [
    font("Newsreader", "normal", "400 500", "webui/src/fonts/newsreader-latin.woff2"),
    font("Newsreader", "italic", "400", "webui/src/fonts/newsreader-italic-latin.woff2"),
    font("Instrument Sans", "normal", "400 600", "webui/src/fonts/instrument-sans-latin.woff2"),
  ];
  const google = text.slice(text.indexOf("<!-- The only external request."), text.indexOf("<style>"));
  text = swap(text, google, `<style>\n${fonts.join("\n")}\n</style>\n`);
  text = swap(
    text,
    `href="../webui/src/favicon.svg"`,
    `href="data:image/svg+xml;base64,${b64("webui/src/favicon.svg")}"`,
  );
  text = swap(
    text,
    `href="../webui/src/apple-touch-icon.png"`,
    `href="data:image/png;base64,${b64("webui/src/apple-touch-icon.png")}"`,
  );
  fs.mkdirSync(OUT, { recursive: true });
  fs.writeFileSync(path.join(OUT, "index.html"), text);
});

test("link preview", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1200, height: 630 });
  await page.goto("file://" + path.join(OUT, "index.html"));
  await fontsReady(page);
  await page.screenshot({ path: path.join(OUT, "og.png") });
});
