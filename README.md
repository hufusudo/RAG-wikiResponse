# LLMwiki 智能伴学系统

> 本地优先的 Markdown 知识库伴学系统：只读你的笔记，生成 Wiki 索引层，用混合检索 + DAG 寻址完成**可溯源**的 LLM 问答。

## 一、项目介绍

LLMwiki 以本地 Obsidian 风格 Markdown 笔记为数据源：应用**只读**原始笔记（`raw/`），在其旁生成**只写**的 Wiki 索引层（`wiki/`），把散乱的长笔记压缩成结构化知识卡片，再基于卡片与原文完成检索问答。

```text
raw/ 原始笔记（只读，永不修改）
   │  扫描 + LLM 蒸馏
   ▼
wiki/ 索引层：Master INDEX → Sub INDEX → Wiki Card（25~40 行语义压缩）
   │  BM25L + Embedding + RRF 混合检索 → 概念图 1-hop 扩展 → 五级 DAG 寻址
   ▼
LLM 五段式回答（SSE 流式）+ 原文溯源（每个来源可点击回看）
```

**设计要点**

- **本地优先**：笔记、索引、会话全部留在本机，只需一个 OpenAI 兼容的 LLM API
- **绝不写原始笔记**：应用层拒绝一切对 `raw/` 的写入，并做 hash 前后校验
- **确定性归代码，发散性归 LLM**：扫描 / 拓扑 / 防环 / 预算截断全部是普通 Python，LLM 只负责提炼与回答
- **回答有据可查**：回答下方列出真实读取的原文来源；Wiki 卡片头部带标准 Markdown 相对链接，VS Code / GitHub / Typora 可直接 Ctrl+Click 跳转

**技术栈**：Python 3.11 · FastAPI · 原生 HTML/CSS/JS（无前端框架）· BM25L + Embedding 混合检索 · KaTeX

## 二、如何使用

### 1. 安装

```bash
git clone https://github.com/hufusudo/RAG-wikiResponse.git
cd llmwiki

conda create -n learning_assistant python=3.11 -y
conda activate learning_assistant
pip install -r requirements.txt
```

### 2. 配置模型

支持任意 OpenAI 兼容服务（DeepSeek / OpenAI / 智谱 / 通义 / 硅基流动 等）：

```bash
cp .env.example .env
# 编辑 .env：至少填 LLM_API_KEY；LLM_BASE_URL / LLM_MODEL 按服务商填写
```

Embedding 可不配置：不填时自动降级为**本地离线 Embedding**（字符 n-gram 哈希），混合检索仍可用。也可以启动后在页面「模型与 API Key 设置」中填写，无需改 `.env`。

### 3. 启动

```bash
python scripts/make_demo_vault.py   # 可选：生成演示仓库，先体验再连自己的笔记
python run.py                       # http://127.0.0.1:8000
```

### 4. 使用流程

1. 点「打开本地仓库…」→ 在弹窗中选择你的 Obsidian / Markdown 笔记文件夹
2. 点「初始化 / 更新索引」建立两级目录索引
3. 直接提问：回答流式输出，底部来源 chips 可点击回看原文
4. 想提升检索精度时，用「Wiki 管理 → 批量初始化」把未 Wiki 化的笔记逐篇蒸馏成卡片
5. 历史会话在左侧栏，可随时切换 / 删除；输入框下「📝 回答风格」可为当前仓库设置全局回答风格

### 5. 测试

```bash
python -m pytest tests/ -q     # 75 项，LLM 全 mock，离线可跑
```

## 三、项目结构

```text
llmwiki/
├── api/                    # FastAPI 路由与入口（SSE、静态前端挂载）
├── core/
│   ├── vault/              # 仓库生命周期、布局适配（任意目录结构）、Raw 保护
│   ├── indexer/            # 两级索引构建、Wiki 卡片蒸馏（单篇 / 批量 / 飞轮）
│   ├── rag/                # BM25L + Embedding + RRF 混合检索、五级 DAG 寻址
│   ├── graph/              # 概念有向图抽取、溯源行双格式解析适配
│   ├── generator/          # 五段式回答组装、上下文预算、飞轮补卡
│   ├── chat_store.py       # 会话与回答风格 Prompt 的仓库级持久化
│   ├── llm.py              # OpenAI 兼容客户端（流式 / 连通性测试 / 热更新）
│   └── log.py
├── config/                 # 配置（.env 热更新）
├── web/                    # 前端：index.html + js/（应用 / 聊天）+ css/
├── scripts/
│   └── make_demo_vault.py  # 生成演示仓库
├── tests/                  # pytest：API / 索引 / 检索 / 图谱 / 布局 / Raw 保护
├── requirements.txt
└── run.py                  # python run.py → 127.0.0.1:8000
```

个人笔记库与 `.env` 均不入库（见 `.gitignore`）。

## 四、已完成功能

| 模块 | 功能 | 入口 |
|---|---|---|
| 仓库 | 打开 / 切换任意布局仓库（应用内文件夹选择器；自动检测 + 手动声明 + frontmatter 学科覆盖） | `POST /api/vault/open`、`GET /api/fs/list` |
| 安全 | Raw 只读：写入隔离 + hash/mtime 前后校验，外部改动检测 | `core/vault/raw_guardian.py` |
| 索引 | 0 Token 两级索引（Master INDEX → Sub INDEX） | `POST /api/indexer/init` |
| Wiki | 卡片蒸馏：单篇交互提炼 / 批量断点续传 / 问答后 Lazy 飞轮补卡 | `POST /api/wiki/distill`、`/api/wiki/save`、`/api/wiki/batch` |
| 检索 | BM25L + Embedding + RRF 混合；相关性门槛过滤 + 冷启动兜底；概念图 1-hop 扩展（visited 防环 + 配额熔断） | `core/rag/dag_search.py` |
| 问答 | 五段式回答、SSE 流式输出、原文溯源 chips、KaTeX 公式 | `POST /api/chat` |
| 会话 | 按仓库隔离的会话侧边栏：新建 / 切换 / 删除，多轮记忆 | `GET/DELETE /api/chat/sessions/*` |
| 风格 | 全局回答风格 Prompt（当前仓库生效，不破坏五段式与溯源纪律） | `GET/POST /api/prompt` |
| 巡检 | 知识库拓扑巡检：孤岛概念与悬空链报告 | `GET /api/graph/lint` |
| 质量 | 75 项 pytest 全绿（LLM mock，离线可跑）；真实 350 篇笔记库实测通过 | `python -m pytest tests/ -q` |
