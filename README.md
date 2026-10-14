# 60 周线预警系统

A 股与国内期货的 60 周线上穿预警。系统每日多次扫描全市场，向微信推送触发清单，并在线上看板展示明细。

线上看板：https://ma60-alert-dashboard.app.workbuddy.host/

## 系统做什么

- 扫描全部 A 股。判定口径：上周收盘 < 60 周线，且现价 ≥ 60 周线。
- 扫描 82 个期货品种的持仓量加权指数。判定分两层：突破（上穿）与临近（偏离在 ±3% 内）。
- 每条推送带周 K 图、量比、市值分层与回测参照。
- 看板展示触发明细、数据健康度、推送额度与运行流水。列表支持排序。

## 数据流

1. baostock 提供日线（后复权）。腾讯行情兜底。新浪期货提供合约日线与持仓量。
2. 系统把日线聚合为周线。只保留已完成周。
3. 云端定时任务（GitHub Actions，北京时间）驱动三段流程：prepare 判定出图 → 提交 → notify 推送。
4. 看板读 GitHub 仓库里的 JSON 文件。数据文件由每次运行刷新。

## 目录结构

```
src/alert/        A 股预警：快照、行情、判定、推送、图表
src/fut/          期货预警：加权指数合成、82 品种清单、分层判定
src/dashboard/    看板数据构建（data.json / fut.json 等）
dashboard/        前端静态站（发布目录）
store/            云端数据仓（周线、状态、期货仓）
config/           配置。alert_config.local.yaml 存密钥，不入库
output/_uicheck/  回归与渲染验证脚本
charts/           推送用周 K 图（按日存放）
```

## 本地运行

先装依赖：`pip install -r requirements.txt`（如无则按 import 安装：pandas numpy requests baostock matplotlib pyyaml）。

```bash
python -m alert.run_alert --scan     # 盘中扫描一次
python -m alert.run_alert --update   # 日线增量更新 + 快照重建
python -m alert.run_alert --demo     # 离线演示，只打印不推送
```

命令需在仓库根目录执行，且 `PYTHONPATH` 含 `src`。本地状态写到 `output/alert/`。

## 云端运行

云端任务由 `.github/workflows/alert.yml` 定义。周一至周五：

- 10:05 / 11:05 / 14:05 / 14:55 盘中扫描
- 15:15 收盘确认
- 16:07 数据增量更新

云端需要两个 Secrets：`ALERT_SENDKEY`（Server酱）、`GITHUB_TOKEN`（内置）。云端状态写到 `store/state/`。

## 测试

```bash
python output/_uicheck/test_e2e_v140.py    # 端到端回归，60 项断言
node output/_uicheck/verify_v150.js        # 前端排序与渲染，33 项断言（需本地静态服务）
```

改完代码先跑这两个脚本。全部通过才算完成。

## 版本规则

版本号三段：主.次.修。修 bug 加修订号。加功能加次版本。架构重写加主版本。每次改动必须递增 `dashboard/app.js` 的 `APP_VERSION`，并同步 `CHANGELOG.md`。

## 写作规范（ASD-STE100 精神中文版）

本项目所有注释、文档、界面文案、推送文案遵守以下规则：

1. 写短句。一句只说一件事。
2. 用主动语态。
3. 一个概念只用一个词。按下表取词。
4. 指令用祈使句。直接写「先跑测试」，不写「建议可以先跑一下测试」。
5. 不用模糊词（可能、大概、一些）。要么给出数字，要么删掉。

### 术语表

| 概念 | 统一用词 | 禁用 |
|---|---|---|
| 价格相对 60 周线的差距 | 偏离 | 乖离 |
| A 股最新成交价 | 现价 | 最新价、当前价 |
| 期货品种的最新加权价 | 最新加权价 | 当前加权价、当前价 |
| 60 周简单移动平均 | 60 周线 | 60 周均线、MA60（代码标识符除外） |
| 周线级别首次上穿事件 | 触发 | 信号、命中 |
| 加权指数合成口径 | 持仓量加权 | 其他说法 |

新增概念先入表，再使用。改词先全库替换。
