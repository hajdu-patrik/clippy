(() => {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const show = (tab) => {
    for (const other of tabs) {
      const on = other === tab;
      other.setAttribute("aria-selected", String(on));
      other.tabIndex = on ? 0 : -1;
      document.getElementById(other.getAttribute("aria-controls")).hidden = !on;
    }
  };
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => show(tab));
    tab.addEventListener("keydown", (e) => {
      const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!step) return;
      const next = tabs[(i + step + tabs.length) % tabs.length];
      show(next);
      next.focus();
    });
  });
  const platform = (navigator.userAgentData?.platform || navigator.platform || "").toLowerCase();
  const guess = platform.includes("mac") ? "tab-mac" : platform.includes("linux") && !/android/.test(navigator.userAgent.toLowerCase())
    ? "tab-linux" : "tab-win";
  show(document.getElementById(guess));

  for (const block of document.querySelectorAll(".cmd")) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Copy";
    button.addEventListener("click", async () => {
      const text = block.querySelector("code").textContent;
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        const area = document.createElement("textarea");
        area.value = text;
        document.body.append(area);
        area.select();
        document.execCommand("copy");
        area.remove();
      }
      button.textContent = "Copied";
      button.classList.add("done");
      setTimeout(() => { button.textContent = "Copy"; button.classList.remove("done"); }, 1600);
    });
    block.append(button);
  }
})();
