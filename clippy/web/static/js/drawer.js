import { $ } from "./util.js";

const EDGE = 28;
const MOBILE = window.matchMedia("(max-width: 900px)");
let returnFocus = null;

const sidebar = () => $("#sidebar");
const scrim = () => $("#scrim");
export const isOpen = () => sidebar().classList.contains("open");

export function openDrawer() {
  if (!MOBILE.matches) return;
  returnFocus = document.activeElement;
  scrim().hidden = false;
  requestAnimationFrame(() => scrim().classList.add("show"));
  sidebar().classList.add("open");
  $("#openSide").setAttribute("aria-expanded", "true");
  setTimeout(() => sidebar().querySelector(".chat-item.active a, #newChat")?.focus({ preventScroll: true }), 320);
}

export function closeDrawer() {
  if (!isOpen()) return;
  sidebar().classList.remove("open");
  sidebar().style.transform = "";
  scrim().classList.remove("show");
  scrim().style.opacity = "";
  $("#openSide").setAttribute("aria-expanded", "false");
  setTimeout(() => { if (!isOpen()) scrim().hidden = true; }, 320);
  if (returnFocus?.isConnected && MOBILE.matches) returnFocus.focus({ preventScroll: true });
}

function bindSwipe() {
  let drag = null;
  const width = () => sidebar().getBoundingClientRect().width;

  document.addEventListener("touchstart", (e) => {
    if (!MOBILE.matches || e.touches.length !== 1) return;
    const t = e.touches[0];
    const open = isOpen();
    if (!open && t.clientX > EDGE) return;
    if (open && !sidebar().contains(e.target) && e.target !== scrim()) return;
    drag = { x0: t.clientX, y0: t.clientY, t0: performance.now(), open, decided: false, horizontal: false, dx: 0 };
  }, { passive: true });

  document.addEventListener("touchmove", (e) => {
    if (!drag) return;
    const t = e.touches[0];
    const dx = t.clientX - drag.x0, dy = t.clientY - drag.y0;
    if (!drag.decided) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      drag.decided = true;
      drag.horizontal = Math.abs(dx) > Math.abs(dy) * 1.2;
      if (!drag.horizontal) { drag = null; return; }
      sidebar().classList.add("dragging");
      scrim().hidden = false;
    }
    const w = width();
    drag.dx = dx;
    const offset = drag.open ? Math.min(0, dx) : Math.min(0, -w + dx);
    sidebar().style.transform = `translateX(${offset}px)`;
    scrim().style.opacity = String(Math.max(0, Math.min(1, 1 + offset / w)));
  }, { passive: true });

  const end = () => {
    if (!drag) return;
    const { dx, open, t0, horizontal } = drag;
    drag = null;
    if (!horizontal) return;
    sidebar().classList.remove("dragging");
    const w = width();
    const velocity = dx / Math.max(1, performance.now() - t0);
    sidebar().style.transform = "";
    scrim().style.opacity = "";
    const shouldOpen = open ? !(dx < -w * 0.3 || velocity < -0.45) : (dx > w * 0.35 || velocity > 0.45);
    if (shouldOpen) {
      if (!open) openDrawer();
      else scrim().classList.add("show");
    } else if (open) {
      closeDrawer();
    } else {
      scrim().hidden = true;
    }
  };
  document.addEventListener("touchend", end);
  document.addEventListener("touchcancel", end);
}

export function bindDrawer() {
  $("#openSide").addEventListener("click", openDrawer);
  $("#closeSide").addEventListener("click", closeDrawer);
  scrim().addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  MOBILE.addEventListener("change", () => { if (!MOBILE.matches) closeDrawer(); });
  bindSwipe();
}
