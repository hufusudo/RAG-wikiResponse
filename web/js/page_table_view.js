/* 寻址轨迹 + 索引树 渲染（§17：仅为检索过程可视化，非真实地址） */
window.PageTableView = (function () {
  const LEVEL_NAMES = {
    master: "总索引", sub: "子索引", wiki: "Wiki", related: "关联", raw: "Raw",
  };

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function renderTrace(container, trace) {
    if (!container) return;
    const steps = (trace || []).map((s, i) => {
      const arrow = i === 0 ? "" : '<span class="route-arrow"> → </span>';
      const label = esc(s.label || LEVEL_NAMES[s.level] || s.level);
      const clickable = (s.level === "wiki" || s.level === "raw") && s.detail
        ? ' clickable" title="' + esc(s.detail) + '" data-path="' + esc(s.detail) + '"'
        : '"';
      return arrow + '<span class="route-step' + clickable + '>' +
        '<span class="lv">' + esc(LEVEL_NAMES[s.level] || s.level) + "</span>" + label + "</span>";
    });
    container.innerHTML = steps.join("") || '<span class="muted">无轨迹</span>';
    container.querySelectorAll(".route-step.clickable").forEach(el => {
      el.addEventListener("click", () => App.openPreview(el.dataset.path));
    });
  }

  // ---------- 文件系统树（可折叠，§23 Tab 1） ----------
  const FILE_ICONS = { raw: "📄", wiki: "🃏" };

  function fileLabel(node) {
    if (node.name === "INDEX.md") return "📑 INDEX";
    let html = esc(node.name);
    if (node.type === "raw") {
      html += node.wikified
        ? ' <span class="mark ok">✅ 已 Wiki 化</span>'
        : ' <span class="mark">⬜ 未 Wiki 化</span>';
    }
    return html;
  }

  function renderNode(node, handlers, depth) {
    const el = document.createElement("div");
    if (node.type === "dir" || node.type === "root") {
      el.className = "tree-dir";
      const label = document.createElement("div");
      label.className = "node-label";
      label.innerHTML = '<span class="caret">▾</span>' +
        '<span class="dicon">📁</span>' +
        '<span class="dir-name">' + esc(node.name) + "</span>" +
        '<span class="count">' + node.count + " 篇</span>";
      const kids = document.createElement("div");
      kids.className = "tree-children";
      (node.children || []).forEach(c => kids.appendChild(renderNode(c, handlers, depth + 1)));
      el.appendChild(label);
      el.appendChild(kids);
      const setCollapsed = (col) => {
        el.classList.toggle("collapsed", col);
        kids.style.display = col ? "none" : "";
        label.querySelector(".dicon").textContent = col ? "📁" : "📂";
      };
      label.addEventListener("click", () =>
        setCollapsed(!el.classList.contains("collapsed")));
      setCollapsed(depth >= 3);  // 根 / raw / wiki / 一级学科默认展开，更深默认折叠
    } else {
      el.className = "tree-file";
      el.innerHTML = '<span class="icon">' + (FILE_ICONS[node.type] || "📄") + "</span>" +
        '<span class="file-name">' + fileLabel(node) + "</span>";
      el.title = node.rel;
      el.addEventListener("click", () => handlers.openFile(node.rel));
    }
    return el;
  }

  function renderTree(container, tree, handlers) {
    container.innerHTML = "";  // 重绘前清空，避免多次 refresh 后重复追加
    if (!tree) { container.innerHTML = '<p class="muted pad">暂无索引</p>'; return; }
    container.appendChild(renderNode(tree, handlers, 0));
  }

  return { renderTrace, renderTree, esc };
})();
