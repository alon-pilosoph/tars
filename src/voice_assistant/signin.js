// The sign-in page's passkey: asks the server for a challenge, has the browser's passkey answer it, and sends the
// answer back. The server sets the session cookie; then back to where the person was going.
const go = document.getElementById("go");
const said = document.getElementById("said");
const invite = go.dataset.kind === "invite" ? location.pathname.split("/").pop() : null;
const base = invite ? `/api/sign-in/invite/${invite}` : "/api/sign-in";

const bytes = s => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), c => c.charCodeAt(0));
const text = b =>
  btoa(String.fromCharCode(...new Uint8Array(b)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

// Where ?next= sends the person once they're in: a page of this site only, else Home. It's resolved the way the
// browser would resolve it, so "/\evil.example" (a backslash counts as a slash), "//evil.example", another site's
// address and "javascript:" all go Home; and only the path, query and fragment are used, never a scheme or host.
function back(next) {
  if (!next) return "/";
  let url;
  try {
    url = new URL(next, location.origin);
  } catch {
    return "/";
  }
  return url.origin === location.origin ? url.pathname + url.search + url.hash : "/";
}

function answer(c) {
  const r = c.response;
  const out = {
    id: c.id,
    rawId: text(c.rawId),
    type: c.type,
    clientExtensionResults: c.getClientExtensionResults(),
    response: { clientDataJSON: text(r.clientDataJSON) },
  };
  if (r.attestationObject) {
    out.response.attestationObject = text(r.attestationObject);
    out.response.transports = r.getTransports ? r.getTransports() : [];
  } else {
    out.response.authenticatorData = text(r.authenticatorData);
    out.response.signature = text(r.signature);
    out.response.userHandle = r.userHandle ? text(r.userHandle) : null;
  }
  return out;
}

go.addEventListener("click", async () => {
  if (!window.PublicKeyCredential || !window.isSecureContext) {
    said.textContent = "Passkeys need this page over HTTPS. Open TARS by its https:// address.";
    return;
  }
  go.disabled = true;
  said.textContent = "";
  try {
    const { ceremony, options } = await post(`${base}/options`, {});
    options.challenge = bytes(options.challenge);
    let credential;
    if (invite) {
      options.user.id = bytes(options.user.id);
      options.excludeCredentials = (options.excludeCredentials || []).map(c => ({ ...c, id: bytes(c.id) }));
      credential = await navigator.credentials.create({ publicKey: options });
    } else {
      options.allowCredentials = (options.allowCredentials || []).map(c => ({ ...c, id: bytes(c.id) }));
      credential = await navigator.credentials.get({ publicKey: options });
    }
    const { person } = await post(base, { ceremony, credential: answer(credential) });
    said.textContent = `Signed in as ${person}.`;
    location.assign(back(new URLSearchParams(location.search).get("next")));
  } catch (e) {
    said.textContent = e.name === "NotAllowedError" ? "That was cancelled. Try again when you're ready." : e.message;
    go.disabled = false;
  }
});
