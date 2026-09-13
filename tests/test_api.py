"""API 集成测试：Vault / Settings / Wiki / Chat SSE / Batch / Raw 保护（Test 1~8）。"""
from __future__ import annotations

import json
import time

import pytest

from core.vault.raw_guardian import scan_raw_manifest, verify_raw_intact

FAKE_CARD = """## 一句话本质
通过红黑着色约束维持近似平衡的二叉搜索树，保证 O(log n) 操作。

## 核心要点
1. 五条性质限制最长路径不超过最短路径两倍
2. 插入后通过旋转与变色恢复平衡
3. 删除同理，修复成本低于 AVL
4. 复杂度：查找/插入/删除均为 O(log n)

## 相关知识点
- 横向对比/延伸：[[AVL树]]
"""


@pytest.fixture()
def llm_mock(monkeypatch):
    """Mock 掉真实 LLM：chat_complete 返回固定卡片，chat_stream 返回固定 token 流。"""
    import core.llm as llm

    monkeypatch.setattr(
        llm, "chat_complete",
        lambda *a, **k: FAKE_CARD, raising=True,
    )

    def fake_stream(messages, **k):
        yield "红黑树通过"
        yield "**红黑着色**约束"
        yield "保持近似平衡。"

    monkeypatch.setattr(llm, "chat_stream", fake_stream, raising=True)
    monkeypatch.setattr(
        llm, "test_connection",
        lambda: {"success": True, "latency_ms": 5, "message": "connection ok"},
        raising=True,
    )
    # 让设置检查通过（写测试专用 .env，由 conftest 重定向）
    from config.settings import settings

    settings.update({"LLM_API_KEY": "test-key", "LLM_MODEL": "test-model"})


# ---------------------------------------------------------------- Vault

def test_open_vault_invalid(client):
    resp = client.post("/api/vault/open", json={"path": "/nonexistent/xyz"})
    assert resp.json()["success"] is False


def test_open_vault_missing_raw_dir(client, tmp_path):
    """v2.1 布局适配：无 raw/ 不再拒绝；空仓库仍结构化报错。"""
    # 无 raw/ 但有笔记 → 正常打开（学科即目录布局）
    d = tmp_path / "学科布局"
    touch = d / "数学wiki" / "a.md"
    touch.parent.mkdir(parents=True)
    touch.write_text("# 导数\n", encoding="utf-8")
    body = client.post("/api/vault/open", json={"path": str(d)}).json()
    assert body["success"] is True
    assert body["layout"]["mode"] == "flat"  # 单学科目录 → flat（R2 需 ≥2）
    assert body["layout"]["subjects"] == {"数学wiki": "数学"}

    # 真正空的仓库（排除后无任何 md）→ 结构化报错，不再是 raw/ 缺失文案
    empty = tmp_path / "空仓库"
    empty.mkdir()
    body = client.post("/api/vault/open", json={"path": str(empty)}).json()
    assert body["success"] is False
    assert "未发现任何 Markdown" in body["error"]


def test_open_vault_and_stats(opened_client):
    data = opened_client.get("/api/vault/state").json()
    assert data["opened"] is True
    assert data["raw_count"] == 4
    assert data["wiki_count"] == 0
    assert data["subjects"] == 2  # 示例数字全部来自真实扫描，非写死


# ---------------------------------------------------------------- Indexer

def test_indexer_init_and_raw_untouched(opened_client, demo_vault):
    hash_before = scan_raw_manifest(demo_vault)
    resp = opened_client.post("/api/indexer/init")
    body = resp.json()
    assert body["success"] is True
    assert body["index_ready"] is True

    tree = opened_client.get("/api/index/tree").json()["tree"]
    assert tree["type"] == "root"
    raw_node = next(c for c in tree["children"] if c["name"] == "raw")
    assert raw_node["count"] == 4
    wiki_node = next(c for c in tree["children"] if c["name"] == "wiki")
    assert wiki_node["count"] == 3  # 1 master INDEX + 2 sub INDEX

    assert verify_raw_intact(demo_vault, hash_before) == []  # Test 2


