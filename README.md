# simAgent

基于 **Pydantic AI slim 2.66.0** 的企业仿真agent。

Python 3.11+；使用 `uv sync` 安装核心依赖。模型供应商和 MCP 按需安装：
`uv sync --extra openai`、`uv sync --extra anthropic`、`uv sync --extra mcp`。

## 导航

```text
src/simagent/
├── cli.py                # 启动入口
├── agents/               # 规划与结果解释
├── engine/               # 工作流、校验、调度、评估
├── tools/
│   ├── toolset.py        # 工具注册入口：官方 FunctionToolset
│   ├── serpapi_search.py # SerpApi 联网搜索
│   ├── leave_approval/   # 请假流程规则、排队分析、仿真与资源优化
│   ├── catalog.py        # 自动生成工具清单
│   └── registered_tools.md # 已注册工具记录
├── schemas/              # 业务输入、计划、结果、运行记录
├── web/                  # 网页服务与 static 前端资源
└── infrastructure/       # 配置、模型连接、存储、依赖
```

工具从 [tools/toolset.py](src/simagent/tools/toolset.py) 接入；

## 网页任务工作台

在项目根目录的 `.env` 中填写配置。首次克隆仓库时，可将 `.env.example` 复制为 `.env`；
如果已经存在 `.env`，直接编辑，保留其中自己的配置。

```dotenv
DEEPSEEK_API_KEY=填入你的密钥
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com

SERPAPI_API_KEY=填入你的SerpApi密钥

SIMAGENT_HOST=127.0.0.1
SIMAGENT_PORT=8000
SIMAGENT_TIMEOUT_SECONDS=180
```

在项目目录启动：

```sh
uv run --extra web simagent
```

浏览器打开 **http://127.0.0.1:8000**，即可创建任务并持续补充信息。


