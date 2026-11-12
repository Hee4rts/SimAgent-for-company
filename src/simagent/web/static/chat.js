"use strict";

const $ = (id) => document.getElementById(id);
const input = $("question");
const messages = $("messages");
const scrollArea = $("scroll-area");
const drafts = new Map();
let currentTaskId = new URL(window.location.href).searchParams.get("task");
let configured = false;
let busy = false;
let navigating = false;
let managing = false;
let refreshAfterManagement = false;
let loaded = false;
let followOutput = true;
let connection = null;
let navigationVersion = 0;
let pollTimer = null;
let taskList = [];
let deletedTaskList = [];
let actionTaskId = null;
let renameTaskId = null;
let purgeTaskId = null;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function notice(text = "") {
  $("notice").textContent = text;
  $("notice").hidden = !text;
}

function controls() {
  const pending = navigating || managing;
  input.disabled = !configured || busy || pending;
  $("send").hidden = busy;
  $("send").disabled = !configured || pending || !input.value.trim();
  $("stop").hidden = !busy;
  $("stop").disabled = pending;
  $("new-task").disabled = !loaded || pending;
  $("recently-deleted").disabled = !loaded || pending;
  $("task-name-input").disabled = pending;
  $("rename-save").disabled = pending;
  $("purge-confirm").disabled = pending;
  document.querySelectorAll("[data-create], .task-select, .task-options, .restore-task, .purge-task, [data-close]")
    .forEach((button) => { button.disabled = pending; });
  syncTaskMenu();
}

function menu(open) {
  $("new-task-menu").hidden = !open;
  $("new-task").setAttribute("aria-expanded", String(open));
}

function taskMenuTrigger(id) {
  return [...document.querySelectorAll(".task-options")].find((button) => button.dataset.taskId === id);
}

function closeTaskMenu(restoreFocus = false) {
  const trigger = taskMenuTrigger(actionTaskId);
  actionTaskId = null;
  $("task-actions").hidden = true;
  if (trigger) {
    trigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger.focus();
  }
}

function syncTaskMenu() {
  if (!actionTaskId) return;
  const task = taskList.find((item) => item.id === actionTaskId);
  const trigger = taskMenuTrigger(actionTaskId);
  if (!task || !trigger) { closeTaskMenu(); return; }
  const running = task.id === currentTaskId ? busy : task.busy;
  const popup = $("task-actions");
  popup.hidden = false;
  popup.setAttribute("aria-label", `任务操作：${task.title}`);
  trigger.setAttribute("aria-expanded", "true");
  $("rename-task").disabled = navigating || managing;
  $("delete-task").disabled = navigating || managing || running;
  $("delete-task-hint").hidden = !running;
  $("delete-task").setAttribute("aria-describedby", running ? "delete-task-hint" : "");
  const rect = trigger.getBoundingClientRect();
  const left = Math.max(8, Math.min(rect.right - popup.offsetWidth, window.innerWidth - popup.offsetWidth - 8));
  const top = rect.bottom + popup.offsetHeight + 8 <= window.innerHeight ? rect.bottom + 4 : rect.top - popup.offsetHeight - 4;
  popup.style.left = `${left}px`;
  popup.style.top = `${Math.max(8, top)}px`;
}

function openTaskMenu(id) {
  if (navigating || managing) return;
  if (actionTaskId === id) { closeTaskMenu(true); return; }
  closeTaskMenu();
  menu(false);
  actionTaskId = id;
  syncTaskMenu();
  $("rename-task").focus();
}

function scrollDown() {
  if (followOutput) scrollArea.scrollTop = messages.children.length ? scrollArea.scrollHeight : 0;
}

scrollArea.addEventListener("scroll", () => {
  followOutput = scrollArea.scrollHeight - scrollArea.scrollTop - scrollArea.clientHeight < 100;
});

