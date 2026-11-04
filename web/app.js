const statusLabels = {
  pending: "未着手",
  planning: "計画中",
  running: "実行中",
  paused: "一時停止",
  waiting: "待機中",
  failed: "失敗",
  completed: "完了",
};

const taskList = document.querySelector("#task-list");
const taskForm = document.querySelector("#task-form");
const statusFilter = document.querySelector("#status-filter");
const taskCount = document.querySelector("#task-count");
const formError = document.querySelector("#form-error");
const memoryForm = document.querySelector("#memory-form");
const memoryList = document.querySelector("#memory-list");
const memoryCount = document.querySelector("#memory-count");
const memorySearch = document.querySelector("#memory-search");
const memoryFilter = document.querySelector("#memory-filter");
const memoryError = document.querySelector("#memory-error");
const memoryCancel = document.querySelector("#memory-cancel");
const chatForm = document.querySelector("#chat-form");
const chatMessages = document.querySelector("#chat-messages");
const chatInput = document.querySelector("#chat-input");
const fileForm = document.querySelector("#file-form");
const fileList = document.querySelector("#file-list");
const fileSearch = document.querySelector("#file-search");
const fileImport = document.querySelector("#file-import");
const documentPreview = document.querySelector("#document-preview");
const fileError = document.querySelector("#file-error");
const loginOverlay = document.querySelector("#login-overlay");
const loginForm = document.querySelector("#login-form");
const loginError = document.querySelector("#login-error");
const logoutButton = document.querySelector("#logout-button");
const backupRestore = document.querySelector("#backup-restore");
const toast = document.querySelector("#toast");
let toastTimer;
let memorySearchTimer;
let fileSearchTimer;
let editingMemoryId = null;
let currentFolder = "";
let appReady = false;
const selectedChatFiles = new Set();
const memoryCategories = {
  short_term: "短期記憶",
  long_term: "長期記憶",
  working: "作業記憶",
  project: "プロジェクト",
  knowledge: "知識ベース",
};

document.querySelector("#today").textContent = new Intl.DateTimeFormat("ja-JP", {
  year: "numeric",
  month: "long",
  day: "numeric",
  weekday: "short",
}).format(new Date());

logoutButton.addEventListener("click", async () => {
  try {
    await request("/api/auth/logout", { method: "POST", body: JSON.stringify({}) });
    appReady = false;
    loginOverlay.hidden = false;
    logoutButton.hidden = true;
    showToast("ログアウトしました");
  } catch (error) {
    showToast(error.message);
  }
});

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = loginForm.querySelector("button[type='submit']");
  button.disabled = true;
  loginError.hidden = true;
  try {
    await request("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ password: document.querySelector("#login-password").value }),
    });
    document.querySelector("#login-password").value = "";
    loginOverlay.hidden = true;
    logoutButton.hidden = false;
    startApp();
  } catch (error) {
    loginError.textContent = error.message;
    loginError.hidden = false;
  } finally {
    button.disabled = false;
  }
});

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("is-visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 2800);
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const payload = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/auth/login") {
      appReady = false;
      loginOverlay.hidden = false;
      logoutButton.hidden = true;
    }
    throw new Error(payload.error || "リクエストに失敗しました");
  }
  return payload;
}

function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function priorityElement(priority) {
  const wrap = makeElement("span", "priority-mark");
  wrap.setAttribute("aria-label", `優先度 ${priority} / 5`);
  for (let index = 1; index <= 5; index += 1) {
    const bar = document.createElement("i");
    if (index <= priority) bar.classList.add("on");
    wrap.append(bar);
  }
  return wrap;
}

