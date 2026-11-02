"use strict";

const state = { conversationId: null, busy: false };
const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (response.status === 401) {
    window.location.reload();
    throw new Error("Sesión caducada");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `Error ${response.status}`);
  }
  return response.json();
}

function addMessage(role, text, extraClass = "") {
  const div = document.createElement("div");
  div.className = `msg ${role} ${extraClass}`.trim();
  div.textContent = text;
  $("messages").appendChild(div);
  $("messages").scrollTop = $("messages").scrollHeight;
  return div;
}

async function loadConversations() {
  const list = await api("/api/conversations");
  const ul = $("conversations");
  ul.replaceChildren();
  for (const conv of list) {
    const li = document.createElement("li");
    li.textContent = (conv.channel === "telegram" ? "📱 " : "") + conv.title;
    li.title = conv.title;
    if (conv.id === state.conversationId) li.classList.add("active");
    li.addEventListener("click", () => openConversation(conv.id));
    ul.appendChild(li);
  }
}

async function openConversation(id) {
  state.conversationId = id;
  $("messages").replaceChildren();
  for (const m of await api(`/api/conversations/${encodeURIComponent(id)}/messages`)) {
    addMessage(m.role, m.content);
  }
  await loadConversations();
}

async function send(event) {
  event.preventDefault();
  const text = $("input").value.trim();
  if (!text || state.busy) return;
  state.busy = true;
  $("send").disabled = true;
  $("input").value = "";
  addMessage("user", text);
  const pending = addMessage("assistant", "Jarvis está trabajando…", "pending");
  try {
    const data = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message: text, conversation_id: state.conversationId }),
    });
    state.conversationId = data.conversation_id;
    pending.classList.remove("pending");
    pending.textContent = data.reply;
    await loadConversations();
  } catch (err) {
    pending.textContent = `No se pudo enviar: ${err.message}`;
  } finally {
    state.busy = false;
    $("send").disabled = false;
    $("input").focus();
  }
}

$("composer").addEventListener("submit", send);
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) send(e);
});
$("new-chat").addEventListener("click", () => {
  state.conversationId = null;
  $("messages").replaceChildren();
  loadConversations();
});
loadConversations();
