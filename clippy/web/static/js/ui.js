import { t } from "./i18n.js";
import { POSTING, platformLabel } from "./platforms.js";
import { esc } from "./util.js";

const dataAttrs = (data = {}) => Object.entries(data).map(([k, v]) => ` data-${k}="${esc(v)}"`).join("");

export function button(label, { variant = "ghost", size = "sm", data, attrs = "" } = {}) {
  const cls = ["btn", size, variant].filter(Boolean).join(" ");
  return `<button type="button" class="${cls}"${dataAttrs(data)}${attrs ? ` ${attrs}` : ""}>${esc(label)}</button>`;
}

export const actions = (...items) => `<div class="watch-btns">${items.filter(Boolean).join("")}</div>`;

export function blank(text, { tag = "div", alert = false } = {}) {
  return `<${tag} class="blank"><p${alert ? ' role="alert"' : ""}>${esc(text)}</p></${tag}>`;
}

export function row({ title, state = "", error = "", tail = "", cls = "", data }) {
  const err = error ? `<span class="account-error">${esc(error)}</span>` : "";
  return `<li class="account ${cls}"${dataAttrs(data)}><div><b>${esc(title)}</b>`
    + `<span class="account-state">${esc(state)}</span>${err}</div>${tail}</li>`;
}

export class PlatformPicker {
  constructor(box, { name = "platform", accounts = null } = {}) {
    this.box = box;
    this.name = name;
    this.accounts = accounts;
  }

  account(platform) {
    return this.accounts ? this.accounts()?.[platform] || {} : { connected: true };
  }

  option(platform, keep) {
    const a = this.account(platform);
    const usable = a.connected;
    const who = this.accounts ? ` <small>${esc(usable ? a.name || "" : t("accounts.notConnected"))}</small>` : "";
    const checked = usable && keep.includes(platform) ? " checked" : "";
    return `<label class="check${usable ? "" : " off"}"><input type="checkbox" name="${this.name}" value="${platform}"`
      + `${usable ? "" : " disabled"}${checked}><span>${platformLabel(platform)}${who}</span></label>`;
  }

  render(keep = []) {
    this.box.innerHTML = POSTING.map((p) => this.option(p, keep)).join("");
  }

  usable() {
    return POSTING.filter((p) => this.account(p).connected);
  }

  selected() {
    return [...this.box.querySelectorAll(`input[name="${this.name}"]:checked`)].map((x) => x.value);
  }

  clear() {
    this.box.querySelectorAll("input").forEach((x) => { x.checked = false; });
  }
}