function formatTime(value) {
  return new Intl.DateTimeFormat("ja-JP", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    .format(new Date(value));
}

function createTaskCard(task, index) {
  const card = makeElement("article", "task-card");
  card.dataset.status = task.status;
  card.style.animationDelay = `${Math.min(index, 6) * 35}ms`;
  const main = makeElement("div", "task-main");
  const titleRow = makeElement("div", "task-title-row");
  titleRow.append(makeElement("h3", "task-title", task.title));
  const status = makeElement("span", "status-badge", statusLabels[task.status] || task.status);
  status.dataset.status = task.status;
  titleRow.append(status);
  main.append(titleRow, makeElement("p", "task-goal-text", task.goal));

  const metadata = makeElement("div", "task-meta");
  const priority = makeElement("span", "");
  priority.append(priorityElement(task.priority), document.createTextNode("優先度"));
  metadata.append(priority);
  const updated = makeElement("span", "", `更新 ${formatTime(task.updated_at)}`);
  metadata.append(updated);
  if (task.next_action) metadata.append(makeElement("span", "", task.next_action));
  main.append(metadata);

  if (task.plan.length || task.findings.length || task.completion || task.error_history.length) {
    const details = makeElement("details", "task-details");
    details.append(makeElement("summary", "", `作業記録を確認 · ${task.findings.length} 件の情報`));
    if (task.completion) details.append(makeElement("p", "completion-text", task.completion));
    if (task.plan.length) {
      details.append(makeElement("h4", "detail-label", "計画"));
      const plan = document.createElement("ol");
      task.plan.forEach((step) => plan.append(makeElement("li", "", step)));
      details.append(plan);
    }
    if (task.findings.length) {
      details.append(makeElement("h4", "detail-label", "調査記録・出典"));
      const findings = makeElement("div", "finding-list");
      task.findings.forEach((finding) => {
        const item = makeElement("div", "finding-item");
        if (finding.type === "search") {
          item.append(makeElement("strong", "", `検索: ${finding.query}`));
          if (finding.results?.length) {
            const results = document.createElement("ul");
            finding.results.forEach((result) => {
              const row = document.createElement("li");
              row.append(makeSourceLink(result.url, result.title));
              results.append(row);
            });
            item.append(results);
          } else {
            item.append(makeElement("p", "finding-excerpt", "検索結果は取得できませんでした。"));
          }
        } else if (finding.type === "page") {
          if (finding.error) {
            item.append(makeElement("strong", "", `閲覧できませんでした: ${finding.url}`));
            item.append(makeElement("p", "finding-excerpt", finding.error));
          } else {
            item.append(makeSourceLink(finding.url, `${finding.title} · ${formatTime(finding.fetched_at)}`));
            item.append(makeElement("p", "finding-excerpt", `${finding.text.slice(0, 420)}${finding.text.length > 420 ? "…" : ""}`));
          }
        }
        findings.append(item);
      });
      details.append(findings);
    }
    if (task.error_history.length) {
      details.append(makeElement("h4", "detail-label", "エラー履歴"));
      const errors = document.createElement("ul");
      task.error_history.forEach((message) => errors.append(makeElement("li", "error-entry", message)));
      details.append(errors);
    }
    main.append(details);
  }

  card.append(main);
  if (["pending", "planning", "running", "waiting", "paused", "failed"].includes(task.status)) {
    const action = makeElement("button", "task-action", task.status === "paused" || task.status === "waiting" || task.status === "failed" ? "再開" : "一時停止");
    action.type = "button";
    action.addEventListener("click", async () => {
      action.disabled = true;
      try {
        const actionName = task.status === "paused" || task.status === "waiting" || task.status === "failed" ? "resume" : "pause";
        await request(`/api/tasks/${encodeURIComponent(task.id)}/action`, {
          method: "POST",
          body: JSON.stringify({ action: actionName }),
        });
        showToast(actionName === "resume" ? "タスクを再開待ちにしました" : "タスクを一時停止しました");
        await loadTasks();
      } catch (error) {
        showToast(error.message);
        action.disabled = false;
      }
    });
    card.append(action);
  }
  return card;
}

function makeSourceLink(url, label) {
  const link = document.createElement("a");
  link.textContent = label;
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "https:") {
      link.href = parsed.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    } else {
      link.textContent += ` (${url})`;
    }
  } catch {
    link.textContent += ` (${url})`;
  }
  return link;
}

function renderEmpty(filtered) {
  const empty = makeElement("div", "empty-state");
  empty.append(makeElement("span", "empty-mark", filtered ? "⌕" : "＋"));
  empty.append(makeElement("strong", "", filtered ? "該当するタスクはありません" : "まだタスクがありません"));
  empty.append(makeElement("p", "", filtered ? "状態を変えてもう一度確認してください。" : "上のフォームから、最初の目標を保存できます。"));
  return empty;
}

