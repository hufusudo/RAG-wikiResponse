/* 主应用：状态切换 / Vault / 设置 / 索引树 / Wiki 管理 / Markdown 渲染 */
window.App = (function () {
  const $ = (id) => document.getElementById(id);

  async function api(path, opts) {
    const resp = await fetch(path, opts);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || data.error || "HTTP " + resp.status);
    return data;
  }
  const post = (path, body) =>
    api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  /* ---------------- Markdown 迷你渲染器（安全：先转义） ---------------- */
  function renderMarkdown(src) {
    let text = esc(src ?? "");
    const blocks = [];
    text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
      blocks.push('<pre><code>' + code.replace(/\n$/, "") + "</code></pre>");
      return "\u0000B" + (blocks.length - 1) + "\u0000";
    });
    // 行内代码先抽走（其中的 $ 不参与公式识别）
    const codes = [];
    text = text.replace(/`([^`\n]+)`/g, (_, c) => {
      codes.push('<code class="inline">' + c + "</code>");
      return "\u0000C" + (codes.length - 1) + "\u0000";
    });
    // 数学公式抽走（$$..$$ / \[..\] / \(..\) / $..$），避免被 markdown 处理破坏
    const maths = [];
    text = text.replace(
      /(\$\$[\s\S]+?\$\$|\\[[\s\S]+?\\]|\\\([\s\S]+?\\\)|\$[^\$\n]+?\$)/g,
      (m) => { maths.push(m); return "\u0000M" + (maths.length - 1) + "\u0000"; }
    );
    text = text.replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g,
      '<span class="wikilink" data-target="$1">$2</span>');
    text = text.replace(/\[\[([^\]]+)\]\]/g,
      '<span class="wikilink" data-target="$1">$1</span>');
    text = text.replace(/^### (.*)$/gm, "<h4>$1</h4>");
    text = text.replace(/^## (.*)$/gm, "<h3>$1</h3>");
    text = text.replace(/^# (.*)$/gm, "<h2>$1</h2>");
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    // 列表与引用（逐行）
    const lines = text.split("\n");
    let out = "", inUl = false, inOl = false;
    for (const line of lines) {
      const li = line.match(/^\s*[-*] (.*)$/);
      const oi = line.match(/^\s*\d+[.、] (.*)$/);
      const bq = line.match(/^&gt; ?(.*)$/);
      if (li) {
        if (!inUl) { out += "<ul>"; inUl = true; }
        out += "<li>" + li[1] + "</li>";
      } else if (oi) {
        if (!inOl) { out += "<ol>"; inOl = true; }
        out += "<li>" + oi[1] + "</li>";
      } else if (bq) {
        out += '<blockquote>' + bq[1] + "</blockquote>";
      } else {
        if (inUl) { out += "</ul>"; inUl = false; }
        if (inOl) { out += "</ol>"; inOl = false; }
        if (line.startsWith("\u0000B")) { out += line; continue; }
        if (line.trim() === "") continue;
        if (/^<h\d|<blockquote|<pre/.test(line.trim())) { out += line; continue; }
        out += "<p>" + line + "</p>";
      }
    }
    if (inUl) out += "</ul>";
    if (inOl) out += "</ol>";
    return out
      .replace(/\u0000B(\d+)\u0000/g, (_, i) => blocks[i])
      .replace(/\u0000C(\d+)\u0000/g, (_, i) => codes[i])
      .replace(/\u0000M(\d+)\u0000/g, (_, i) => renderMath(maths[i]));
  }

  /* KaTeX 渲染（CDN 未加载/离线时回退为原始代码） */
  function renderMath(src) {
    const display = src.startsWith("$$") || src.startsWith("\\[");
    const tex = display || src.startsWith("\\(") ? src.slice(2, -2) : src.slice(1, -1);
    if (!window.katex) return '<code class="math-raw">' + esc(src) + "</code>";
    try {
      return katex.renderToString(tex.trim(), { displayMode: display, throwOnError: false });
    } catch (e) {
      return '<code class="math-raw">' + esc(src) + "</code>";
    }
  }

  /* ---------------- Toast / Modal ---------------- */
  let toastTimer = null;
  function toast(msg, kind = "") {
    const el = $("toast");
    el.textContent = msg;
    el.className = "toast " + kind;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
  }

  function openModal(id) { $(id).classList.remove("hidden"); }
  function closeModal(id) { $(id).classList.add("hidden"); }
  function initModals() {
    document.querySelectorAll("[data-close]").forEach(btn =>
      btn.addEventListener("click", () => closeModal(btn.dataset.close)));
    document.querySelectorAll(".modal-mask").forEach(mask =>
      mask.addEventListener("click", (e) => {
        if (e.target === mask) mask.classList.add("hidden");
      }));
  }

  /* ---------------- 状态切换 ---------------- */
  let lastVaultPath = null;

  function showWelcome() {
    lastVaultPath = null;
    $("state-welcome").classList.remove("hidden");
    $("state-app").classList.add("hidden");
  }
  function showApp(state) {
    $("state-welcome").classList.add("hidden");
    $("state-app").classList.remove("hidden");
    $("vault-name").textContent = state.vault_name;
    $("vault-path").textContent = state.path;
    $("vault-path").title = state.path;
    $("meta-vault-name").textContent = state.vault_name;  // 输入区元信息栏
    const s = state;
    $("topbar-stats").innerHTML =
      '<span class="stat-chip">一级索引：<b>' + (s.index_ready ? "就绪" : "未初始化") + "</b></span>" +
      '<span class="stat-chip">二级索引：<b>' + s.subjects + "</b></span>" +
      '<span class="stat-chip">Wiki Card：<b>' + s.wiki_count + "</b></span>" +
      '<span class="stat-chip">Raw：<b>' + s.raw_count + "</b></span>";
    // 仅真正切换仓库时重置会话视图（单纯的 refreshState 不打断当前会话）
    if (state.path !== lastVaultPath) {
      lastVaultPath = state.path;
      Chat.onVaultChanged();
    }
    refreshTree();
    refreshMissing();
  }

  /* ---------------- Vault ---------------- */
  async function openVault(path) {
    try {
      const data = await post("/api/vault/open", { path });
      if (!data.success) throw new Error(data.error || "打开失败");
      $("welcome-error").classList.add("hidden");
      closeModal("modal-fs");
      showApp(data);
      toast("已打开 Vault：" + data.vault_name, "ok");
      return true;
    } catch (e) {
      const msg = "⚠️ " + e.message;
      if (!$("modal-fs").classList.contains("hidden")) {
        // 弹窗内报错，当前工作区不受影响
        $("fs-error").textContent = msg;
        $("fs-error").classList.remove("hidden");
      } else {
        const el = $("welcome-error");
        el.textContent = msg;
        el.classList.remove("hidden");
      }
      return false;
    }
  }

  /* ---------------- 文件夹选择器（打开 / 切换仓库） ---------------- */
  let fsPath = "";
  let fsSeq = 0;  // 快速连点/回车时，丢弃过期响应，防止乱序覆盖

  function fsJoin(dir, name) {
    return dir.endsWith("/") ? dir + name : dir + "/" + name;
  }

  async function openFsPicker(startAt) {
    $("fs-error").classList.add("hidden");
    $("fs-list").innerHTML = '<p class="muted pad">加载中…</p>';
    openModal("modal-fs");
    await fsLoad(startAt || "");
  }

  async function fsLoad(target) {
    const seq = ++fsSeq;
    try {
      const data = await api(
        "/api/fs/list" + (target ? "?path=" + encodeURIComponent(target) : ""));
      if (seq !== fsSeq) return;
      fsPath = data.path;
      $("fs-path").value = data.path;
      $("btn-fs-up").disabled = !data.parent;
      $("btn-fs-up").dataset.parent = data.parent || "";
      $("fs-error").classList.add("hidden");
      renderFsList(data.dirs);
    } catch (e) {
      if (seq !== fsSeq) return;
      $("fs-error").textContent = "⚠️ " + e.message;
      $("fs-error").classList.remove("hidden");
    }
  }

  function renderFsList(dirs) {
    const el = $("fs-list");
    if (!dirs || !dirs.length) {
      el.innerHTML = '<p class="muted pad">（此目录暂无子文件夹）</p>';
      return;
    }
    el.innerHTML = dirs.map(d =>
      '<button type="button" class="fs-item" data-dir="' + esc(d) + '">📁 ' + esc(d) + "</button>"
    ).join("");
    el.querySelectorAll(".fs-item").forEach(b =>
      b.addEventListener("click", () => fsLoad(fsJoin(fsPath, b.dataset.dir))));
  }

  async function pickFsFolder() {
    if (!fsPath) return;
    const btn = $("btn-fs-pick");
    btn.disabled = true;
    btn.textContent = "打开中…";
    await openVault(fsPath);
    btn.disabled = false;
    btn.textContent = "选择此文件夹";
  }

  async function refreshState() {
    const data = await api("/api/vault/state");
    if (data.opened) showApp(data); else showWelcome();
  }

  /* ---------------- 索引树 ---------------- */
  async function refreshTree() {
    try {
      const data = await api("/api/index/tree");
      PageTableView.renderTree($("index-tree"), data.tree, {
        openFile: (rel) => openPreview(rel),
      });
    } catch (e) {
      $("index-tree").innerHTML = '<p class="muted pad">索引树加载失败：' + esc(e.message) + "</p>";
    }
  }

  async function reindex() {
    try {
      const data = await post("/api/indexer/init");
      toast("索引已重建：Raw " + data.raw_count + " / Wiki " + data.wiki_count, "ok");
      await refreshState();
    } catch (e) { toast(e.message, "err"); }
  }

  /* ---------------- Wiki 管理 ---------------- */
  let pollTimer = null;

  async function refreshMissing() {
    try {
      const data = await api("/api/wiki/missing");
      const el = $("missing-list");
      if (!data.items.length) {
        el.innerHTML = '<p class="muted pad">🎉 全部 Raw 均已 Wiki 化</p>';
        return;
      }
      el.innerHTML = data.items.map(it =>
        '<div class="missing-item">' +
        '<div class="grow"><div>' + esc(it.title) + "</div>" +
        '<div class="path">' + esc(it.raw_path) + "</div></div>" +
        '<button class="btn btn-ghost btn-sm" data-preview="' + esc(it.raw_path) + '">预览</button>' +
        '<button class="btn btn-ghost btn-sm" data-distill="' + esc(it.raw_path) + '">提炼</button>' +
        "</div>"
      ).join("");
      el.querySelectorAll("[data-preview]").forEach(b =>
        b.addEventListener("click", () => openPreview(b.dataset.preview)));
      el.querySelectorAll("[data-distill]").forEach(b =>
        b.addEventListener("click", () => interactiveDistill(b.dataset.distill)));
    } catch (e) {
      $("missing-list").innerHTML = '<p class="muted pad">加载失败：' + esc(e.message) + "</p>";
    }
  }

  async function interactiveDistill(rawPath) {
    try {
      toast("正在提炼：" + rawPath);
      const data = await post("/api/wiki/distill", { raw_path: rawPath });
      $("draft-warnings").textContent = data.warnings.length
        ? "⚠️ " + data.warnings.join("；") : "";
      $("draft-warnings").classList.toggle("hidden", !data.warnings.length);
      $("draft-editor").value = data.draft;
      $("draft-status").textContent = "";
      $("btn-save-draft").onclick = async () => {
        try {
          const r = await post("/api/wiki/save", {
            raw_path: rawPath, content: $("draft-editor").value,
          });
          $("draft-status").textContent = "✅ 已保存 " + r.wiki_rel;
          toast("Wiki 已保存：" + r.wiki_rel, "ok");
          await refreshState();
          setTimeout(() => closeModal("modal-draft"), 600);
        } catch (e) { $("draft-status").textContent = "❌ " + e.message; }
      };
      openModal("modal-draft");
    } catch (e) {
      toast(e.message, "err");
      if (String(e.message).includes("LLM_API_KEY")) openSettings();
    }
  }

  let batchItemsCache = [];

  async function openBatchModal() {
    try {
      const data = await api("/api/wiki/missing");
      if (!data.count) {
        toast("没有待处理的笔记：所有 Raw 都已有 Wiki 卡片", "ok");
        return;
      }
      batchItemsCache = data.items || [];
      const sel = $("batch-scope");
      const subjects = [...new Set(batchItemsCache.map(i => i.subject).filter(Boolean))];
      sel.innerHTML = '<option value="">全部学科</option>' +
        subjects.map(s => '<option value="' + esc(s) + '">' + esc(s) + '</option>').join("");
      renderBatchStats("");
      openModal("modal-batch");
    } catch (e) { toast(e.message, "err"); }
  }

  // 粗略估算：UTF-8 中文笔记 ≈ 2.5 字节/字符；LLM 中文 ≈ 1.5 字符/token；
  // 输出每卡 ≈ 700 token。仅用于量级参考，实际以 API 用量为准。
  const EST_BYTES_PER_CHAR = 2.5;
  const EST_CHARS_PER_TOKEN = 1.5;
  const EST_OUTPUT_TOKENS_PER_CARD = 700;
  const DISTILL_INPUT_CAP = 12000;

  function fmtTok(n) {
    return n >= 10000 ? (n / 10000).toFixed(1) + " 万" : String(Math.round(n));
  }

  function renderBatchStats(subject) {
    const list = batchItemsCache.filter(i => !subject || i.subject === subject);
    const n = list.length;
    const inChars = list.reduce(
      (s, i) => s + Math.min((i.size_bytes || 0) / EST_BYTES_PER_CHAR, DISTILL_INPUT_CAP), 0);
    const inTok = Math.round(inChars / EST_CHARS_PER_TOKEN);
    const outTok = n * EST_OUTPUT_TOKENS_PER_CARD;
    const stat = (v, label) =>
      '<div class="stat"><div class="stat-val">' + v + '</div><div class="stat-label">' + label + '</div></div>';
    $("batch-stats").innerHTML =
      stat(n, "待处理笔记（篇）") +
      stat(n, "LLM 调用次数") +
      stat("≈ " + fmtTok(inTok), "预估输入 Token") +
      stat("≈ " + fmtTok(outTok), "预估输出 Token");
  }

  async function confirmBatch() {
    const subject = $("batch-scope").value || null;
    closeModal("modal-batch");
    try {
      const data = await post("/api/wiki/batch", { scope: "all", subject: subject });
      toast("批量任务已启动：" + data.task_id, "ok");
      pollTask(data.task_id);
    } catch (e) {
      toast(e.message, "err");
      if (String(e.message).includes("LLM_API_KEY")) openSettings();
    }
  }

  function pollTask(taskId) {
    clearInterval(pollTimer);
    const box = $("batch-progress");
    box.classList.remove("hidden");
    pollTimer = setInterval(async () => {
      try {
        const t = await api("/api/wiki/task/" + taskId);
        box.innerHTML =
          "进度 " + t.percent + "%（" + t.completed + "/" + t.total + "，错误 " + t.errors + "）" +
          (t.status === "running" && t.current_file
            ? '<div class="path small muted">' + esc(t.current_file) + "</div>" : "") +
          '<div class="progress-bar"><div style="width:' + t.percent + '%"></div></div>';
        if (t.status === "finished") {
          clearInterval(pollTimer);
          toast("批量任务完成：" + t.completed + " 篇，错误 " + t.errors, t.errors ? "" : "ok");
          refreshMissing();
          refreshState();
        }
      } catch {
        clearInterval(pollTimer);
      }
    }, 1000);
  }

  /* ---------------- 预览（raw / wiki / 索引） ---------------- */
  async function openPreview(path) {
    try {
      const data = await api("/api/file/content?path=" + encodeURIComponent(path));
      $("preview-title").textContent = path.split("/").pop();
      $("preview-path").textContent = data.path;
      $("preview-body").innerHTML = renderMarkdown(data.content);
      $("preview-body").querySelectorAll(".wikilink").forEach(el =>
        el.addEventListener("click", () => {
          const t = el.dataset.target;
          if (t.startsWith("raw/")) openPreview(t);
          else resolveWikiLink(t);
        }));
      openModal("modal-preview");
    } catch (e) { toast(e.message, "err"); }
  }

  async function resolveWikiLink(target) {
    // [[名称]] → 尝试 wiki/<名称>.md 或 wiki/<学科>/<名称>.md
    try {
      await openPreview("wiki/" + target.replace(/\.md$/, "") + ".md");
    } catch {
      toast("未找到 Wiki：[[ " + target + " ]]（可能尚未 Wiki 化）", "err");
    }
  }

  /* ---------------- 设置 ---------------- */
  async function openSettings() {
    try {
      const data = await api("/api/settings");
      const s = data.settings;
      $("set-llm-key").value = "";
      $("set-llm-key").placeholder = s.LLM_API_KEY || "sk-…";
      $("set-llm-url").value = s.LLM_BASE_URL || "";
      $("set-emb-key").value = "";
      $("set-emb-key").placeholder = s.EMBEDDING_API_KEY || "留空则使用本地离线 Embedding";
      $("set-emb-url").value = s.EMBEDDING_BASE_URL || "";
      $("llm-test-result").textContent = "";
      openModal("modal-settings");
    } catch (e) { toast(e.message, "err"); }
  }

  async function saveSettings() {
    const values = {};
    const put = (k, v) => { values[k] = v === "" ? "" : v || ""; };
    if ($("set-llm-key").value) values.LLM_API_KEY = $("set-llm-key").value;
    put("LLM_BASE_URL", $("set-llm-url").value.trim());
    if ($("set-emb-key").value) values.EMBEDDING_API_KEY = $("set-emb-key").value;
    put("EMBEDDING_BASE_URL", $("set-emb-url").value.trim());
    try {
      await post("/api/settings", { values });
      closeModal("modal-settings");
      // 保存后自动拉取模型列表；当前模型无效时自动选第一个
      const ok = await refreshModels(false);
      if (ok && modelCache.models.length && !modelCache.models.includes(modelCache.current)) {
        await pickModel(modelCache.models[0]);
      }
      toast("设置已保存，可用模型 " + (modelCache.models.length || "?") + " 个（输入框左侧可切换）", "ok");
    } catch (e) { toast(e.message, "err"); }
  }

  async function testLLM() {
    $("llm-test-result").textContent = "测试中…";
    // 先保存当前表单值再测试，保证测试的是用户填的配置
    if ($("set-llm-key").value || $("set-llm-url").value) {
      await saveSettingsNoClose();
    }
    const r = await post("/api/settings/test");
    $("llm-test-result").textContent = r.success
      ? "✅ 连接成功（" + r.latency_ms + "ms）"
      : "❌ " + (r.error || "失败");
  }

  async function saveSettingsNoClose() {
    const values = {};
    if ($("set-llm-key").value) values.LLM_API_KEY = $("set-llm-key").value;
    values.LLM_BASE_URL = $("set-llm-url").value.trim();
    if ($("set-emb-key").value) values.EMBEDDING_API_KEY = $("set-emb-key").value;
    values.EMBEDDING_BASE_URL = $("set-emb-url").value.trim();
    const r = await post("/api/settings", { values });
    // 确保模型就绪：拉取列表；当前模型无效时自动选第一个
    const ok = await refreshModels(false);
    if (ok && modelCache.models.length && !modelCache.models.includes(modelCache.current)) {
      await pickModel(modelCache.models[0]);
    }
    return r;
  }

  /* ---------------- 模型切换器（保存 Key/BaseURL 后自动拉取模型名） ---------------- */
  let modelCache = { models: [], current: "" };

  async function refreshModels(notify) {
    try {
      const data = await api("/api/llm/models");
      modelCache = { models: data.models || [], current: data.current || "" };
      renderModelBtn();
      if (notify) toast("已获取 " + modelCache.models.length + " 个可用模型", "ok");
      return true;
    } catch (e) {
      if (notify) toast("获取模型列表失败：" + e.message, "err");
      return false;
    }
  }

  function renderModelBtn() {
    $("btn-model-label").textContent = modelCache.current || "模型";
  }

  function renderModelMenu() {
    const menu = $("model-menu");
    const items = modelCache.models.map(m =>
      '<button type="button" class="model-item' + (m === modelCache.current ? " active" : "") +
      '" data-model="' + esc(m) + '">' + esc(m) +
      (m === modelCache.current ? '<span class="check">✓</span>' : "") + "</button>"
    ).join("");
    menu.innerHTML = (items || '<div class="model-empty">暂无可用模型，请检查 Key / Base URL</div>') +
      '<div class="model-input-row"><input id="model-manual" placeholder="手动输入模型名…">' +
      '<button type="button" id="btn-model-manual" class="btn btn-ghost btn-sm">确定</button></div>';
    menu.querySelectorAll(".model-item").forEach(el =>
      el.addEventListener("click", () => pickModel(el.dataset.model)));
    menu.querySelector("#btn-model-manual").addEventListener("click", () => {
      const v = menu.querySelector("#model-manual").value.trim();
      if (v) pickModel(v);
    });
  }

  async function pickModel(model) {
    try {
      await post("/api/settings", { values: { LLM_MODEL: model } });
      modelCache.current = model;
      renderModelBtn();
      $("model-menu").classList.add("hidden");
      toast("模型已切换：" + model, "ok");
    } catch (e) { toast(e.message, "err"); }
  }

  function initModelPicker() {
    $("btn-model").addEventListener("click", () => {
      const menu = $("model-menu");
      if (menu.classList.contains("hidden")) {
        renderModelMenu();
        menu.classList.remove("hidden");
      } else menu.classList.add("hidden");
    });
    // 点击选择器外部收起菜单
    document.addEventListener("click", (e) => {
      const menu = $("model-menu");
      if (!menu.classList.contains("hidden") && !e.target.closest(".model-picker")) {
        menu.classList.add("hidden");
      }
    });
    refreshModels(false);  // 已配置时静默预加载
  }

  /* ---------------- 帮助 ---------------- */
  const HELP_MD = `## 项目功能
