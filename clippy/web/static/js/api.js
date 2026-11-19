const CSRF = { "X-Clippy-Request": "1" };
const JSON_HEADERS = { "Content-Type": "application/json", ...CSRF };

function failure(status, statusText, body) {
  const detail = body?.detail;
  const err = new Error(typeof detail === "string" ? detail : detail?.message || statusText || "Request failed");
  if (detail?.key) err.t = { key: detail.key, params: detail.params || {} };
  err.status = status;
  return err;
}

async function request(path, options = {}) {
  const res = await fetch(path, { ...options, headers: JSON_HEADERS });
  if (!res.ok) {
    let body = null;
    try { body = await res.json(); } catch { body = null; }
    throw failure(res.status, res.statusText, body);
  }
  return res.json();
}

function upload(chatId, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/chats/${chatId}/assets?name=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("X-Clippy-Request", "1");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress?.(e.loaded / e.total); };
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch { body = null; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(failure(xhr.status, xhr.statusText, body));
    };
    xhr.onerror = () => reject(failure(0, "Network error", null));
    xhr.send(file);
  });
}

export const api = {
  status: () => request("/api/status"),
  i18n: () => request("/api/i18n"),
  chats: () => request("/api/chats"),
  chat: (id) => request(`/api/chats/${id}`),
  createChat: () => request("/api/chats", { method: "POST" }),
  renameChat: (id, title) => request(`/api/chats/${id}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  deleteChat: (id) => request(`/api/chats/${id}`, { method: "DELETE" }),
  send: (id, text, attachments = []) =>
    request(`/api/chats/${id}/messages`, { method: "POST", body: JSON.stringify({ text, attachments }) }),
  upload,
  undo: (id, number) => request(`/api/chats/${id}/clips/${number}/undo`, { method: "POST" }),
  settings: () => request("/api/settings"),
  saveSettings: (profileLinks) =>
    request("/api/settings", { method: "PUT", body: JSON.stringify({ profile_links: profileLinks }) }),
  llm: () => request("/api/llm"),
  saveLlm: (body) => request("/api/llm", { method: "PUT", body: JSON.stringify(body) }),
  llmProblem: (mode, model) =>
    request(`/api/llm/problem?mode=${encodeURIComponent(mode)}&model=${encodeURIComponent(model)}`),
  testLlm: (body) => request("/api/llm/test", { method: "POST", body: JSON.stringify(body) }),
  accounts: () => request("/api/accounts"),
  connectAccount: (platform) => request(`/api/accounts/${platform}/connect`, { method: "POST" }),
  disconnectAccount: (platform) => request(`/api/accounts/${platform}`, { method: "DELETE" }),
  instagramToken: (token) => request("/api/accounts/instagram", { method: "PUT", body: JSON.stringify({ token }) }),
  publish: (id, number, body) =>
    request(`/api/chats/${id}/clips/${number}/publish`, { method: "POST", body: JSON.stringify(body) }),
  cancelScheduled: (id) => request(`/api/scheduled/${id}`, { method: "DELETE" }),
  dictate: async (blob) => {
    const res = await fetch("/api/dictate", {
      method: "POST", body: blob, headers: { "X-Clippy-Request": "1", "Content-Type": "application/octet-stream" } });
    let body = null;
    try { body = await res.json(); } catch { body = null; }
    if (!res.ok) throw failure(res.status, res.statusText, body);
    return body;
  },
  kit: () => request("/api/kit"),
  saveKit: (body) => request("/api/kit", { method: "PUT", body: JSON.stringify(body) }),
  removeKitFile: (slot) => request(`/api/kit/${slot}`, { method: "DELETE" }),
  kitFile: async (slot, file) => {
    const res = await fetch(`/api/kit/${slot}?name=${encodeURIComponent(file.name)}`, {
      method: "PUT", body: file, headers: { "X-Clippy-Request": "1", "Content-Type": "application/octet-stream" } });
    let body = null;
    try { body = await res.json(); } catch { body = null; }
    if (!res.ok) throw failure(res.status, res.statusText, body);
    return body;
  },
  results: () => request("/api/results"),
  refreshResults: () => request("/api/results/refresh", { method: "POST" }),
  watches: () => request("/api/watches"),
  addWatch: (body) => request("/api/watches", { method: "POST", body: JSON.stringify(body) }),
  toggleWatch: (id, enabled) => request(`/api/watches/${id}`, { method: "PATCH", body: JSON.stringify({ enabled }) }),
  checkWatch: (id) => request(`/api/watches/${id}/check`, { method: "POST" }),
  deleteWatch: (id) => request(`/api/watches/${id}`, { method: "DELETE" }),
  daily: () => request("/api/daily"),
  addDaily: (body) => request("/api/daily", { method: "POST", body: JSON.stringify(body) }),
  toggleDaily: (id, enabled) => request(`/api/daily/${id}`, { method: "PATCH", body: JSON.stringify({ enabled }) }),
  runDaily: (id) => request(`/api/daily/${id}/run`, { method: "POST" }),
  deleteDaily: (id) => request(`/api/daily/${id}`, { method: "DELETE" }),
  events: () => new EventSource("/api/events"),
};