async function loadTasks() {
  const selected = statusFilter.value;
  const query = selected === "all" ? "" : `?status=${encodeURIComponent(selected)}`;
  const payload = await request(`/api/tasks${query}`);
  taskList.replaceChildren();
  taskCount.textContent = String(payload.tasks.length);
  if (!payload.tasks.length) {
    taskList.append(renderEmpty(selected !== "all"));
    return;
  }
  payload.tasks.forEach((task, index) => taskList.append(createTaskCard(task, index)));
}

function resetMemoryForm() {
  editingMemoryId = null;
  memoryForm.reset();
  document.querySelector("#memory-form-title").textContent = "記憶を追加";
  memoryCancel.hidden = true;
  memoryError.hidden = true;
}

function createMemoryCard(memory) {
  const card = makeElement("article", "memory-card");
  const heading = makeElement("div", "memory-card-heading");
  const title = makeElement("h3", "task-title", memory.title);
  const category = makeElement("span", "memory-category", memoryCategories[memory.category] || memory.category);
  heading.append(title, category);
  const content = makeElement("p", "memory-content", memory.content);
  const metadata = makeElement("div", "memory-meta", `更新 ${formatTime(memory.updated_at)}`);
  const actions = makeElement("div", "memory-actions");
  const edit = makeElement("button", "task-action", "編集");
  edit.type = "button";
  edit.addEventListener("click", () => {
    editingMemoryId = memory.id;
    document.querySelector("#memory-category").value = memory.category;
    document.querySelector("#memory-title").value = memory.title;
    document.querySelector("#memory-content").value = memory.content;
    document.querySelector("#memory-form-title").textContent = "記憶を編集";
    memoryCancel.hidden = false;
    memoryError.hidden = true;
    memoryForm.scrollIntoView({ behavior: "smooth", block: "center" });
    document.querySelector("#memory-title").focus();
  });
  const remove = makeElement("button", "task-action memory-delete", "削除");
  remove.type = "button";
  remove.addEventListener("click", async () => {
    if (!window.confirm(`「${memory.title}」を削除しますか？この操作は取り消せません。`)) return;
    remove.disabled = true;
    try {
      await request(`/api/memories/${encodeURIComponent(memory.id)}`, {
        method: "DELETE",
        body: JSON.stringify({ confirm: true }),
      });
      if (editingMemoryId === memory.id) resetMemoryForm();
      await loadMemories();
      showToast("記憶を削除しました");
    } catch (error) {
      showToast(error.message);
      remove.disabled = false;
    }
  });
  actions.append(edit, remove);
  card.append(heading, content, metadata, actions);
  return card;
}

async function loadMemories() {
  const parameters = new URLSearchParams();
  const category = memoryFilter.value;
  const query = memorySearch.value.trim();
  if (category !== "all") parameters.set("category", category);
  if (query) parameters.set("q", query);
  const suffix = parameters.size ? `?${parameters.toString()}` : "";
  const payload = await request(`/api/memories${suffix}`);
  memoryList.replaceChildren();
  memoryCount.textContent = String(payload.memories.length);
  if (!payload.memories.length) {
    const empty = makeElement("div", "empty-state");
    empty.append(makeElement("span", "empty-mark", "▧"));
    empty.append(makeElement("strong", "", query || category !== "all" ? "該当する記憶はありません" : "記憶はまだありません"));
    empty.append(makeElement("p", "", "上のフォームから記憶を追加できます。"));
    memoryList.append(empty);
    return;
  }
  payload.memories.forEach((memory) => memoryList.append(createMemoryCard(memory)));
}

function renderChatMessage(message) {
  const article = makeElement("article", `chat-message chat-${message.role}`);
  article.append(
    makeElement("strong", "chat-role", message.role === "user" ? "あなた" : "Mars"),
    makeElement("p", "chat-content", message.content),
  );
  return article;
}

