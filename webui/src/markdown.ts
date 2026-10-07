/* The little markdown TARS writes notes in: paragraphs, **bold**, *italic*, - lists, 1. lists, # headings. */

export const BOLD = /\*\*(.+?)\*\*/g;
const ITALIC = /\*(.+?)\*/g;

const ENTITIES: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, c => ENTITIES[c] ?? c);

/** The result is rendered as HTML, so everything is escaped before any tag is added. */
export function md(src: string): string {
  let out = "";
  let list: string | null = null;
  const close = () => {
    const c = list ? `</${list}>` : "";
    list = null;
    return c;
  };
  const inl = (t: string) => t.replace(BOLD, "<strong>$1</strong>").replace(ITALIC, "<em>$1</em>");
  const item = (kind: "ul" | "ol", text: string) => {
    if (list !== kind) {
      out += close() + `<${kind}>`;
      list = kind;
    }
    out += `<li>${inl(text)}</li>`;
  };
  for (const l of esc(src).split("\n")) {
    let m;
    if ((m = l.match(/^\s*[-*] (.*)/))) item("ul", m[1]);
    else if ((m = l.match(/^\s*\d+[.)] (.*)/))) item("ol", m[1]);
    else {
      out += close();
      if ((m = l.match(/^#+ (.*)/))) out += `<h4>${inl(m[1])}</h4>`;
      else if (l.trim()) out += `<p>${inl(l)}</p>`;
    }
  }
  return out + close();
}

export const plain = (src: string) => (src || "").replace(BOLD, "$1").replace(ITALIC, "$1");
