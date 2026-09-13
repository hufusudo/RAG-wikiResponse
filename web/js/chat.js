/* 问答区：会话侧边栏 + SSE 流式渲染（Test 8：持续接收 token，实时更新 UI）
 *
 * 会话（v2.3）：每仓库持久化于 wiki/.llmwiki/chats/，可切换 / 删除；
 * 继续会话时后端自动携带最近几轮历史，并附加当前仓库全局「回答风格 Prompt」。
 *
 * 展示策略（用户约定）：
 *   显示 = 溯源到的 raw 笔记（来源 chips）+ 回答正文 + 追问
 *   隐藏 = 寻址轨迹条、LLM 的「笔记原文溯源」「覆盖声明」章节、飞轮过程文案
 *   （五段式仍由模型完整生成，只是前端过滤展示；轨迹/覆盖信息进后台日志）
 */
window.Chat = (function () {
  const $ = (id) => document.getElementById(id);
  const messagesEl = () => $("chat-messages");
  const EMPTY_HTML =
    '<div class="chat-empty"><p>向你的知识库提问。</p>' +
    '<p class="muted">回答将展示寻址轨迹与 Raw 原文来源。</p></div>';

  let busy = false;
  let currentSessionId = null; // null = 新会话（首轮发送时由后端创建）
  let sessions = [];

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  /* ---------------- 会话侧边栏 ---------------- */

  function clearMessages() { messagesEl().innerHTML = EMPTY_HTML; }

  function fmtTime(ts) {
    const ms = (ts || 0) * 1000;
    if (!ms) return "";
    const diff = Date.now() - ms;
    if (diff < 60e3) return "刚刚";
    if (diff < 3600e3) return Math.floor(diff / 60e3) + " 分钟前";
    if (diff < 86400e3) return Math.floor(diff / 3600e3) + " 小时前";
    const d = new Date(ms);
    return (d.getMonth() + 1) + "-" + String(d.getDate()).padStart(2, "0");
  }

  async function loadSessions() {
    try {
      const data = await App.api("/api/chat/sessions");
      sessions = data.sessions || [];
      if (currentSessionId && !sessions.some(s => s.id === currentSessionId)) {
        currentSessionId = null; // 会话已被删除（可能来自其它标签页）
        clearMessages();
      }
      renderSessionList();
    } catch (e) {
      $("session-list").innerHTML =
        '<p class="muted small pad">加载失败：' + esc(e.message) + "</p>";
    }
  }

  function renderSessionList() {
    const el = $("session-list");
    if (!sessions.length) {
      el.innerHTML = '<p class="muted small pad">暂无历史会话</p>';
      return;
    }
    el.innerHTML = sessions.map(s =>
      '<div class="session-item' + (s.id === currentSessionId ? " active" : "") +
        '" data-id="' + esc(s.id) + '">' +
        '<div class="s-title">' + esc(s.title || "新会话") + "</div>" +
        '<div class="s-time">' + esc(fmtTime(s.updated_at)) + " · " + s.message_count + " 条</div>" +
        '<button type="button" class="session-del" data-del="' + esc(s.id) +
          '" title="删除会话">✕</button>' +
      "</div>"
    ).join("");
  }

  function newSession() {
    if (busy) { App.toast("正在回答中，请稍候…"); return; }
    currentSessionId = null;
    clearMessages();
    renderSessionList();
    $("chat-input").focus();
  }

  async function openSession(id) {
    if (busy) { App.toast("正在回答中，请稍候…"); return; }
    if (id === currentSessionId) return;
    try {
      const data = await App.api("/api/chat/sessions/" + encodeURIComponent(id));
      currentSessionId = id;
      renderSession(data.session.messages || []);
      renderSessionList();
    } catch (e) { App.toast(e.message, "err"); }
  }

  function renderSession(msgs) {
    messagesEl().innerHTML = "";
    for (const m of msgs) {
      if (m.role === "user") {
        const user = document.createElement("div");
        user.className = "msg-user";
        user.textContent = m.content || "";
        messagesEl().appendChild(user);
      } else if (m.role === "assistant") {
        const ai = document.createElement("div");
        ai.className = "msg msg-ai";
        ai.innerHTML = '<div class="md"></div><div class="msg-meta"></div>';
        const content = (m.content || "") + (m.error ? "\n\n> ⚠️ " + m.error : "");
        ai.querySelector(".md").innerHTML = content.trim()
          ? renderAnswer(content) : '<span class="muted">（空回答）</span>';
        renderSources(ai.querySelector(".msg-meta"), { sources: m.sources || [] });
        messagesEl().appendChild(ai);
      }
    }
    messagesEl().scrollTop = messagesEl().scrollHeight;
  }

  async function deleteSession(id) {
    if (busy) { App.toast("正在回答中，请稍候…"); return; }
    if (!confirm("确定删除该会话？\n\n仅删除聊天记录，不影响笔记与 Wiki 卡片。")) return;
    try {
      await App.api("/api/chat/sessions/" + encodeURIComponent(id), { method: "DELETE" });
      if (currentSessionId === id) { currentSessionId = null; clearMessages(); }
      await loadSessions();
      App.toast("会话已删除", "ok");
    } catch (e) { App.toast(e.message, "err"); }
  }

  /* ---------------- 回答风格 Prompt（当前仓库全局） ---------------- */

  async function openPromptModal() {
    try {
      const data = await App.api("/api/prompt");
      $("prompt-editor").value = data.prompt || "";
      $("prompt-status").textContent = data.prompt
        ? "当前仓库已启用风格 Prompt" : "当前使用默认风格";
      $("modal-prompt").classList.remove("hidden");
    } catch (e) { App.toast(e.message, "err"); }
  }

  async function savePrompt() {
    try {
      const data = await App.post("/api/prompt", { content: $("prompt-editor").value });
      $("modal-prompt").classList.add("hidden");
      App.toast(
        data.prompt ? "回答风格已保存（当前仓库全局生效）" : "已清空，恢复默认风格", "ok");
    } catch (e) { $("prompt-status").textContent = "❌ " + e.message; }
  }

  /* ---------------- 五段式 → 三块展示（正文 / 来源 / 追问） ---------------- */

  const BODY_TITLES = new Set(["核心概念", "深度剖析"]);
  const HIDDEN_TITLES = new Set(["笔记原文溯源", "覆盖声明"]);
  const FOLLOWUP_TITLE = "追问引导";

  function splitSections(md) {
    const sections = [];
    let cur = { title: null, lines: [] };
    for (const line of md.split("\n")) {
      const m = line.match(/^##\s+(.+?)\s*$/);
      if (m) {
        sections.push(cur);
        cur = { title: m[1].replace(/[：:\s]/g, ""), lines: [] };
      } else {
        cur.lines.push(line);
      }
    }
    sections.push(cur);
    return sections;
  }

  function renderAnswer(md) {
    const sections = splitSections(md);
    let body = "";
    let followup = "";
    for (const s of sections) {
      const content = s.lines.join("\n").trim();
      if (!content) continue;
      if (s.title === null || BODY_TITLES.has(s.title)) {
        body += content + "\n\n";            // 正文（不带章节头）
      } else if (s.title === FOLLOWUP_TITLE) {
        followup = content;                   // 追问
      }
      // 其余（笔记原文溯源/覆盖声明/未知标题）→ 隐藏
    }
    let html = App.renderMarkdown(body.trim()) || '<span class="loading-dots"> </span>';
    if (followup) {
      html += '<div class="followup"><div class="followup-title">追问</div>'
            + App.renderMarkdown(followup) + "</div>";
    }
    return html;
  }

  /* ---------------- 发送 / SSE ---------------- */

  async function send(question) {
    if (busy || !question.trim()) return;
    busy = true;
    document.getElementById("btn-send").disabled = true;

    const empty = messagesEl().querySelector(".chat-empty");
    if (empty) empty.remove();

    const user = document.createElement("div");
    user.className = "msg-user";
    user.textContent = question;
    messagesEl().appendChild(user);

    const ai = document.createElement("div");
    ai.className = "msg msg-ai";
    ai.innerHTML = '<div class="md"><span class="loading-dots">思考中</span></div>'
                 + '<div class="msg-meta"></div>';
    messagesEl().appendChild(ai);
    const bodyEl = ai.querySelector(".md");
    const metaEl = ai.querySelector(".msg-meta");
    messagesEl().scrollTop = messagesEl().scrollHeight;

    let text = "";
    let mdTimer = null;
    const flush = () => { bodyEl.innerHTML = renderAnswer(text); };

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: question, session_id: currentSessionId }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || err.error || ("HTTP " + resp.status));
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      // 解析 SSE：event:/data: 帧
      const handleFrame = (frame) => {
        let ev = "message", data = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) ev = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!data) return;
        let payload;
        try { payload = JSON.parse(data); } catch { return; }

        if (ev === "route") {
          // 新会话：后端创建后返回 id → 侧边栏立即出现
          if (payload.session_id && payload.session_id !== currentSessionId) {
            currentSessionId = payload.session_id;
            loadSessions();
          }
        } else if (ev === "token") {
          text += payload.delta;
          if (!mdTimer) mdTimer = setTimeout(() => { mdTimer = null; flush(); }, 60);
          messagesEl().scrollTop = messagesEl().scrollHeight;
        } else if (ev === "error") {
          text += "\n\n> ⚠️ " + payload.error;
          flush();
        } else if (ev === "done") {
          renderSources(metaEl, payload);
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          handleFrame(buf.slice(0, idx));
          buf = buf.slice(idx + 2);
        }
      }
      if (mdTimer) { clearTimeout(mdTimer); mdTimer = null; }
      flush();
      if (!text) bodyEl.innerHTML = '<span class="muted">（空回答）</span>';
    } catch (e) {
      bodyEl.innerHTML = '<span class="error-text">⚠️ ' + esc(e.message) + "</span>";
      if (String(e.message).includes("LLM_API_KEY")) App.openSettings();
    } finally {
      busy = false;
      document.getElementById("btn-send").disabled = false;
      messagesEl().scrollTop = messagesEl().scrollHeight;
      loadSessions(); // 更新标题 / 时间 / 条数 / 排序
    }
  }

  function renderSources(metaEl, payload) {
    // 只展示溯源到的 raw 笔记；兑底时标注“标题匹配”，避免误当精准溯源
    const raws = (payload.sources || []).filter(s => s.type === "raw");
    const chips = raws.map(s => {
      const tag = s.wikified ? ""
        : (s.fallback ? "（兜底·标题匹配）" : "（未 Wiki 化）");
      const tip = s.fallback ? "冷启动：按标题匹配兑底；Wiki 化后溯源会更精准"
        : (s.wikified ? "已有 Wiki 卡片" : "尚未 Wiki 化");
      return '<button class="src-chip' + (s.wikified ? "" : " raw-not-wikified")
        + '" data-path="' + esc(s.rel) + '" title="' + tip + '">📄 ' + esc(s.rel)
        + tag + "</button>";
    }).join("");
    metaEl.innerHTML = chips
      ? '<div class="src-label">来源</div><div class="sources">' + chips + "</div>"
      : "";
    metaEl.querySelectorAll(".src-chip").forEach(el =>
      el.addEventListener("click", () => App.openPreview(el.dataset.path))
    );
    if (payload.lazy_task) watchLazyTask(payload.lazy_task.task_id);
  }

  function watchLazyTask(taskId) {
    // 飞轮后台运行；完成仅以 toast 提示并刷新界面（过程不在回答区展示）
    const timer = setInterval(async () => {
      try {
        const r = await fetch("/api/wiki/task/" + taskId).then(x => x.json());
        if (r.status === "finished") {
          clearInterval(timer);
          if (r.completed > 0) {
            App.toast("后台已 Wiki 化 " + r.completed + " 篇（错误 " + r.errors + "）",
                      r.errors ? "" : "ok");
          }
          App.refreshState();
        }
      } catch { clearInterval(timer); }
    }, 1200);
  }

  /* ---------------- init ---------------- */

  function init() {
    document.getElementById("chat-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const input = document.getElementById("chat-input");
      const q = input.value.trim();
      if (!q) return;
      input.value = "";
      send(q);
    });
    document.getElementById("chat-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        document.getElementById("chat-form").requestSubmit();
      }
    });

    $("btn-new-session").addEventListener("click", newSession);
    $("session-list").addEventListener("click", (e) => {
      const del = e.target.closest(".session-del");
      if (del) { e.stopPropagation(); deleteSession(del.dataset.del); return; }
      const item = e.target.closest(".session-item");
      if (item) openSession(item.dataset.id);
    });
    $("btn-style").addEventListener("click", openPromptModal);
    $("btn-prompt-save").addEventListener("click", savePrompt);
    $("btn-prompt-clear").addEventListener("click", () => {
      $("prompt-editor").value = "";
      $("prompt-editor").focus();
    });
  }

  /* 切换仓库：回到新会话并加载该仓库自己的会话列表 */
  function onVaultChanged() {
    currentSessionId = null;
    clearMessages();
    loadSessions();
  }

  return { send, init, onVaultChanged };
})();