def test_missing_list(opened_client):
    data = opened_client.get("/api/wiki/missing").json()
    assert data["count"] == 4
    assert {i["raw_path"] for i in data["items"]} >= {"raw/数据结构/红黑树.md"}
    # size_bytes / total_size_bytes：供前端估算批量蒸馏 Token 成本
    assert all("size_bytes" in i and i["size_bytes"] > 0 for i in data["items"])
    assert data["total_size_bytes"] == sum(i["size_bytes"] for i in data["items"])


# ---------------------------------------------------------------- Settings

def test_settings_update_and_mask(opened_client):
    resp = opened_client.post(
        "/api/settings",
        json={"values": {"LLM_API_KEY": "sk-secret-123", "LLM_BASE_URL": "https://x/v1"}},
    )
    s = resp.json()["settings"]
    assert s["LLM_API_KEY"].endswith("****") and "sk-secret-123" not in json.dumps(s)
    s2 = opened_client.get("/api/settings").json()["settings"]
    assert s2["LLM_BASE_URL"] == "https://x/v1"


def test_settings_unknown_key_rejected(opened_client):
    resp = opened_client.post("/api/settings", json={"values": {"HACK": "1"}})
    assert resp.status_code == 400


# ---------------------------------------------------------------- Wiki distill

def test_distill_and_save_flow(opened_client, demo_vault, llm_mock):
    hash_before = scan_raw_manifest(demo_vault)

    draft = opened_client.post(
        "/api/wiki/distill", json={"raw_path": "raw/数据结构/红黑树.md"}
    ).json()
    assert draft["success"] is True
    assert draft["draft"].startswith("# 红黑树")
    assert "raw/数据结构/红黑树.md" in draft["draft"]  # Test 3：溯源行存在

    saved = opened_client.post(
        "/api/wiki/save",
        json={"raw_path": "raw/数据结构/红黑树.md", "content": draft["draft"]},
    ).json()
    assert saved["success"] is True
    assert saved["wiki_rel"] == "wiki/数据结构/红黑树.md"

    state = opened_client.get("/api/vault/state").json()
    assert state["wiki_count"] == 1
    missing = opened_client.get("/api/wiki/missing").json()
    assert "raw/数据结构/红黑树.md" not in {i["raw_path"] for i in missing["items"]}

    # Test 6（部分）：整个 distill→save 流程 Raw 不变
    assert verify_raw_intact(demo_vault, hash_before) == []


def test_save_without_traceability_rejected(opened_client, llm_mock):
    resp = opened_client.post(
        "/api/wiki/save",
        json={"raw_path": "raw/数据结构/AVL树.md", "content": "# AVL\n\n没有溯源行"},
    )
    assert resp.status_code == 400


def test_dangling_link_removed_on_save(opened_client, llm_mock):
    content = (
        "# AVL 树\n\n> 📖 原始笔记：[[raw/数据结构/AVL树.md|AVL 树 (raw)]]\n\n"
        "## 一句话本质\n严格平衡的 BST。\n\n"
        "## 核心要点\n1. 高度差 ≤ 1\n2. 旋转恢复\n3. O(log n)\n4. 易错点：平衡因子方向\n\n"
        "## 相关知识点\n- 前置依赖：[[不存在的概念]]\n"
    )
    saved = opened_client.post(
        "/api/wiki/save",
        json={"raw_path": "raw/数据结构/AVL树.md", "content": content},
    ).json()
    assert saved["success"] is True
    card = opened_client.get(
        "/api/file/content", params={"path": "wiki/数据结构/AVL树.md"}
    ).json()["content"]
    assert "[[不存在的概念]]" not in card  # Test 4：悬空双链被降级
    assert "不存在的概念" in card


# ---------------------------------------------------------------- Chat (SSE)

def _read_sse(resp):
    events = []
    for line in resp.iter_lines():
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            events.append((ev, json.loads(line[5:].strip())))
    return events


def test_chat_requires_llm_config(opened_client):
    resp = opened_client.post("/api/chat", json={"message": "什么是红黑树？"})
    assert resp.status_code == 400
    assert "LLM_API_KEY" in resp.json()["detail"]


