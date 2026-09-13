"""pytest 全局夹具。

注意：必须在导入 api 之前设置 LLMWIKI_ENV_FILE，
把测试期间的 .env 重定向到临时目录（不污染真实配置）。
"""
from __future__ import annotations

import os
import tempfile

_TMP_ENV_DIR = tempfile.mkdtemp(prefix="llmwiki-test-env-")
os.environ["LLMWIKI_ENV_FILE"] = os.path.join(_TMP_ENV_DIR, ".env")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_state():
    """每个测试前清空共享状态（settings 单例 / Vault 上下文），避免测试间泄漏。"""
    from api.routes import vault_manager
    from config.settings import SETTINGS_KEYS, settings

    settings.update({k: "" for k in SETTINGS_KEYS})
    vault_manager.close()
    yield


@pytest.fixture()
def demo_vault(tmp_path):
    """标准结构的演示 Vault：2 个学科 + 1 个根级笔记。"""
    root = tmp_path / "DemoVault"
    d1 = root / "raw" / "数据结构"
    d2 = root / "raw" / "计算机组成原理"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)

    (d1 / "红黑树.md").write_text(
        "# 红黑树\n\n"
        "红黑树是一种自平衡二叉搜索树，通过红黑着色约束保证从根到叶子的"
        "最长路径不超过最短路径的两倍，从而保证查找、插入、删除均为 O(log n)。\n\n"
        "## 五条性质\n"
        "1. 节点是红色或黑色\n"
        "2. 根节点是黑色\n"
        "3. 叶子（NIL）是黑色\n"
        "4. 红色节点的子节点必为黑色\n"
        "5. 任一节点到其后代叶子的黑色节点数目相同\n\n"
        "插入后通过旋转与变色恢复平衡；AVL 树是另一种更严格的平衡方案。\n",
        encoding="utf-8",
    )
    (d1 / "AVL树.md").write_text(
        "# AVL 树\n\nAVL 树是严格平衡的二叉搜索树，任意节点左右子树高度差不超过 1。\n"
        "相比红黑树查找更快，但插入删除需要更多旋转。\n",
        encoding="utf-8",
    )
    (d2 / "中断机制.md").write_text(
        "# 异常与中断机制\n\n中断使 CPU 能够响应异步事件；"
        "异常来自指令执行内部。二者共同构成控制流转移的核心机制。\n",
        encoding="utf-8",
    )
    (root / "raw" / "学习方法.md").write_text(
        "# 学习方法\n\n费曼技巧：把概念讲给外行听，暴露理解缺口。\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def opened_client(client, demo_vault):
    resp = client.post("/api/vault/open", json={"path": str(demo_vault)})
    assert resp.json()["success"] is True, resp.json()
    return client
