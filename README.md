# HEAT RADAR

HEAT RADAR 是面向 A 股的热度分析与事件追踪工具，集成人气榜、财经资讯、公告和公司资料，提供候选排序、题材关系分析及历史观测管理。

应用在本地运行，通过浏览器访问。采集数据、配置和分析结果存储于本机，事件时间与采集时间分别记录，分析结果按运行时点保存。

## 功能

| 模块 | 功能 |
|---|---|
| 板块与个股 | 候选排序、股票检索、证据详情、观察清单及结果导出 |
| 题材发现与传播 | 新词发现、题材成员分析及业务关系图 |
| 量化研究实验室 | 后续标签更新、模型训练、实验结果及模型管理 |
| 盘前海外映射 | 海外信息与国内题材、公司之间的关联分析 |
| 数据来源与接入 | 来源配置、采集状态、凭据管理及 JSON / JSONL 导入 |
| 历史与检验 | 历史快照、数据回填、名单后续表现及数据库备份 |
| 参数与主题 | 筛选条件、关注清单、主题词及业务关系配置 |

## 安装

### Windows

下载仓库压缩包并解压，或克隆仓库：

```powershell
git clone https://github.com/phuongdung160114-ux/heat-radar.git
cd heat-radar
```

| 操作 | 入口 |
|---|---|
| 安装 | `一键安装_Windows.cmd` |
| 启动 | `启动热度雷达_Windows.cmd` |
| 修复依赖 | `修复依赖_Windows.cmd` |

安装程序支持 Windows x64，自动检测 Python 3.11–3.13，并安装项目依赖。缺少兼容版本时，从 Python 官网下载独立运行环境。安装完成后打开应用页面，并创建 `Ashare Heat Radar` 桌面快捷方式。

### 手动安装

环境要求：Python 3.11–3.13。以下 PowerShell 命令使用 Python 3.13：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

### macOS 与 Linux

使用仓库内的安装脚本：

```bash
bash installers/install_unix.sh
```

环境要求为 Python 3.11–3.13。Linux 需安装 venv 模块，macOS 上的 LightGBM 依赖 OpenMP。

## 启动参数

默认访问地址：`http://127.0.0.1:8787`。端口占用时自动选择后续可用端口，实际地址显示于运行窗口。

| 参数 | 作用 |
|---|---|
| `--port 8899` | 指定起始端口 |
| `--no-auto` | 关闭自动采集 |
| `--data-dir "D:\HeatRadarData"` | 指定数据目录 |
| `--no-browser` | 关闭浏览器自动启动 |

示例：

```powershell
.\.venv\Scripts\python.exe run.py --port 8899 --data-dir "D:\HeatRadarData"
```

命令窗口中的 `Ctrl+C` 用于停止程序。关闭浏览器页面不影响后台采集。

## 采集与分析

在“数据来源与接入”中启用所需来源并配置凭据。各来源独立记录采集状态、更新时间和错误信息。

| 操作 | 行为 |
|---|---|
| 采集并筛选 | 获取最新数据后生成分析结果 |
| 更新筛选 | 基于已有数据重新计算 |
| 测试已启用来源 | 执行已启用来源的采集与筛选 |
| 回填可得历史 | 导入来源提供的历史记录 |
| 更新后续标签 | 根据后续观测更新研究标签 |

盘中观测与历史对比数据通过持续采集积累。默认排序采用规则分位数，训练并启用模型后使用对应模型排序。研究实验支持逻辑回归、岭回归和 LambdaMART，使用成熟标签进行训练、验证和测试。

## 数据来源

公开数据接入东方财富、雪球、财联社、互动易等来源，主要通过 AKShare 获取。

东方财富人气榜和个股概况采用独立适配。行情请求断连时自动切换东方财富备用入口，并记录实际来源；人气排名与股票名称补充分开处理。

Tushare、X、Reddit 和外部数据服务通过各自凭据启用。RSS 订阅和本地文件目录在高级配置中设置。

## 数据存储

| 安装方式 | 默认数据目录 |
|---|---|
| Windows 一键安装，项目内无 `.venv` | `%LOCALAPPDATA%\AshareHeatRadar\data` |
| 手动安装或使用项目内 `.venv` | `data/` |
| 使用 `--data-dir` | 参数指定目录 |

数据目录内的文件：

| 路径 | 内容 |
|---|---|
| `live/radar.sqlite` | 观测数据、业务关系和分析结果 |
| `settings.json` | 应用配置及来源凭据 |
| `logs/app.log` | 运行日志 |

Windows 默认安装日志位于 `%LOCALAPPDATA%\AshareHeatRadar\logs`；项目内环境的安装日志位于 `data/logs`。

数据库备份入口位于“历史与检验”。迁移配置时单独复制 `settings.json`。

## 故障排查

| 现象 | 处理方式 |
|---|---|
| 启动失败 | 执行 `修复依赖_Windows.cmd`，检查安装日志和运行日志 |
| 浏览器未自动打开 | 访问运行窗口显示的本地地址 |
| 数据来源采集失败 | 检查该来源的错误信息、凭据和连接状态 |
| 研究结果未生成 | 检查观测日期数量及后续标签是否成熟 |
| 日期超出日历范围 | 按交易所安排更新 `config/calendar.json` 与 `tools/build_calendar.py`；内置范围为 2025-01-01 至 2026-12-31 |

提交问题时附上操作系统、Python 版本、来源名称、复现步骤和错误信息。

## 项目结构

```text
config/        默认配置、交易日历、主题词及业务关系
installers/    安装与启动脚本
radar/         采集、存储、分析及网页接口
static/        前端页面与资源
tools/         导入、维护及研究命令
run.py         应用入口
```

## 许可证

[MIT](LICENSE)。第三方依赖遵循各自许可证。
