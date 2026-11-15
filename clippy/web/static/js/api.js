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
  events: () => new EventSource("/api/events"),
};
