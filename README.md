# 多租户湖表的列级投影与快照访问边界

多租户学习活动事实的授权投影、脱敏快照和版本清单基线。

仓库内的实现使用 Python 3.11 标准库和 SQLite，数据只写入命令行指定的本地数据库及导出目录。`lakeapp.storage` 保存原始接收、业务状态、合同版本、脱敏规则版本和导出运行记录；`lakeapp.contract` 负责输入约定；`lakeapp.ingest` 接收来源批次；`lakeapp.derive` 生成当前基线支持的事实；`lakeapp.policy` 负责合同解析、行范围、列授权和脱敏；`lakeapp.publish` 生成并回读租户快照；`lakeapp.query` 提供核对视图；`lakeapp.cli` 提供本地操作入口。

## 运行

在 Python 3.11 环境中执行 `python3 -m unittest discover -s tests -v` 运行测试，执行 `python3 -m compileall -q lakeapp tests` 检查构建和语法。示例数据是本地合成记录，不依赖外部服务或联网。

## 交付边界

合作机构从同一份事实表出发，各自按合同得到不同的列、行范围和脱敏方式。导出先在内存中解析合同、筛选行、投影列并脱敏，再把结果原子写入 `导出目录/<租户>/v<版本>.jsonl`；未获授权的列不会先落盘再过滤。

- **列**：合同版本记录授予列。请求列必须全部在授予列内，越权列直接阻断，不会静默裁掉后交付。`source`、`payload`、`source_batch`、`version` 等控制列永远不能授予。
- **行**：合同必须写明行范围（来源、业务键、时间窗、`where` 等值条件），或用 `{"all": true}` 显式声明整表；省略条件会报错，避免"少写一个过滤条件"变成全量交付。
- **脱敏**：每个合同版本固定一个脱敏规则版本。规则版本注册后不可修改；支持 `redact`、`hash`、`partial`、`email`，另有 `protected` 列永远以占位符交付。脱敏后逐行复核，失败即阻断。
- **版本**：合同换版只能追加，版本号与生效时间必须递增，脱敏规则版本不允许回退。新导出使用当时生效的最新合同版本，不会复用旧授权；显式指定 `contract_version` 也只能确认当前生效版本。

## 合同生命周期

合同版本带有 `effective_at`、`expires_at` 和 `revoked_at`。合同失效或被撤销后，新的导出一律阻断并留下记录；已生成快照不受影响，仍然可以按版本读取和核对。历史快照把生成时的合同条款、脱敏规则和校验和固化在自身记录里，后续换版、撤销或新增脱敏规则都不会改变旧快照的含义。

## 阻断与审计

每次导出（无论成功还是失败）都写入 `export_runs`：运行的租户、状态、原因、请求列与生效列、合同/脱敏规则版本、行范围、行数、校验和、快照路径和时间。缺少合同、合同失效、越权列、脱敏失败、写入失败都会形成可查询的 `blocked` 记录，不会用空行或空值伪装成成功。`snapshot_path` 为空表示没有落盘文件。

## 命令行

```
python3 -m lakeapp.cli <数据库> masking    '{"version":1,"rules":{"email":"email"},"protected":["cohort"]}'
python3 -m lakeapp.cli <数据库> contract   '{"tenant":"partner-a","version":1,"columns":["business_key","value","email"],"row_scope":{"sources":["enrollment"]},"masking_version":1,"effective_at":"2026-11-01T00:00:00"}'
python3 -m lakeapp.cli <数据库> revoke     '{"tenant":"partner-a","version":1,"revoked_at":"2026-11-11T00:00:00"}'
python3 -m lakeapp.cli <数据库> export     '{"tenant":"partner-a","export_dir":"./out","as_of":"2026-11-10T00:00:00"}'
python3 -m lakeapp.cli <数据库> audit      '{"tenant":"partner-a"}'
python3 -m lakeapp.cli <数据库> run        '{"run_id":2}'
python3 -m lakeapp.cli <数据库> scope      '{"tenant":"partner-a","version":1}'
python3 -m lakeapp.cli <数据库> explain    '{"tenant":"partner-a","version":1}'
python3 -m lakeapp.cli <数据库> diff       '{"tenant":"partner-a","left":1,"right":2}'
python3 -m lakeapp.cli <数据库> contracts  '{"tenant":"partner-a"}'
python3 -m lakeapp.cli <数据库> maskings   '{}'
python3 -m lakeapp.cli <数据库> tenant     '{"tenant":"partner-a"}'
```

`export` 在阻断时以非零状态退出。`scope`/`explain` 给出指定版本实际交付的列、行范围、行数、生成依据（合同条款、脱敏规则、生效状态）和完整性校验；`diff` 给出合同版本、列、行范围、脱敏规则和行数的变化，说明规则变更对后续版本的影响。`receive`、`facts`、`progress`、`publish`、`read` 保持原有的接收、推导和非租户版本清单行为。

## 现有契约

输入批次包含来源名、批次标识、游标和记录数组。原始记录以 JSON 保存，重复批次不会再次产生事实；业务键相同但内容不同的记录会保留冲突信息。记录中未声明的额外字段会随事实保存，供合同按列授权后投影。事实查询按业务日期和版本清单读取，清单中的文件摘要与行数用于发现导出内容被替换。数据库初始化由运行入口完成，已有数据不会因重新打开而丢失。