读取本地 Obsidian Vault（raw/ 目录）中的 Markdown 笔记，在不修改原始笔记的前提下建立 wiki/ 索引层，用于检索、溯源与问答。

## 目录结构约定
\`\`\`
Vault/
├── raw/     你的原始笔记（系统只读，绝不修改）
│   ├── 学科A/知识点.md
│   └── 学科B/知识点.md
└── wiki/    系统生成的 Wiki 索引与知识卡片
\`\`\`

## Wiki Card 规范
- 顶部必须有 \`> 📖 原始笔记：[标题 (raw)](<相对路径>)\` 溯源行
- 目标 25~40 行：一句话本质 + 核心要点 + 相关知识点
- 双链只指向真实存在的知识点，不制造悬空链接

## API 配置
支持 OpenAI 兼容接口（DeepSeek / OpenAI / 智谱 / 通义 / 硅基流动 等）。Embedding 可不配置，将自动使用本地离线 Embedding。

## Raw 保护说明
- 所有生成内容只写入 wiki/，应用内部禁止写 raw/
- 每次写 Wiki 前后都会校验 Raw 的 hash 与 mtime
- 若 Raw 在使用过程中被外部修改，系统会明确报告`;

  function showHelp() {
    $("help-body").innerHTML = renderMarkdown(HELP_MD);
    openModal("modal-help");
  }

  /* ---------------- init ---------------- */
  function init() {
    initModals();
    Chat.init();

    api("/api/version").then(d => {
      $("app-version").textContent = "v" + d.version;
    }).catch(() => {});

    $("btn-open-vault").addEventListener("click", () => openFsPicker());
    $("btn-quickstart").addEventListener("click", showHelp);
    $("btn-help").addEventListener("click", showHelp);
    $("btn-open-settings").addEventListener("click", openSettings);

    // 顶栏「切换仓库」：直接弹出选择器（从当前仓库目录开始）
    $("btn-switch").addEventListener("click", () =>
      openFsPicker($("vault-path").textContent || ""));
    $("btn-fs-up").addEventListener("click", () => {
      const parent = $("btn-fs-up").dataset.parent;
      if (parent) fsLoad(parent);
    });
    $("btn-fs-home").addEventListener("click", () => fsLoad(""));
    $("btn-fs-go").addEventListener("click", () => fsLoad($("fs-path").value.trim()));
    $("fs-path").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); fsLoad($("fs-path").value.trim()); }
    });
    $("btn-fs-pick").addEventListener("click", pickFsFolder);
    $("btn-reindex").addEventListener("click", reindex);
    $("btn-settings").addEventListener("click", openSettings);
    $("btn-save-settings").addEventListener("click", saveSettings);
    $("btn-test-llm").addEventListener("click", testLLM);
    initModelPicker();
    $("btn-batch").addEventListener("click", openBatchModal);
    $("btn-batch-confirm").addEventListener("click", confirmBatch);
    $("batch-scope").addEventListener("change", (e) => renderBatchStats(e.target.value));

    // Tab 切换
    document.querySelectorAll(".tab").forEach(tab =>
      tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
        tab.classList.add("active");
        document.querySelectorAll(".tab-body").forEach(b => b.classList.add("hidden"));
        $("tab-" + tab.dataset.tab).classList.remove("hidden");
      }));

    refreshState().catch(() => showWelcome());
  }

  document.addEventListener("DOMContentLoaded", init);

  return {
    api, post, toast, renderMarkdown, esc,
    openPreview, openSettings, openVault, refreshState,
  };
})();