function inlineText(container, text) {
  const pattern = /(\*\*([^*\n]+)\*\*|`([^`\n]+)`)/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    container.append(document.createTextNode(text.slice(offset, match.index)));
    const node = element(match[2] ? "strong" : "code", "", match[2] || match[3]);
    container.append(node);
    offset = match.index + match[0].length;
  }
  container.append(document.createTextNode(text.slice(offset)));
}

function renderText(container, text) {
  container.replaceChildren();
  const lines = text.split("\n");
  const cells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (/^\s*```/.test(line)) {
      const code = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      if (i < lines.length) i++;
      const pre = element("pre");
      pre.append(element("code", "", code.join("\n")));
      container.append(pre);
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && cells(lines[i + 1]).every((cell) => /^:?-{3,}:?$/.test(cell))) {
      const wrap = element("div", "answer-table");
      const table = element("table");
      const head = element("thead");
      const heading = element("tr");
      cells(line).forEach((cell) => { const th = element("th"); inlineText(th, cell); heading.append(th); });
      head.append(heading);
      const body = element("tbody");
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        const row = element("tr");
        cells(lines[i++]).forEach((cell) => { const td = element("td"); inlineText(td, cell); row.append(td); });
        body.append(row);
      }
      table.append(head, body);
      wrap.append(table);
      container.append(wrap);
      continue;
    }
    const bullet = /^\s*(?:[-*•]|\d+[.)、])\s+/.test(line);
    if (bullet) {
      const ordered = /^\s*\d+[.)、]/.test(line);
      const list = element(ordered ? "ol" : "ul");
      const pattern = ordered ? /^\s*\d+[.)、]\s+/ : /^\s*[-*•]\s+/;
      while (i < lines.length && pattern.test(lines[i])) {
        const li = element("li");
        inlineText(li, lines[i++].replace(pattern, ""));
        list.append(li);
      }
      container.append(list);
      continue;
    }
    const heading = /^#{1,6}\s+/.test(line);
    const node = element(heading ? "h3" : "p");
    inlineText(node, heading ? line.replace(/^#{1,6}\s+/, "") : line);
    container.append(node);
    i++;
  }
}

function addMessage(role, content = "") {
  $("welcome").hidden = true;
  const article = element("article", `message ${role}`);
  const label = element("div", "message-label", role === "user" ? "你" : "simAgent");
  const body = element("div", "message-body");
  if (role === "user") body.textContent = content;
  else renderText(body, content);
  article.append(label, body);
  messages.append(article);
  scrollDown();
  return body;
}

function renderTasks() {
  const focused = document.activeElement;
  const focusedId = focused.dataset.taskId;
  const focusedClass = focused.classList.contains("task-options") ? "task-options" : "task-select";
  $("task-list").replaceChildren();
  for (const task of taskList) {
    const row = element("div", `task-item${task.id === currentTaskId ? " active" : ""}`);
    const button = element("button", "task-select");
    button.type = "button";
    button.dataset.taskId = task.id;
    button.setAttribute("aria-current", task.id === currentTaskId ? "page" : "false");
    button.title = task.title;
    button.append(element("span", "task-kind", task.mode === "approval" ? "审批" : "通用"), element("span", "task-name", task.title));
    if (task.id === currentTaskId ? busy : task.busy) button.append(element("span", "running-dot"));
    button.addEventListener("click", () => selectTask(task.id));
    const options = element("button", "task-options", "⋯");
    options.type = "button";
    options.dataset.taskId = task.id;
    options.setAttribute("aria-label", `任务操作：${task.title}`);
    options.setAttribute("aria-expanded", String(actionTaskId === task.id));
    options.setAttribute("aria-controls", "task-actions");
    options.addEventListener("click", () => openTaskMenu(task.id));
    row.append(button, options);
    $("task-list").append(row);
  }
  if (focusedId) {
    [...document.querySelectorAll(`.${focusedClass}`)]
      .find((button) => button.dataset.taskId === focusedId)?.focus();
  }
  syncTaskMenu();
}

function dialogError(id, message = "") {
  $(id).textContent = message;
  $(id).hidden = !message;
}