async function loadChat() {
  const payload = await request("/api/chat");
  chatMessages.replaceChildren();
  if (!payload.messages.length) {
    chatMessages.append(makeElement("p", "chat-empty", "目標を伝えると、Marsが回答します。"));
    return;
  }
  payload.messages.forEach((message) => chatMessages.append(renderChatMessage(message)));
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

function openFileEditor(path, content = "") {
  document.querySelector("#file-path").value = path;
  document.querySelector("#file-content").value = content;
  document.querySelector("#file-form-title").textContent = content ? "ファイルを編集" : "ファイルを作成";
  fileError.hidden = true;
  fileForm.scrollIntoView({ behavior: "smooth", block: "center" });
  document.querySelector("#file-content").focus();
}

function renderFileEntry(entry) {
  const row = makeElement("div", "file-entry");
  const name = makeElement("span", "file-name", `${entry.type === "folder" ? "▰" : "▤"} ${entry.name}`);
  const meta = makeElement("span", "file-size", entry.type === "folder" ? "フォルダー" : `${entry.size} bytes`);
  const actions = makeElement("div", "file-actions");
  if (entry.type === "folder") {
    const open = makeElement("button", "task-action", "開く");
    open.type = "button";
    open.addEventListener("click", () => {
      currentFolder = entry.path;
      fileSearch.value = "";
      loadFiles().catch((error) => showToast(error.message));
    });
    actions.append(open);
  } else if (entry.readable) {
    const open = makeElement("button", "task-action", entry.supported ? "開く・編集" : "内容を表示");
    open.type = "button";
    open.addEventListener("click", async () => {
      try {
        const query = new URLSearchParams({ path: entry.path });
        const result = await request(`/api/files/content?${query}`);
        if (entry.supported) openFileEditor(result.path, result.content);
        else {
          document.querySelector("#document-preview-title").textContent = entry.name;
          document.querySelector("#document-preview-content").textContent = result.content;
          documentPreview.showModal();
        }
      } catch (error) {
        showToast(error.message);
      }
    });
    const download = makeElement("a", "task-action file-download", "ダウンロード");
    download.href = `/api/files/download?${new URLSearchParams({ path: entry.path })}`;
    const attach = makeElement("button", "task-action", selectedChatFiles.has(entry.path) ? "添付中" : "チャットに添付");
    attach.type = "button";
    attach.addEventListener("click", () => {
      if (selectedChatFiles.has(entry.path)) selectedChatFiles.delete(entry.path);
      else if (selectedChatFiles.size >= 3) {
        showToast("一度に添付できるファイルは3件までです");
        return;
      } else selectedChatFiles.add(entry.path);
      renderChatAttachments();
      loadFiles().catch((error) => showToast(error.message));
    });
    actions.append(open, download, attach);
  }
  document.querySelector("#document-preview-close").addEventListener("click", () => documentPreview.close());
  documentPreview.addEventListener("click", (event) => {
    if (event.target === documentPreview) documentPreview.close();
  });
  const remove = makeElement("button", "task-action memory-delete", "削除");
  remove.type = "button";
  remove.addEventListener("click", async () => {
    if (!window.confirm(`「${entry.path}」を削除しますか？この操作は取り消せません。`)) return;
    remove.disabled = true;
    try {
      await request(`/api/files/${entry.path.split("/").map(encodeURIComponent).join("/")}`, {
        method: "DELETE",
        body: JSON.stringify({ confirm: true }),
      });
      await loadFiles();
      showToast("ファイルを削除しました");
    } catch (error) {
      showToast(error.message);
      remove.disabled = false;
    }
  });
  actions.append(remove);
  row.append(name, meta, actions);
  return row;
}

function renderFileBreadcrumb() {
  const breadcrumb = document.querySelector("#file-breadcrumb");
  breadcrumb.replaceChildren();
  const root = makeElement("button", "file-breadcrumb-link", "ホーム");
  root.type = "button";
  root.addEventListener("click", () => {
    currentFolder = "";
    loadFiles().catch((error) => showToast(error.message));
  });
  breadcrumb.append(root);
  const parts = currentFolder ? currentFolder.split("/") : [];
  parts.forEach((part, index) => {
    breadcrumb.append(document.createTextNode(" / "));
    const link = makeElement("button", "file-breadcrumb-link", part);
    link.type = "button";
    link.addEventListener("click", () => {
      currentFolder = parts.slice(0, index + 1).join("/");
      loadFiles().catch((error) => showToast(error.message));
    });
    breadcrumb.append(link);
  });
}

async function loadFiles() {
  renderFileBreadcrumb();
  const query = fileSearch.value.trim();
  const parameters = new URLSearchParams();
  if (query) parameters.set("q", query);
  else if (currentFolder) parameters.set("path", currentFolder);
  const endpoint = query ? "/api/files/search" : "/api/files";
  const result = await request(`${endpoint}${parameters.size ? `?${parameters}` : ""}`);
  const entries = query ? result.files : result.entries;
  fileList.replaceChildren();
  if (!entries.length) {
    fileList.append(makeElement("p", "file-empty", query ? "一致するファイルはありません。" : "このフォルダーは空です。"));
    return;
  }
  entries.forEach((entry) => fileList.append(renderFileEntry(entry)));
}

function renderChatAttachments() {
  const container = document.querySelector("#chat-attachments");
  container.replaceChildren();
  for (const path of selectedChatFiles) {
    const chip = makeElement("span", "chat-attachment", path);
    const remove = makeElement("button", "", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `${path} を添付から外す`);
    remove.addEventListener("click", () => {
      selectedChatFiles.delete(path);
      renderChatAttachments();
      loadFiles().catch((error) => showToast(error.message));
    });
    chip.append(remove);
    container.append(chip);
  }
}

async function refreshHealth() {
  const health = await request("/api/health");
  const engineLabel = document.querySelector(".engine-label");
  const dot = engineLabel.querySelector(".engine-dot");
  const active = health.engine === "active";
  document.querySelector("#settings-model").textContent = active
    ? `${health.model_remote ? "外部" : "ローカル"}モデル「${health.model}」を設定中です。APIキーを画面やデータベースには保存しません。`
    : "モデルは未設定です。環境変数で設定します。APIキーを画面やデータベースには保存しません。";
  document.querySelector(".chat-note").textContent = health.model_remote
    ? "会話・添付ファイル（文書の抽出テキストを含む）・関連記憶は外部モデルへ送信されます。機密情報を入力・添付しないでください。"
    : active
      ? "会話・添付ファイル（文書の抽出テキストを含む）・関連記憶をローカルモデルに渡します。内容は外部モデルへ送信されません。"
      : "モデル未設定時は会話を送信できません。ファイル添付内容はモデル設定後に送信されます。";
  document.querySelector("#chat-model").textContent = active
    ? `${health.model_remote ? "外部" : "ローカル"}モデル: ${health.model}`
    : "モデル未接続";
  engineLabel.replaceChildren(dot, document.createTextNode(active ? `エージェント準備済み · ${health.model}` : "モデル未接続"));
  document.querySelector(".notice p").textContent = active
    ? health.model_remote
      ? `外部モデル ${health.model} へ依頼内容と調査資料が送信されます。タスクは優先度順に実行されます。`
      : `ローカルモデル ${health.model} で計画・Web検索・公開HTTPSページの調査を実行します。検索語は検索事業者へ送信されます。`
    : `タスクは保存できますが自動実行されません。OllamaなどOpenAI互換モデルを設定して再起動してください。検索語は検索事業者へ送信されます。`;
  dot.classList.toggle("is-running", active);
}

function startApp() {
  appReady = true;
  loadTasks().catch((error) => showToast(`データを読み込めません: ${error.message}`));
  loadMemories().catch((error) => showToast(`記憶を読み込めません: ${error.message}`));
  loadChat().catch((error) => showToast(`会話を読み込めません: ${error.message}`));
  loadFiles().catch((error) => showToast(`ファイルを読み込めません: ${error.message}`));
  refreshHealth().catch((error) => showToast(`実行状態を読み込めません: ${error.message}`));
}

taskForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  formError.hidden = true;
  const submitButton = taskForm.querySelector("button[type='submit']");
  submitButton.disabled = true;
  const formData = new FormData(taskForm);
  try {
    await request("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        title: formData.get("title"),
        goal: formData.get("goal"),
        priority: Number(formData.get("priority")),
      }),
    });
    taskForm.reset();
    document.querySelector("#task-priority").value = "3";
    statusFilter.value = "all";
    await loadTasks();
    showToast("タスクをこの端末に保存しました");
  } catch (error) {
    formError.textContent = error.message;
    formError.hidden = false;
  } finally {
    submitButton.disabled = false;
  }
});

