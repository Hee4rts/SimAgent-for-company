"use strict";

const $ = (id) => document.getElementById(id);
const form = $("task-form");
const input = $("question");
const messages = $("messages");
const scrollArea = $("scroll-area");
let configured = false;
let busy = false;
let controller = null;
let followOutput = true;

function notice(message = "") {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

function controls(value) {
  busy = value;
  input.disabled = !configured || busy;
  $("send").hidden = busy;
  $("send").disabled = !configured || !input.value.trim();
  $("stop").hidden = !busy;
  $("stop").disabled = !controller;
  $("new-task").disabled = busy;
}

function scrollDown() {
  if (followOutput) scrollArea.scrollTop = scrollArea.scrollHeight;
}

scrollArea.addEventListener("scroll", () => {
  followOutput = scrollArea.scrollHeight - scrollArea.scrollTop - scrollArea.clientHeight < 100;
});

function inlineText(container, text) {
  const pattern = /(\*\*([^*\n]+)\*\*|`([^`\n]+)`)/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    container.append(document.createTextNode(text.slice(offset, match.index)));
    const node = document.createElement(match[2] ? "strong" : "code");
    node.textContent = match[2] || match[3];
    container.append(node);
    offset = match.index + match[0].length;
  }
  container.append(document.createTextNode(text.slice(offset)));
}

function renderText(container, text) {
  container.replaceChildren();
  // Parse complete fences separately; all model content is inserted as text nodes.
  const fence = /```[^\n]*\n([\s\S]*?)```/g;
  let offset = 0;
  const prose = (value) => {
    for (const line of value.split("\n")) {
      if (/^#{1,6}\s/.test(line)) {
        const heading = document.createElement("h3");
        inlineText(heading, line.replace(/^#{1,6}\s+/, ""));
        container.append(heading);
      } else {
        inlineText(container, line);
        container.append(document.createTextNode("\n"));
      }
    }
  };
  for (const match of text.matchAll(fence)) {
    prose(text.slice(offset, match.index));
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.textContent = match[1];
    pre.append(code);
    container.append(pre);
    offset = match.index + match[0].length;
  }
  prose(text.slice(offset));
}

function addMessage(role, content = "") {
  $("welcome").hidden = true;
  const article = document.createElement("article");
  article.className = `message ${role}`;
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  if (role === "assistant") {
    const icon = document.createElement("img");
    icon.src = "/static/favicon.svg";
    icon.alt = "";
    avatar.append(icon);
  } else avatar.textContent = "你";
  const inner = document.createElement("div");
  inner.className = "message-inner";
  const label = document.createElement("div");
  label.className = "message-label";
  label.textContent = role === "assistant" ? "simAgent" : "你";
  const body = document.createElement("div");
  body.className = "message-body";
  if (role === "assistant") renderText(body, content);
  else body.textContent = content;
  const note = document.createElement("div");
  note.className = "message-note";
  note.setAttribute("role", "status");
  inner.append(label, body, note);
  article.append(avatar, inner);
  messages.append(article);
  scrollDown();
  return { body, note, inner };
}

function addCopy(message, text) {
  const button = document.createElement("button");
  button.className = "copy-button";
  button.type = "button";
  button.textContent = "复制回答";
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "已复制";
      setTimeout(() => { button.textContent = "复制回答"; }, 1600);
    } catch { notice("浏览器不允许访问剪贴板，可以选中回答后手动复制。"); }
  });
  message.inner.append(button);
}

async function requestJSON(url, options = {}) {
  const response = await fetch(url, { credentials: "same-origin", ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求失败，请稍后重试。");
  return data;
}

function showSession(data) {
  configured = data.configured;
  notice(configured ? "" : "请在项目根目录的 .env 中填写 DEEPSEEK_API_KEY，并重启服务后刷新此页。");
  messages.replaceChildren();
  $("welcome").hidden = data.messages.length > 0;
  for (const item of data.messages) {
    const message = addMessage(item.role, item.content);
    if (item.role === "assistant") addCopy(message, item.content);
  }
  controls(data.busy);
  if (data.busy) notice("当前任务正在其他页面生成回答，完成后会自动更新。");
  else if (configured) input.focus();
  scrollDown();
}

async function loadSession() {
  try {
    const data = await requestJSON("/api/session");
    showSession(data);
    if (data.busy) setTimeout(loadSession, 1500);
  } catch {
    notice("无法连接服务，请确认启动命令仍在运行，然后刷新页面。");
  }
}

$("new-task").addEventListener("click", async () => {
  if (busy) return;
  try {
    const data = await requestJSON("/api/session/reset", { method: "POST" });
    input.value = "";
    followOutput = true;
    showSession(data);
  } catch (error) { notice(error.message); }
});

input.addEventListener("input", () => {
  $("send").disabled = !configured || !input.value.trim();
});
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!busy && input.value.trim()) form.requestSubmit();
  }
});

$("stop").addEventListener("click", async () => {
  $("stop").disabled = true;
  try {
    await requestJSON("/api/chat/cancel", { method: "POST" });
  } catch {
    if (controller) controller.abort();
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = input.value.trim();
  if (!configured || busy || !question) return;
  notice();
  controller = new AbortController();
  controls(true);
  followOutput = true;
  addMessage("user", question);
  input.value = "";
  const answer = addMessage("assistant");
  answer.body.textContent = "正在思考…";
  answer.body.classList.add("thinking");
  let text = "";
  let finished = false;
  let stopped = false;

  function onEvent(item) {
    if (item.type === "delta" || item.type === "replace") {
      text = item.type === "replace" ? item.text : text + item.text;
      answer.body.classList.remove("thinking");
      renderText(answer.body, text);
      scrollDown();
    } else if (item.type === "done") {
      if (typeof item.text === "string") {
        text = item.text;
        renderText(answer.body, text);
      }
      finished = true;
    }
    else if (item.type === "stopped") stopped = true;
    else if (item.type === "error") throw new Error(item.message);
  }

  try {
    const response = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: question }), credentials: "same-origin",
      signal: controller.signal,
    });
    if (!response.ok) {
      const error = await response.json();
      throw new Error(typeof error.detail === "string" ? error.detail : "问题过长或格式无效，请调整后重试。");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let end;
      while ((end = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, end);
        buffer = buffer.slice(end + 1);
        if (line.trim()) onEvent(JSON.parse(line));
      }
      if (done) break;
    }
    if (buffer.trim()) onEvent(JSON.parse(buffer));
    if (!finished && !stopped) throw new Error("连接中断，回答未完成，请重新提问。");
    if (stopped) answer.note.textContent = "已停止，本轮未加入后续上下文。";
  } catch (error) {
    stopped = error.name === "AbortError";
    answer.note.classList.toggle("error", !stopped);
    answer.note.textContent = stopped ? "已停止，本轮未加入后续上下文。" : `${error.message} 本轮未加入上下文。`;
    if (!stopped) input.value = question;
  } finally {
    if (!finished && controller) controller.abort();
    answer.body.classList.remove("thinking");
    if (!text) answer.body.textContent = stopped ? "生成已停止。" : "未收到回答。";
    if (text) addCopy(answer, text);
    controller = null;
    controls(false);
    input.focus();
    scrollDown();
  }
});

loadSession();