def test_chat_sse_streaming(opened_client, llm_mock):
    with opened_client.stream(
        "POST", "/api/chat", json={"message": "红黑树为什么能够保持近似平衡？"}
    ) as resp:
        assert resp.status_code == 200
        events = _read_sse(resp)

    kinds = [e for e, _ in events]
    assert kinds[0] == "route"  # Test 5：寻址轨迹先行
    assert kinds.count("token") == 3  # Test 8：流式 token，而非一次性返回
    assert kinds[-1] == "done"

    route = dict(events)["route"]
    assert route["coverage"]["fallback_used"] is True  # 冷启动无 Wiki → Raw 标题兜底
    done = dict(events)["done"]
    assert any(s["rel"] == "raw/数据结构/红黑树.md" for s in done["sources"])


def test_chat_requires_vault(client):
    resp = client.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 400


# ---------------------------------------------------------------- 会话与风格（v2.3）

def _chat_events(client, message, session_id=None):
    body = {"message": message}
    if session_id:
        body["session_id"] = session_id
    with client.stream("POST", "/api/chat", json=body) as resp:
        assert resp.status_code == 200
        return _read_sse(resp)


def test_chat_sessions_crud(opened_client, demo_vault, llm_mock):
    """会话：首轮自动创建 → 列表 → 详情（含来源）→ 继续 → 删除。"""
    hash_before = scan_raw_manifest(demo_vault)
    assert opened_client.get("/api/chat/sessions").json()["sessions"] == []

    events = _chat_events(opened_client, "红黑树为什么能保持近似平衡？")
    sid = dict(events)["route"]["session_id"]
    assert sid

    sessions = opened_client.get("/api/chat/sessions").json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["id"] == sid
    assert sessions[0]["title"].startswith("红黑树")
    assert sessions[0]["message_count"] == 2

    detail = opened_client.get(f"/api/chat/sessions/{sid}").json()["session"]
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"].startswith("红黑树通过")
    assert any(s["rel"] == "raw/数据结构/红黑树.md" for s in detail["messages"][1]["sources"])

    # 继续同一会话
    events2 = _chat_events(opened_client, "它和 AVL 树有什么区别？", session_id=sid)
    assert dict(events2)["route"]["session_id"] == sid
    detail = opened_client.get(f"/api/chat/sessions/{sid}").json()["session"]
    assert len(detail["messages"]) == 4

    # 删除
    assert opened_client.delete(f"/api/chat/sessions/{sid}").json()["removed"] is True
    assert opened_client.get("/api/chat/sessions").json()["sessions"] == []
    assert opened_client.get(f"/api/chat/sessions/{sid}").status_code == 404
    assert opened_client.delete(f"/api/chat/sessions/{sid}").status_code == 404

    # 会话落盘不碰 raw（铁律）
    assert verify_raw_intact(demo_vault, hash_before) == []


def test_chat_session_invalid_id_rejected(opened_client, llm_mock):
    assert opened_client.get("/api/chat/sessions/BAD_ID%21").status_code == 400
    assert opened_client.delete("/api/chat/sessions/BAD_ID%21").status_code == 400


def test_chat_multi_turn_history(opened_client, llm_mock, monkeypatch):
    """继续会话时历史轮次进入 LLM messages（多轮上下文）。"""
    import core.llm as llm

    captured = []

    def capture(messages, **k):
        captured.append(messages)
        yield "ok"

    monkeypatch.setattr(llm, "chat_stream", capture, raising=True)

    sid = dict(_chat_events(opened_client, "什么是红黑树？"))["route"]["session_id"]
    _chat_events(opened_client, "它和 AVL 有什么区别？", session_id=sid)

    second = captured[1]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    assert "什么是红黑树？" in second[1]["content"]
    assert second[2]["content"] == "ok"


def test_chat_sessions_isolated_per_vault(opened_client, demo_vault, tmp_path, llm_mock):
    """会话按仓库隔离：切换仓库只看到该仓库自己的会话。"""
    sid = dict(_chat_events(opened_client, "什么是红黑树？"))["route"]["session_id"]

    other = tmp_path / "OtherVault"
    (other / "raw").mkdir(parents=True)
    (other / "raw" / "笔记.md").write_text("# 另一仓库\n\n内容。\n", encoding="utf-8")
    assert opened_client.post("/api/vault/open", json={"path": str(other)}).json()["success"]
    assert opened_client.get("/api/chat/sessions").json()["sessions"] == []

    assert opened_client.post("/api/vault/open", json={"path": str(demo_vault)}).json()["success"]
    ids = [s["id"] for s in opened_client.get("/api/chat/sessions").json()["sessions"]]
    assert ids == [sid]