statusFilter.addEventListener("change", () => loadTasks().catch((error) => showToast(error.message)));
chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = chatForm.querySelector("button[type='submit']");
  const message = chatInput.value.trim();
  if (!message) return;
  button.disabled = true;
  chatInput.disabled = true;
  chatMessages.append(renderChatMessage({ role: "user", content: message }));
  chatMessages.append(makeElement("p", "chat-thinking", "Marsが考えています…"));
  chatMessages.scrollTop = chatMessages.scrollHeight;
  try {
    const result = await request("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message, files: [...selectedChatFiles] }),
    });
    chatMessages.append(renderChatMessage(result.message));
    chatInput.value = "";
    selectedChatFiles.clear();
    renderChatAttachments();
    loadFiles().catch((error) => showToast(error.message));
  } catch (error) {
    try {
      await loadChat();
    } catch (historyError) {
      showToast(`回答に失敗しました: ${error.message} / 会話履歴: ${historyError.message}`);
      return;
    }
    showToast(error.message);
  } finally {
    button.disabled = false;
    chatInput.disabled = false;
    chatInput.focus();
  }
});
fileForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  fileError.hidden = true;
  const button = fileForm.querySelector("button[type='submit']");
  button.disabled = true;
  try {
    const result = await request("/api/files", {
      method: "POST",
      body: JSON.stringify({
        action: "save",
        path: document.querySelector("#file-path").value,
        content: document.querySelector("#file-content").value,
      }),
    });
    currentFolder = result.file.path.split("/").slice(0, -1).join("/");
    fileSearch.value = "";
    await loadFiles();
    showToast("ファイルを保存しました");
  } catch (error) {
    fileError.textContent = error.message;
    fileError.hidden = false;
  } finally {
    button.disabled = false;
  }
});
document.querySelector("#folder-create").addEventListener("click", async () => {
  const name = window.prompt("作成するフォルダー名（現在の場所からの相対パス）");
  if (name === null) return;
  const path = [currentFolder, name.trim()].filter(Boolean).join("/");
  try {
    await request("/api/files", { method: "POST", body: JSON.stringify({ action: "mkdir", path }) });
    await loadFiles();
    showToast("フォルダーを作成しました");
  } catch (error) {
    showToast(error.message);
  }
});
fileSearch.addEventListener("input", () => {
  window.clearTimeout(fileSearchTimer);
  fileSearchTimer = window.setTimeout(() => loadFiles().catch((error) => showToast(error.message)), 250);
});
fileImport.addEventListener("change", async () => {
  const files = [...(fileImport.files || [])];
  if (!files.length) return;
  const textExtensions = new Set([".txt", ".md", ".csv", ".json", ".log"]);
  const documentExtensions = new Set([".pdf", ".docx", ".xlsx"]);
  const imported = [];
  const failures = [];
  for (const file of files) {
    const dot = file.name.lastIndexOf(".");
    const extension = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
    const isText = textExtensions.has(extension);
    if (!isText && !documentExtensions.has(extension)) {
      failures.push(`${file.name}: 対応していない形式です`);
      continue;
    }
    const limit = isText ? 1_000_000 : 20 * 1024 * 1024;
    if (file.size > limit) {
      failures.push(`${file.name}: ${isText ? "1" : "20"} MBを超えています`);
      continue;
    }
    try {
      const content = isText
        ? { content: await file.text() }
        : { content_base64: await fileToBase64(file) };
      const path = [currentFolder, file.name].filter(Boolean).join("/");
      const result = await request("/api/files", {
        method: "POST",
        body: JSON.stringify({ action: "import", path, ...content }),
      });
      imported.push(result.file.path);
    } catch (error) {
      failures.push(`${file.name}: ${error.message}`);
    }
  }
  fileImport.value = "";
  fileSearch.value = "";
  try {
    await loadFiles();
  } catch (error) {
    failures.push(`一覧の更新に失敗しました: ${error.message}`);
  }
  if (failures.length) {
    showToast(`${imported.length}件取り込み、${failures.length}件失敗: ${failures.join(" / ")}`);
  } else {
    showToast(`${imported.length}件を取り込みました`);
  }
});

