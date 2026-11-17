class Tabs {
  constructor(list) {
    this.tabs = [...list.querySelectorAll('[role="tab"]')];
    this.tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => this.select(tab));
      tab.addEventListener("keydown", (event) => this.onKey(event, index));
    });
  }

  select(tab) {
    for (const other of this.tabs) {
      const selected = other === tab;
      other.setAttribute("aria-selected", String(selected));
      other.tabIndex = selected ? 0 : -1;
      document.getElementById(other.getAttribute("aria-controls")).hidden = !selected;
    }
  }

  onKey(event, index) {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (!step) return;
    const next = this.tabs[(index + step + this.tabs.length) % this.tabs.length];
    this.select(next);
    next.focus();
  }

  selectById(id) {
    const tab = this.tabs.find((t) => t.id === id);
    if (tab) this.select(tab);
  }
}

class CopyButton {
  static RESET_MS = 1600;

  constructor(block) {
    this.source = block.querySelector("code");
    this.button = Object.assign(document.createElement("button"), { type: "button", textContent: "Copy" });
    this.button.addEventListener("click", () => this.copy());
    block.append(this.button);
  }

  async copy() {
    const text = this.source.textContent;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = Object.assign(document.createElement("textarea"), { value: text });
      document.body.append(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    this.button.textContent = "Copied";
    this.button.classList.add("done");
    setTimeout(() => {
      this.button.textContent = "Copy";
      this.button.classList.remove("done");
    }, CopyButton.RESET_MS);
  }
}

function visitorSystem() {
  const platform = (navigator.userAgentData?.platform || navigator.platform || "").toLowerCase();
  if (platform.includes("mac")) return "tab-mac";
  if (platform.includes("linux") && !/android/i.test(navigator.userAgent)) return "tab-linux";
  return "tab-win";
}

for (const list of document.querySelectorAll('[role="tablist"]')) new Tabs(list).selectById(visitorSystem());
for (const block of document.querySelectorAll(".cmd")) new CopyButton(block);