function renderDeletedTasks() {
  const focusedId = document.activeElement.dataset.deletedTaskId;
  const focusedClass = document.activeElement.classList.contains("purge-task") ? "purge-task" : "restore-task";
  const list = $("deleted-task-list");
  list.replaceChildren();
  if (!deletedTaskList.length) list.append(element("p", "deleted-empty", "暂无已删除任务"));
  for (const task of deletedTaskList) {
    const row = element("div", "deleted-task");
    const title = element("span", "task-name", task.title);
    title.title = task.title;
    const restore = element("button", "restore-task", "恢复");
    restore.type = "button";
    restore.dataset.deletedTaskId = task.id;
    restore.setAttribute("aria-label", `恢复任务：${task.title}`);
    restore.disabled = navigating || managing;
    restore.addEventListener("click", () => restoreTask(task.id));
    const purge = element("button", "purge-task", "彻底删除");
    purge.type = "button";
    purge.dataset.deletedTaskId = task.id;
    purge.setAttribute("aria-label", `彻底删除任务：${task.title}`);
    purge.disabled = navigating || managing;
    purge.addEventListener("click", () => confirmPurgeTask(task.id));
    const actions = element("div", "deleted-task-actions");
    actions.append(restore, purge);
    row.append(element("span", "task-kind", task.mode === "approval" ? "审批" : "通用"), title, actions);
    list.append(row);
  }
  if (focusedId && !$("purge-dialog").open) {
    const next = [...list.querySelectorAll(`.${focusedClass}`)].find((button) => button.dataset.deletedTaskId === focusedId);
    (next || $("deleted-dialog").querySelector("[data-close]")).focus();
  }
}

function updateTaskHistory(data) {
  taskList = data.tasks;
  deletedTaskList = data.deleted_tasks || [];
  const current = taskList.find((task) => task.id === currentTaskId);
  if (current) {
    $("task-title").textContent = current.title;
    document.title = `${current.title} · simAgent`;
  }
  renderTasks();
  if ($("deleted-dialog").open) renderDeletedTasks();
}

function showSession(data) {
  loaded = true;
  configured = data.configured;
  busy = data.busy;
  currentTaskId = data.task_id;
  const approval = data.mode === "approval";
  $("task-type").textContent = approval ? "审批电子流" : "通用任务";
  $("welcome-title").textContent = approval ? "描述你的审批场景" : "有什么想讨论的？";
  $("welcome-description").textContent = approval ? "输入申请条件和想比较的方案，缺少的信息可以继续补充。" : "直接输入你的问题。";
  input.placeholder = approval ? "输入审批场景或继续补充条件…" : "输入你的问题…";
  const url = new URL(window.location.href);
  url.searchParams.set("task", currentTaskId);
  window.history.replaceState(null, "", url);
  messages.replaceChildren();
  $("welcome").hidden = data.messages.length > 0;
  for (const item of data.messages) {
    const body = addMessage(item.role, item.content);
    if (item.status === "running" && !item.content) {
      body.textContent = "正在处理…";
      body.classList.add("thinking");
    }
  }
  updateTaskHistory(data);
  notice(configured ? "" : "请在项目 .env 中配置模型密钥并重启服务。");
  controls();
  scrollDown();
}

async function requestJSON(url, options = {}) {
  const response = await fetch(url, { credentials: "same-origin", ...options });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.detail === "string" ? data.detail : "请求失败，请稍后重试。");
    error.status = response.status;
    throw error;
  }
  return data;
}

function stopPolling() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
}

async function loadSession() {
  if (managing) { refreshAfterManagement = true; return; }
  stopPolling();
  const version = navigationVersion;
  const query = currentTaskId ? `?task_id=${encodeURIComponent(currentTaskId)}` : "";
  try {
    const data = await requestJSON(`/api/session${query}`);
    if (version !== navigationVersion || connection) return;
    if (data.task_id !== currentTaskId) input.value = drafts.get(data.task_id) || "";
    showSession(data);
    if (data.tasks.some((task) => task.busy)) pollTimer = setTimeout(loadSession, 1800);
  } catch (error) {
    if (version !== navigationVersion) return;
    if (error.status === 404 && currentTaskId) {
      drafts.delete(currentTaskId);
      currentTaskId = null;
      input.value = "";
      await loadSession();
      return;
    }
    notice("无法加载任务，请确认服务已启动后刷新页面。");
  }
}

function detachStream() {
  const previous = connection;
  connection = null;
  if (previous) previous.controller.abort();
}

async function manageTasks(url, method, body) {
  if (navigating || managing) return null;
  managing = true;
  navigationVersion++;
  stopPolling();
  controls();
  try {
    const options = { method };
    if (body !== undefined) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(body);
    }
    const data = await requestJSON(url, options);
    updateTaskHistory(data);
    if (!taskList.some((task) => task.id === currentTaskId)) refreshAfterManagement = true;
    return data;
  } catch (error) {
    if (error.status === 404) refreshAfterManagement = true;
    throw error;
  } finally {
    managing = false;
    navigationVersion++;
    controls();
    if (!connection && (refreshAfterManagement || taskList.some((task) => task.busy))) {
      pollTimer = setTimeout(loadSession, refreshAfterManagement ? 0 : 1800);
    }
    refreshAfterManagement = false;
  }
}

