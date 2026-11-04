# Z-Profile

个人项目源码与技术说明。当前主要项目是 **ZForge：多站点爬虫聚合工作台**。

ZForge 将独立爬虫接入统一的任务调度、账号池、工作流、SQLite 存储和 Web 分析界面。这个仓库提供可阅读、可本地运行和可测试的实现。

## 源码入口

| 内容 | 入口 |
|---|---|
| 项目介绍、启动方式与能力边界 | [ZForge](source/zforge/README.md) |
| 调度与基础内核 | [server/core](source/zforge/05_Web服务/server/core/) |
| 数据存储与项目隔离 | [server/store](source/zforge/05_Web服务/server/store/) |
| Flask 应用与接口 | [app.py](source/zforge/05_Web服务/server/app.py) |
| 站点采集器与扩展模板 | [01_网页爬虫](source/zforge/01_网页爬虫/) |
| 前端界面 | [06_前端](source/zforge/06_前端/) |
| 回归测试 | [07_工具](source/zforge/07_工具/README.md) |
| 架构、接口和安全管理 | [项目文档](source/zforge/00_项目管理/) |

## 可检查的实现

- **采集器契约与任务调度**：通过 `META` / `run(ctx)` 接入采集器，统一处理参数校验、进度、取消和结果入库。
- **账号池**：站点与用途池隔离、成员共享、独占租约、额度和冷却；登录凭据与账号资料分开管理。
- **工作流与恢复**：编排串行任务，保存任务和工作流记录；重启后标记中断任务，尚不支持自动断点续跑。
- **数据边界**：平台和数据集分开读取，历史数据标注来源；SQL 查询使用只读连接和 SQLite authorizer。
- **实时交互**：SSE 推送日志和进度，原生 JavaScript 页面提供数据筛选、图表和导出。

内核、服务、采集器与前端共同组成当前实现；它们均位于 `source/zforge/`。本仓库保留此前提交历史。

## 本地运行

建议使用 Python 3.13。在项目目录内安装依赖并启动：

```bash
cd source/zforge
python -m pip install -r 05_Web服务/requirements.txt
python 05_Web服务/run.py
```

Windows 也可双击 `source/zforge/启动.bat`。浏览器采集和网页登录需要本机 Edge；外站采集需要网络，淘宝账号需自行授权。

公开源码不附带账号、Cookie、数据库或采集结果。首次使用从空数据开始；缺少历史数据库时不会附带旧样本。该应用依赖本地后端，尚未提供独立静态演示版。

## 验证

在 `source/zforge/` 执行：

```bash
python -m unittest discover -s 07_工具 -p "test_*.py"
node 07_工具/test_frontend.cjs
```

2026-11-04 本地验证：46 项后端测试与 6 组前端检查通过。测试使用临时库、测试凭据和模拟浏览器；这些结果不代表目标网站当前可采集或账号登录有效。手动真实采集脚本的区别见[测试说明](source/zforge/07_工具/README.md)。

## 仓库分层

```text
source/zforge/       当前项目源码、测试、文档与必要前端依赖
legacy/showcase/     早期图文展示，仅作历史归档
```

旧图文入口：[历史展示](legacy/showcase/README.md)。历史截图不代表当前版本的界面或能力。

本机数据通过发布目录物理排除与 `.gitignore` 双重隔离；日志和账号资料的生成位置见[安全管理说明](source/zforge/00_项目管理/安全与公开仓库管理.md)。
