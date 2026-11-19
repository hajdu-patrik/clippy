const KEYBOARD_MIN = 120;
const FIELDS = /^(INPUT|TEXTAREA|SELECT)$/;

export function bindViewport() {
  const view = window.visualViewport;
  if (!view) return;
  const root = document.documentElement;
  const fit = () => {
    const covered = window.innerHeight - view.height;
    const typing = FIELDS.test(document.activeElement?.tagName || "");
    if (typing && covered > KEYBOARD_MIN) {
      root.style.setProperty("--vvh", `${view.height}px`);
      root.dataset.keyboard = "1";
      window.scrollTo(0, 0);
    } else {
      root.style.removeProperty("--vvh");
      delete root.dataset.keyboard;
    }
  };
  view.addEventListener("resize", fit);
  view.addEventListener("scroll", fit);
  document.addEventListener("focusout", () => setTimeout(fit, 80));
}