async function navigate(url, body) {
  if (navigating || managing) return;
  drafts.set(currentTaskId, input.value);
  navigating = true;
  navigationVersion++;
  stopPolling();
  detachStream();
  menu(false);
  closeTaskMenu();
  controls();
  try {
    const data = await requestJSON(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    input.value = drafts.get(data.task_id) || "";
    followOutput = true;
    showSession(data);
    if (data.tasks.some((task) => task.busy)) pollTimer = setTimeout(loadSession, 1800);
  } catch (error) {
    notice(error.message);
    await loadSession();
  } finally {
    navigating = false;
    controls();
    if (configured && !busy) input.focus();
  }
}

async function selectTask(id) {
  if (id === currentTaskId) return;
  await navigate("/api/session/task", { task_id: id });
}

$("rename-task").addEventListener("click", () => {
  const task = taskList.find((item) => item.id === actionTaskId);
  if (!task || navigating || managing) return;
  renameTaskId = task.id;
  closeTaskMenu();
  $("task-name-input").value = task.title;
  $("task-name-input").setCustomValidity("");
  dialogError("rename-error");
  $("rename-dialog").showModal();
  $("task-name-input").focus();
  $("task-name-input").select();
});

$("task-name-input").addEventListener("input", () => {
  $("task-name-input").setCustomValidity("");
  dialogError("rename-error");
});

$("rename-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!renameTaskId || navigating || managing) return;
  const title = $("task-name-input").value.trim();
  if (!title) {
    $("task-name-input").setCustomValidity("请输入任务名称。");
    $("task-name-input").reportValidity();
    return;
  }
  dialogError("rename-error");
  try {
    await manageTasks(`/api/tasks/${encodeURIComponent(renameTaskId)}`, "PATCH", { title });
    $("rename-dialog").close();
  } catch (error) {
    dialogError("rename-error", error.message);
    $("task-name-input").focus();
  }
});

$("delete-task").addEventListener("click", async () => {
  const id = actionTaskId;
  if (!id || navigating || managing) return;
  closeTaskMenu();
  try {
    const data = await manageTasks(`/api/tasks/${encodeURIComponent(id)}`, "DELETE");
    if (id === currentTaskId) await navigate("/api/session/task", { task_id: data.fallback_task_id });
    else taskMenuTrigger(currentTaskId)?.focus();
    drafts.delete(id);
    notice("任务已移入“最近删除”，可在左侧恢复。");
  } catch (error) {
    notice(error.message);
  }
});

$("recently-deleted").addEventListener("click", async () => {
  if (navigating || managing) return;
  menu(false);
  closeTaskMenu();
  dialogError("deleted-error");
  renderDeletedTasks();
  $("deleted-dialog").showModal();
  try {
    await manageTasks("/api/tasks", "GET");
    $("deleted-dialog").querySelector("[data-close]").focus();
  } catch (error) {
    dialogError("deleted-error", error.message);
  }
});

async function restoreTask(id) {
  if (navigating || managing) return;
  dialogError("deleted-error");
  try {
    await manageTasks(`/api/tasks/${encodeURIComponent(id)}/restore`, "POST");
    $("deleted-dialog").querySelector("[data-close]").focus();
  } catch (error) {
    dialogError("deleted-error", error.message);
  }
}

function confirmPurgeTask(id) {
  if (navigating || managing) return;
  const task = deletedTaskList.find((item) => item.id === id);
  if (!task) return;
  purgeTaskId = id;
  $("purge-task-title").textContent = task.title;
  dialogError("purge-error");
  $("purge-dialog").showModal();
  $("purge-dialog").querySelector("[data-close]").focus();
}

$("purge-confirm").addEventListener("click", async () => {
  if (!purgeTaskId || navigating || managing) return;
  const id = purgeTaskId;
  dialogError("purge-error");
  dialogError("deleted-error");
  $("purge-confirm").textContent = "正在删除…";
  try {
    await manageTasks(`/api/tasks/${encodeURIComponent(id)}/permanent`, "DELETE");
    drafts.delete(id);
    $("purge-dialog").close();
  } catch (error) {
    dialogError("purge-error", error.message);
  } finally {
    $("purge-confirm").textContent = "彻底删除";
  }
});

