/* Signing in with a passkey, end to end: Chromium's virtual authenticator stands in for a phone's passkey, against a
   real backend with [web] sign_in on (tools/webui_demo.py serve --sign-in, an empty log). `npm run e2e` runs it after
   e2e.spec.ts. The steps run in order in one page and depend on each other. */
import { type Page, expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { SIGN_IN_DATA, SIGN_IN_URL } from "./e2e.config";
import { REPO } from "./serve";

/** A one-time invite token, made as `voice-assistant --web-invite` makes one: in the database, by another process. */
const invite = (person: string) =>
  execFileSync("uv", ["run", "python", "tools/webui_demo.py", "invite", SIGN_IN_DATA, person], {
    cwd: REPO,
    encoding: "utf8",
  }).trim();

test.describe.configure({ mode: "serial" });
let page: Page;
let token: string;
const me = async () => ((await (await page.request.get("/api/me")).json()) as { person: string }).person;
/** Signs out from the sign-in page, which asks the API nothing: on the web UI, a request still on its way would be
    refused and send the page to sign-in while the test goes somewhere else. */
async function signOut() {
  await page.goto("/sign-in");
  return page.evaluate(() => fetch("/api/sign-out", { method: "POST" }).then(r => r.status));
}
/** The web UI itself, loaded and signed in: its header is up and nothing sent it back to sign in. */
async function inTheWebUI(url: string) {
  await expect(page.locator("header.top")).toBeVisible();
  await expect(page).toHaveURL(url);
}

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext({ baseURL: SIGN_IN_URL });
  // Anything sent to another site is caught here, so a redirect that leaks shows as that site's address.
  await context.route(
    url => url.hostname === "evil.example",
    route => route.fulfill({ body: "not TARS" }),
  );
  page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2",
      transport: "internal",
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
});
test.afterAll(() => page.context().close());

test("without a session, the API answers 401 and the page sends you to sign in", async () => {
  expect((await page.request.get("/api/events")).status()).toBe(401);
  await page.goto("/?tab=review");
  await expect(page).toHaveURL(`${SIGN_IN_URL}/sign-in?next=${encodeURIComponent("/?tab=review")}`);
  await expect(page.getByRole("button", { name: "Sign in with a passkey" })).toBeVisible();
});

test("an invite makes a passkey, signs in, and opens the web UI", async () => {
  token = invite("Stacey");
  await page.goto(`/sign-in/invite/${token}`);
  await expect(page.getByRole("heading", { name: "Welcome, Stacey" })).toBeVisible();
  await page.getByRole("button", { name: "Make a passkey" }).click();
  await inTheWebUI(`${SIGN_IN_URL}/`);
  expect(await me()).toBe("stacey");
  expect((await page.request.get("/api/events")).status()).toBe(200);
});

test("the invite works once", async () => {
  const r = await page.goto(`/sign-in/invite/${token}`);
  expect(r?.status()).toBe(410);
  await expect(page.getByRole("heading", { name: "This link has been used" })).toBeVisible();
  expect(
    (await page.request.post(`/api/sign-in/invite/${token}/options`, { headers: { Origin: SIGN_IN_URL } })).status(),
  ).toBe(410);
});

test("signed out, the page goes back to sign-in, and the passkey signs in again to where it was", async () => {
  expect(await signOut()).toBe(200);
  expect((await page.request.get("/api/me")).status()).toBe(401);
  await page.goto("/?tab=voices#top");
  await expect(page).toHaveURL(/\/sign-in\?next=/);
  await page.getByRole("button", { name: "Sign in with a passkey" }).click();
  await inTheWebUI(`${SIGN_IN_URL}/?tab=voices#top`);
  expect(await me()).toBe("stacey");
});

// ?next= is the page's own address (api.ts), but anyone can send a link with another one: a page of this site only.
for (const [next, to] of [
  ["/\\evil.example", "/"], // a backslash is a slash to the browser: //evil.example
  ["//evil.example", "/"],
  ["https://evil.example/", "/"],
  ["javascript:document.title='pwned'", "/"],
  ["/?tab=reminders#soon", "/?tab=reminders#soon"],
  [`${SIGN_IN_URL}/?tab=models`, "/?tab=models"], // this site's own address, in full
]) {
  test(`after signing in, ?next=${next} goes to ${to}`, async () => {
    expect(await signOut()).toBe(200);
    await page.goto(`/sign-in?next=${encodeURIComponent(next)}`);
    await page.getByRole("button", { name: "Sign in with a passkey" }).click();
    await inTheWebUI(`${SIGN_IN_URL}${to}`);
    expect(await page.title()).not.toBe("pwned");
  });
}
