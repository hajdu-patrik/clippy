export const LABELS = {
  youtube: "YouTube", instagram: "Instagram", facebook: "Facebook", tiktok: "TikTok", x: "X", twitch: "Twitch", kick: "Kick",
};
export const POSTING = ["youtube", "instagram", "facebook", "tiktok", "x"];

const stroke = 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';
const GLYPHS = {
  youtube: `<rect x="3" y="6.5" width="18" height="11" rx="3.4" ${stroke}/><path d="M10.2 9.6v4.8l4.2-2.4z" fill="currentColor"/>`,
  instagram: `<rect x="4" y="4" width="16" height="16" rx="4.6" ${stroke}/><circle cx="12" cy="12" r="3.6" ${stroke}/><circle cx="16.6" cy="7.4" r="1.1" fill="currentColor"/>`,
  facebook: `<path d="M14 21v-7.5h2.6l.4-3H14V8.6c0-.9.3-1.5 1.6-1.5H17V4.4c-.3 0-1.2-.1-2.2-.1-2.3 0-3.8 1.4-3.8 3.9v2.3H8.5v3H11V21" ${stroke}/>`,
  tiktok: `<path d="M14 4v10.2a3.7 3.7 0 1 1-3.7-3.7M14 4c.3 2.6 1.9 4.2 4.6 4.5" ${stroke}/>`,
  x: `<path d="M5 5l14 14M19 5L5 19" ${stroke}/>`,
  twitch: `<path d="M5 4h15v10.5l-4.5 4.5h-3.5L9.5 21.5V19H5z" ${stroke}/><path d="M15 8.5v4M11 8.5v4" ${stroke}/>`,
  kick: `<path d="M6 4.5v15M6 12l7-7.5M9.5 10.5L15.5 19.5h3.5M13.5 12.8l5.5-5" ${stroke}/>`,
};

export function platformIcon(name) {
  return `<span class="acct-icon pf-${name}" aria-hidden="true"><svg viewBox="0 0 24 24" width="22" height="22" focusable="false">${GLYPHS[name] || ""}</svg></span>`;
}
