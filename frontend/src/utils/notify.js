// In-page replacements for window.alert / window.confirm, styled like the app.
// Plain DOM so any handler can call them without wiring a React provider.

let stack;
function host() {
  if (!stack || !document.body.contains(stack)) {
    stack = document.createElement("div");
    stack.setAttribute("aria-live", "polite");
    stack.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:1000;display:flex;flex-direction:column;gap:8px;max-width:min(420px,calc(100vw - 32px))";
    document.body.appendChild(stack);
  }
  return stack;
}

// toast("Download failed.", "error") — disappears after a few seconds or on click
export function toast(message, kind = "info") {
  const el = document.createElement("div");
  const err = kind === "error";
  el.style.cssText = `padding:12px 14px;border-radius:10px;font-size:13px;line-height:1.4;white-space:pre-line;cursor:pointer;
    box-shadow:0 6px 20px rgba(11,44,60,.18);border:1px solid ${err ? "#fecaca" : "#bcdce5"};
    background:${err ? "#fef2f2" : "#f1f7f9"};color:${err ? "#b91c1c" : "#0b2c3c"}`;
  el.setAttribute("role", err ? "alert" : "status");
  el.textContent = message;
  el.onclick = () => el.remove();
  host().appendChild(el);
  setTimeout(() => el.remove(), err ? 8000 : 4000);
}

// if (!(await confirmDialog("Delete this?"))) return;
export function confirmDialog(message, { okLabel = "Confirm", danger = true } = {}) {
  return new Promise((resolve) => {
    const back = document.createElement("div");
    back.style.cssText = "position:fixed;inset:0;z-index:1001;background:rgba(7,24,38,.45);display:flex;align-items:center;justify-content:center;padding:16px";
    const box = document.createElement("div");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.style.cssText = "background:#fff;border-radius:14px;max-width:420px;width:100%;padding:20px;box-shadow:0 20px 50px rgba(7,24,38,.3);font-size:14px;color:#0b2c3c";
    const p = document.createElement("p");
    p.textContent = message;
    p.style.cssText = "margin:0 0 18px;line-height:1.5;white-space:pre-line";
    const row = document.createElement("div");
    row.style.cssText = "display:flex;justify-content:flex-end;gap:8px";
    const btn = (label, primary) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.style.cssText = `padding:8px 14px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;border:1px solid ${
        primary ? (danger ? "#b91c1c" : "#006884") : "#cad5ca"};background:${primary ? (danger ? "#b91c1c" : "#006884") : "#fff"};color:${primary ? "#fff" : "#36555b"}`;
      return b;
    };
    const cancel = btn("Cancel", false);
    const ok = btn(okLabel, true);
    const done = (v) => { back.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
    const onKey = (e) => { if (e.key === "Escape") done(false); if (e.key === "Enter") done(true); };
    cancel.onclick = () => done(false);
    ok.onclick = () => done(true);
    back.onclick = (e) => { if (e.target === back) done(false); };
    document.addEventListener("keydown", onKey);
    row.append(cancel, ok);
    box.append(p, row);
    back.appendChild(box);
    document.body.appendChild(back);
    ok.focus();
  });
}
