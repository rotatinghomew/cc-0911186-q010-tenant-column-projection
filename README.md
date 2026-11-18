# 多租户湖表的列级投影与快照访问边界

多租户学习活动事实的授权投影、脱敏快照和版本清单基线。

仓库内的实现使用 Python 3.11 标准库和 SQLite，数据只写入命令行指定的本地数据库及导出目录。`lakeapp.storage` 保存原始接收、业务状态和版本清单；`lakeapp.contract` 负责输入约定；`lakeapp.ingest` 接收来源批次；`lakeapp.derive` 生成当前基线支持的事实；`lakeapp.publish` 读取并写出可追溯版本；`lakeapp.cli` 提供本地操作入口。

## 运行

在 Python 3.11 环境中执行 `python3 -m unittest discover -s tests -v` 运行测试，执行 `python3 -m compileall -q lakeapp tests` 检查构建和语法。示例数据是本地合成记录，不依赖外部服务或联网。

## 现有契约

输入批次包含来源名、批次标识、游标和记录数组。原始记录以 JSON 保存，重复批次不会再次产生事实；业务键相同但内容不同的记录会保留冲突信息。事实查询按业务日期和版本清单读取，清单中的文件摘要与行数用于发现导出内容被替换。数据库初始化由运行入口完成，已有数据不会因重新打开而丢失。

## 目录职责

`lakeapp/storage.py` 负责事务和表结构，`lakeapp/contract.py` 负责记录约束，`lakeapp/ingest.py` 负责接收和来源进度，`lakeapp/derive.py` 负责从原始记录生成事实，`lakeapp/publish.py` 负责版本清单与回读，`lakeapp/cli.py` 负责把这些业务操作组合给本地使用者。测试数据覆盖正常接收、重复批次和版本读取。
