# YNAB 数据自动备份系统

## 简介

本项目用于将 YNAB 预算数据自动备份到 GitHub 私有仓库，作为额外归档手段，补足 YNAB 官方仅保留 5 年历史数据的限制。

## 架构

本方案采用双仓库设计：

- **公共仓库**：存放代码、Workflow、恢复脚本与说明文档。
- **私有仓库**：存放每日备份产生的 JSON 与 CSV 数据。

这样设计的原因：代码仓库可以公开分享与版本管理，不暴露个人财务数据；数据仓库保持私有，避免预算、账户、交易明细泄露。Workflow 只需要通过 `GITHUB_PAT` 向私有仓库写入，不需要把敏感数据提交到代码仓库。

## 前置准备

### 1. 获取 YNAB Personal Access Token

1. 打开 YNAB 网站并登录
2. 点击右上角头像
3. 进入 `Account Settings`
4. 打开 `Developer Settings`
5. 创建并复制 `Personal Access Token`

### 2. 创建 GitHub PAT

创建一个 GitHub Personal Access Token，并赋予 `repo` scope，用于向私有数据仓库写入备份文件。

### 3. 创建私有数据仓库

创建一个新的 GitHub 私有仓库作为数据库仓库，例如 `yourname/ynab-data`。

> **注意**：该私有仓库必须先有一次初始提交（可以只提交一个空的 `README.md`），确保默认分支已经创建完成，否则 API 写入会报 422 错误。

## 配置 Secrets

在代码仓库的 `Settings → Secrets and variables → Actions` 中添加以下 Secrets：

| Secret 名称 | 说明 |
|-------------|------|
| `YNAB_TOKEN` | YNAB Personal Access Token |
| `DATA_REPO_PAT ` | 拥有 `repo` 权限的 GitHub PAT |
| `DATA_REPO` | 私有数据仓库名，格式为 `owner/repo` |

## 手动触发

除了定时任务外，你也可以手动触发备份：

1. 打开 GitHub 仓库的 `Actions` 页面
2. 选择 `YNAB Backup` 工作流
3. 点击 `Run workflow`
4. 按需设置 `full_backup`

`full_backup` 的作用：
- `false`（默认）：增量备份，使用上次记录的 `server_knowledge` 只拉取变化数据
- `true`：全量备份，忽略历史状态，重新拉取预算完整数据（首次运行或数据异常时使用）

## 备份数据说明

### JSON 格式

路径示例：`budgets/<budget_id>/full/2026-04-15.json`

- 保存 YNAB API 的原始响应，包含预算下所有数据（账户、分类、收款方、交易、计划交易等）
- 金额字段为 `milliunits`（÷1000 为实际金额，如 `-25000` = `-¥25.00`）
- 适合后续编程分析、二次处理或重新生成导入文件

### CSV 格式

路径示例：`budgets/<budget_id>/transactions_csv/2026-04-15.csv`

- CSV 列格式与 YNAB 原生导出格式保持一致，可直接导入 YNAB
- 列：`Account, Flag, Date, Payee, Category Group/Category, Category Group, Category, Memo, Outflow, Inflow, Cleared`
- 文件使用 UTF-8 BOM 编码，Excel 可正确显示中文

## 数据恢复

### 可自动恢复

- **交易记录**：`restore.py` 会读取备份 CSV，并通过 YNAB API 批量导入（每批 50 条，使用 `import_id` 防重复）
- **收款方**：在导入交易时由 YNAB 自动创建

### 需要手动处理

- **分类**：YNAB API 不支持创建分类，必须先手动重建分类结构，脚本会打印完整分类列表供参考
- **账户**：先在目标预算中手动创建同名账户，脚本会打印账户列表
- **预算目标、计划交易**：需手动设置

### 使用 restore.py

先演练，查看将恢复的内容（不实际导入）：

```bash
python restore.py \
  --data-repo yourname/ynab-data \
  --github-token ghp_xxx \
  --ynab-token ynab_xxx \
  --budget-id your_budget_id \
  --date 2026-04-15 \
  --dry-run
```

正式导入：

```bash
python restore.py \
  --data-repo yourname/ynab-data \
  --github-token ghp_xxx \
  --ynab-token ynab_xxx \
  --budget-id your_budget_id \
  --date 2026-04-15
```

> 如果不传 `--date`，脚本会自动选择最新备份日期。

## 调度频率

定时任务定义在 `.github/workflows/backup.yml` 中：

```yaml
cron: '0 2 * * *'   # 每天 UTC 02:00（北京时间 10:00）
```

常用频率示例：
- 每天一次：`0 2 * * *`
- 每 12 小时一次：`0 */12 * * *`
- 每周一：`0 2 * * 1`

## 故障排查

### 401 Unauthorized

- `YNAB_TOKEN` 已失效或复制错误 → 重新生成并更新 Secret
- `GITHUB_PAT` 无效或缺少 `repo` 权限 → 重新生成 PAT
- Secret 名称拼写不正确 → 检查 `Settings → Secrets` 中的名称是否完全匹配

### 429 Too Many Requests

- 触发 YNAB API 限流（上限 200 请求/小时） → 脚本已内置指数退避重试
- 如仍频繁触发，请减少手动触发次数，避免同时运行多个备份任务

### 422 Unprocessable Entity

- 导入交易时账户不存在 → 先手动创建同名账户
- GitHub API 写入时私有库不存在或无初始提交 → 确认 `DATA_REPO` 正确，私有库有初始 commit
- 私有库分支名不是 `main` → 修改 workflow 中的 `DATA_REPO_BRANCH` 变量
