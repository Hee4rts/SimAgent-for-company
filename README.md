# simAgent

基于 **Pydantic AI slim 2.66.0** 的企业仿真agent。
业务输入 → PlanSolveAgent → 程序校验 → SimulationExecutor → 程序指标评估 → ResultInterpreter。
执行或验收失败时，可携带结构化反馈返回规划智能体修订。

Python 3.11+；使用 `uv sync` 安装核心依赖。模型供应商和 MCP 按需安装：
`uv sync --extra openai`、`uv sync --extra anthropic`、`uv sync --extra mcp`。

## 导航

```text
src/simagent/
├── cli.py                # 启动入口
├── agents/               # 规划与结果解释
├── engine/               # 工作流、校验、调度、评估
├── tools/
│   └── toolset.py        # 工具注册入口：官方 FunctionToolset
├── schemas/              # 业务输入、计划、结果、运行记录
├── web/                  # 网页服务与 static 前端资源
└── infrastructure/       # 配置、模型连接、存储、依赖
```

工具从 [tools/toolset.py](src/simagent/tools/toolset.py) 接入；
各目录的 `__init__.py` 仅用于包说明和导出，业务代码放在有明确名称的模块中。

## 网页任务工作台

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

浏览器打开 **http://127.0.0.1:8000**，即可创建任务并持续补充信息。
支持流式回答、复制回答、停止生成和新建任务。Enter 发送，Shift + Enter 换行；
“新建任务”会清空当前内容与上下文，当前不保存历史任务列表。生成失败或主动停止的轮次不会加入后续上下文。
网页直接使用 Pydantic AI 官方工具接口。默认工具集为空，添加工具后重启即可调用。
网页采用 Agent 原生调用；需要依赖调度、任务记录和指标评估时，使用 `SimulationFramework`。

## 注册工具

工具注册入口是 **`src/simagent/tools/toolset.py`**，其中创建官方工具集：

```python
from pydantic_ai import FunctionToolset
from ..infrastructure.dependencies import Services

toolset = FunctionToolset[Services]()
```

在这个文件中导入自己的工具函数后，使用官方 API 注册，以下方式任选一种：

| 官方接口 | 用途 |
| --- | --- |
| `toolset.add_function(your_function)` | 添加已有的同步或异步函数，自动识别上下文参数 |
| `toolset.add_tool(Tool(your_function))` | 添加原生 `Tool`，从 `pydantic_ai` 导入 `Tool` |
| `@toolset.tool_plain` | 定义不需要上下文的工具函数 |
| `@toolset.tool` | 定义首参数为 `RunContext[Services]` 的工具函数 |

函数的类型注解生成参数 schema，docstring 提供工具说明；不需要额外定义工具类别。
机器学习、机理模型、运筹求解和搜索都可以封装为这样的函数。
工具返回 Pydantic 对象或可序列化的字典、列表、标量即可；框架不内置业务工具或案例。

业务工具可以放在 `tools/` 的独立模块中。在模块内使用 `from .toolset import toolset`，
并在 `tools/toolset.py` **创建 toolset 之后**导入该模块，使其在启动时完成注册。
也可以直接导入已有函数后调用 `add_function`。同一个工具只注册一次。

## Agent 与网页接入

自行创建 Agent 时，直接按照官方写法传入工具：

```python
from pydantic_ai import Agent, Tool

agent = Agent(model, tools=[Tool(your_function)])
```

使用项目默认工具集和依赖时：

```python
from pydantic_ai import Agent
from simagent import Services
from simagent.tools.toolset import toolset

services = Services()
agent = Agent(model, deps_type=Services, toolsets=[toolset])
result = await agent.run(question, deps=services)
```

默认网页自动加载这个 `toolset`，启动命令保持不变。
也可在自己的应用入口调用 `create_app(tools=[Tool(your_function)], services=services)`，
或 `create_app(toolsets=[your_toolset], services=services)`。
传入 `toolsets` 时替换默认工具集；`tools` 中的函数会与选定工具集一起提供给 Agent。

网页的 `toolsets` 也接受官方 MCP 工具集（按需安装 `uv sync --extra mcp`），
由 Agent 管理连接生命周期。HTTP 接口可封装为普通异步函数，子 Agent 可在普通工具函数内调用。

访问 **http://127.0.0.1:8000/api/tools** 查看工具名称、说明和原生 schema。
在代码中通过 `toolset.tools` 查看已注册的原生 `Tool`。
带有 `prepare` 的工具，其实际可用性由调用上下文决定。

## 依赖与计划执行

客户端、已加载模型等应用依赖放入 `Services(clients=...)`，通过官方
`RunContext[Services]` 的 `ctx.deps.clients` 读取；应用负责资源的创建和关闭。
密钥保留在本机 `.env`，不要写入工具描述或 metadata。

`PlanSolveAgent(model, toolset, ...)`、`PlanValidator(toolset)` 和
`SimulationExecutor(toolset, store, ...)` 接受同一个官方 `FunctionToolset`。
规划器和执行器需要服务依赖时，分别传入 `services=services`，并共用同一个 validator。
计划执行使用静态函数工具集；远程能力可封装为其中的函数。
官方 MCP、动态/包装工具集和审批流程可直接用于原生 Agent 或自建应用。

工具注册时不要求框架专有参数。计划所需的版本、幂等性和单位可以通过官方
`metadata={"simagent": {...}}` 可选补充，支持 `version`、`tags`、`idempotent`、
`parameter_units`、`output_units`；默认版本为 `"1"`、幂等性为 `False`。
只有重复执行安全的工具才设置 `idempotent=True` 并允许计划重试。

执行器通过每次调用的依赖副本提供 `ctx.deps.input`（场景、数据引用、种子等）
和 `ctx.deps.idempotency_key`；网页普通调用时这两项默认为 `None`。
执行结果存入 `ToolResult.payload`，也可以主动返回 `ToolResult` 附带单位、文件引用或诊断。
原生工具的类型注解不强制验证返回值；NumPy、DataFrame 等需在函数中转换或保存为文件引用。

框架内部的 `engine/tool_runtime.py` 只负责计划参数验证、结果存储格式与运行中的函数句柄，
没有注册 API。原生 `prepare`、`args_validator` 在调用时执行；
`sequential=True` 在同一次计划中独占调度。
原生 Agent 的重试与超时遵循 SDK，计划由任务的重试和截止时间控制。
同步工作线程无法强制终止，计划不会因超时就假定它已停止。

任务记录和已完成结果仍会持久化；同一执行器实例可以重新接管尚在内存中的调用。
普通函数的运行状态不能跨进程恢复，未知状态不会被自动重放。
原有自定义 HTTP/MCP 任务协议已移除；跨进程恢复远程长任务需要后端持久化任务状态，
并在原生工具函数中实现提交、查询或重新连接，单纯注册工具不会自动获得这一能力。