async function fileToBase64(file) {
  const buffer = new Uint8Array(await file.arrayBuffer());
  const chunks = [];
  for (let offset = 0; offset < buffer.length; offset += 0x8000) {
    chunks.push(String.fromCharCode(...buffer.subarray(offset, offset + 0x8000)));
  }
  return btoa(chunks.join(""));
}

backupRestore.addEventListener("change", async () => {
  const file = backupRestore.files?.[0];
  if (!file) return;
  const isFullBackup = file.name.toLowerCase().endsWith(".zip");
  const maxBytes = isFullBackup ? 250 * 1024 * 1024 : 50 * 1024 * 1024;
  if (file.size > maxBytes) {
    showToast(`バックアップは${isFullBackup ? "250" : "50"} MB以内にしてください`);
    backupRestore.value = "";
    return;
  }
  const approved = window.confirm(
    isFullBackup
      ? `データベースとファイル領域を「${file.name}」の内容で置き換えます。復元前の完全バックアップを自動退避します。未完了タスクがあると復元できません。続けますか？`
      : `データベースを「${file.name}」の内容で置き換えます。復元前のDBは自動退避されます。ファイル領域は変更されません。未完了タスクがあると復元できません。続けますか？`,
  );
  if (!approved) {
    backupRestore.value = "";
    return;
  }
  try {
    const response = await fetch("/api/backup/restore", {
      method: "POST",
      headers: {
        "Content-Type": isFullBackup ? "application/zip" : "application/vnd.sqlite3",
        "X-Mars-Confirm": "replace-database",
      },
      body: file,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "バックアップを復元できません");
    showToast(`復元しました。復元前のDB: ${result.safety_backup}`);
    window.setTimeout(() => window.location.reload(), 800);
  } catch (error) {
    showToast(error.message);
  } finally {
    backupRestore.value = "";
  }
});
memoryForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  memoryError.hidden = true;
  const submitButton = memoryForm.querySelector("button[type='submit']");
  submitButton.disabled = true;
  const isEditing = Boolean(editingMemoryId);
  const payload = {
    category: document.querySelector("#memory-category").value,
    title: document.querySelector("#memory-title").value,
    content: document.querySelector("#memory-content").value,
  };
  try {
    await request(editingMemoryId ? `/api/memories/${encodeURIComponent(editingMemoryId)}` : "/api/memories", {
      method: editingMemoryId ? "PATCH" : "POST",
      body: JSON.stringify(payload),
    });
    resetMemoryForm();
    await loadMemories();
    showToast(isEditing ? "記憶を更新しました" : "記憶を保存しました");
  } catch (error) {
    memoryError.textContent = error.message;
    memoryError.hidden = false;
  } finally {
    submitButton.disabled = false;
  }
});
memoryCancel.addEventListener("click", resetMemoryForm);
memoryFilter.addEventListener("change", () => loadMemories().catch((error) => showToast(error.message)));
memorySearch.addEventListener("input", () => {
  window.clearTimeout(memorySearchTimer);
  memorySearchTimer = window.setTimeout(() => loadMemories().catch((error) => showToast(error.message)), 250);
});
request("/api/auth").then((auth) => {
  document.querySelector("#auth-status").textContent = auth.auth_required
    ? "ログイン保護が有効です。セッションは12時間で期限切れになります。"
    : "ログイン保護は無効です。アクセスはローカル接続に限定されています。";
  if (auth.auth_required && !auth.authenticated) {
    loginOverlay.hidden = false;
    return;
  }
  logoutButton.hidden = !auth.auth_required;
  startApp();
}).catch((error) => showToast(`認証状態を確認できません: ${error.message}`));
window.setInterval(() => {
  if (!appReady) return;
  loadTasks().catch((error) => showToast(error.message));
  loadMemories().catch((error) => showToast(error.message));
  refreshHealth().catch((error) => showToast(error.message));
}, 5000);