def test_prompt_roundtrip_and_injection(opened_client, demo_vault, llm_mock, monkeypatch):
    """全局风格 Prompt：保存 → 读取 → 注入 system 消息；清空即删除。"""
    import core.llm as llm

    captured = []

    def capture(messages, **k):
        captured.append(messages)
        yield "ok"

    monkeypatch.setattr(llm, "chat_stream", capture, raising=True)

    assert opened_client.get("/api/prompt").json()["prompt"] == ""
    saved = opened_client.post(
        "/api/prompt", json={"content": "用费曼式讲解，多打比方"}
    ).json()
    assert saved["prompt"] == "用费曼式讲解，多打比方"
    assert opened_client.get("/api/prompt").json()["prompt"] == "用费曼式讲解，多打比方"

    _chat_events(opened_client, "什么是红黑树？")
    system = captured[0][0]["content"]
    assert "用费曼式讲解，多打比方" in system
    assert "五段" in system  # 默认结构仍在（风格不破坏溯源纪律）

    # 落盘在 wiki/.llmwiki/（应用可写区），清空后文件删除
    style_file = demo_vault / "wiki" / ".llmwiki" / "answer_style.md"
    assert style_file.is_file()
    assert opened_client.post("/api/prompt", json={"content": "   "}).json()["prompt"] == ""
    assert not style_file.exists()

    # 超长拒绝
    too_long = "长" * 8001
    assert opened_client.post("/api/prompt", json={"content": too_long}).status_code == 400


# ---------------------------------------------------------------- Batch

def _wait_task(client, task_id, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        body = client.get(f"/api/wiki/task/{task_id}").json()
        if body["status"] == "finished":
            return body
        time.sleep(0.2)
    raise AssertionError("batch task timeout")


def test_batch_flow(opened_client, demo_vault, llm_mock):
    hash_before = scan_raw_manifest(demo_vault)
    task_id = opened_client.post("/api/wiki/batch", json={"scope": "all"}).json()["task_id"]
    task = _wait_task(opened_client, task_id)

    assert task["total"] == 4
    assert task["completed"] == 4
    assert task["errors"] == 0

    state = opened_client.get("/api/vault/state").json()
    assert state["wiki_count"] == 4
    assert state["subjects"] == 2

    # 断点续传：再跑一次批量，缺 Wiki 的 Raw 为 0，任务 total = 0
    task2_id = opened_client.post("/api/wiki/batch", json={"scope": "all"}).json()["task_id"]
    task2 = _wait_task(opened_client, task2_id)
    assert task2["total"] == 0

    # Test 6：全流程后 Raw hash 不变
    assert verify_raw_intact(demo_vault, hash_before) == []


def test_batch_subject_scope(opened_client, llm_mock):
    task_id = opened_client.post(
        "/api/wiki/batch", json={"scope": "subject", "subject": "计算机组成原理"}
    ).json()["task_id"]
    task = _wait_task(opened_client, task_id)
    assert task["total"] == 1


# ---------------------------------------------------------------- LLM models

def test_llm_models_requires_key(opened_client):
    """未配置 LLM_API_KEY → 结构化 400。"""
    resp = opened_client.get("/api/llm/models")
    assert resp.status_code == 400


def test_llm_models_list(opened_client, llm_mock, monkeypatch):
    """保存 Key/BaseURL 后自动拉取模型列表（OpenAI 兼容 /models）。"""
    import core.llm as llm
    from config.settings import settings

    monkeypatch.setattr(
        llm, "list_models",
        lambda: ["deepseek-chat", "deepseek-reasoner"], raising=True,
    )
    settings.update({"LLM_MODEL": "deepseek-reasoner"})
    data = opened_client.get("/api/llm/models").json()
    assert data["success"] is True
    assert data["models"] == ["deepseek-chat", "deepseek-reasoner"]
    assert data["current"] == "deepseek-reasoner"
