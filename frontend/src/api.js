async function request(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "Invalid request";
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return data;
}

const post = (body) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const listChats = () => request("/api/chats");
export const createChat = () => request("/api/chats", { method: "POST" });
export const deleteChat = (id) => request(`/api/chats/${id}`, { method: "DELETE" });
export const getChat = (id) => request(`/api/chats/${id}`);
export const syncChat = (id) => request(`/api/chats/${id}/sync`, { method: "POST" });

export const uploadFiles = (id, files) => {
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  return request(`/api/chats/${id}/files`, { method: "POST", body: fd });
};

export const generateQuiz = (id, body) => request(`/api/chats/${id}/quiz`, post(body));
export const explainAnswer = (id, body) => request(`/api/chats/${id}/explain`, post(body));