document.querySelectorAll("[data-close]").forEach((button) => {
  button.addEventListener("click", () => { if (!managing) $(button.dataset.close).close(); });
});
[$("rename-dialog"), $("deleted-dialog"), $("purge-dialog")].forEach((dialog) => {
  dialog.addEventListener("cancel", (event) => { if (managing) event.preventDefault(); });
  dialog.addEventListener("close", () => {
    if (dialog.id === "rename-dialog") {
      taskMenuTrigger(renameTaskId)?.focus();
      renameTaskId = null;
    } else if (dialog.id === "purge-dialog") {
      const trigger = [...$("deleted-task-list").querySelectorAll(".purge-task")]
        .find((button) => button.dataset.deletedTaskId === purgeTaskId);
      (trigger || $("deleted-dialog").querySelector("[data-close]")).focus();
      purgeTaskId = null;
    } else $("recently-deleted").focus();
  });
});

$("new-task").addEventListener("click", () => {
  closeTaskMenu();
  menu($("new-task-menu").hidden);
});
document.querySelectorAll("[data-create]").forEach((button) => {
  button.addEventListener("click", () => navigate("/api/tasks", { mode: button.dataset.create }));
});
document.addEventListener("click", (event) => {
  if (!$("new-task-control").contains(event.target)) menu(false);
  if (!$("task-actions").contains(event.target) && !event.target.closest(".task-options")) closeTaskMenu();
});
document.addEventListener("focusin", (event) => {
  if (!$("task-actions").contains(event.target) && !event.target.closest(".task-options")) closeTaskMenu();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") { menu(false); closeTaskMenu(true); }
});
$("task-list").addEventListener("scroll", () => closeTaskMenu());
window.addEventListener("resize", () => closeTaskMenu());
input.addEventListener("input", controls);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!busy && !navigating && !managing && input.value.trim()) $("task-form").requestSubmit();
  }
});

$("stop").addEventListener("click", async () => {
  const id = currentTaskId;
  $("stop").disabled = true;
  try {
    await requestJSON(`/api/chat/cancel?task_id=${encodeURIComponent(id)}`, { method: "POST" });
    if (id === currentTaskId && !connection) await loadSession();
  } catch (error) {
    if (id === currentTaskId) { notice(error.message); controls(); }
  }
});

$("task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = input.value.trim();
  if (!configured || busy || navigating || managing || !question) return;
  navigationVersion++;
  stopPolling();
  notice();
  const stream = { controller: new AbortController(), taskId: currentTaskId };
  connection = stream;
  busy = true;
  renderTasks();
  controls();
  followOutput = true;
  addMessage("user", question);
  input.value = "";
  drafts.delete(currentTaskId);
  const answer = addMessage("assistant");
  answer.textContent = "正在处理…";
  answer.classList.add("thinking");
  let accepted = false;
  let finished = false;
  const active = () => connection === stream && currentTaskId === stream.taskId;
  const onEvent = (item) => {
    if (!active()) return;
    if (item.type === "stage") {
      answer.textContent = ["executing", "evaluating"].includes(item.stage) ? "正在计算…" : "正在处理…";
    } else if (item.type === "done" || item.type === "stopped") {
      finished = true;
      answer.classList.remove("thinking");
      renderText(answer, item.text || "已停止。");
      scrollDown();
    } else if (item.type === "error") throw new Error(item.message);
  };
  try {
    const response = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
      body: JSON.stringify({ message: question, task_id: stream.taskId }), signal: stream.controller.signal,
    });
    if (!response.ok) {
      const error = await response.json();
      throw new Error(typeof error.detail === "string" ? error.detail : "任务提交失败。");
    }
    accepted = true;
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
    if (!finished) throw new Error("连接已中断，正在恢复任务状态。");
  } catch (error) {
    if (!active()) return;
    answer.classList.remove("thinking");
    renderText(answer, error.message);
    if (!accepted) input.value = question;
  } finally {
    if (active()) {
      connection = null;
      stream.controller.abort();
      busy = false;
      renderTasks();
      controls();
      if (accepted) await loadSession();
      if (configured && !busy && !navigating && !managing && !document.querySelector("dialog[open]")) input.focus();
    }
  }
});

loadSession();
