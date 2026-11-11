# simAgent

基于 **Pydantic AI slim 2.66.0** 的企业仿真agent。
业务输入 → PlanSolveAgent → 程序校验 → SimulationExecutor → 程序指标评估 → ResultInterpreter。
执行或验收失败时，可携带结构化反馈返回规划智能体修订。

Python 3.11+；使用 `uv sync` 安装核心依赖。模型供应商和 MCP 按需安装：
`uv sync --extra openai`、`uv sync --extra anthropic`、`uv sync --extra mcp`。


## 网页问答

在项目根目录的 `.env` 中填写配置。首次克隆仓库时，可将 `.env.example` 复制为 `.env`；
如果已经存在 `.env`，直接编辑，保留其中自己的配置。

```dotenv
DEEPSEEK_API_KEY=填入你的密钥
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com

SIMAGENT_HOST=127.0.0.1
SIMAGENT_PORT=8000
SIMAGENT_TIMEOUT_SECONDS=180
```

在项目目录启动：

```sh
uv run --extra web simagent
```

浏览器打开 **http://127.0.0.1:8000**，即可连续问答。默认启动网页服务，原终端输入模式已替换。
支持流式回答、复制回答、停止生成和新建对话。Enter 发送，Shift + Enter 换行；
“新建对话”会清空当前会话上下文。生成失败或主动停止的轮次不会加入后续上下文。






