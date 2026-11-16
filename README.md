# 采购三方勾稽报告

面向真实数据复核的中型数据工程工具，包含六个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18218。测试：docker compose exec -T app pytest -q

## 模块

- M1 单据采信：页面 /documents，接口 /api/documents/import
- M2 主数据映射：页面 /master，接口 /api/master/resolve
- M3 数量勾稽：页面 /quantity，接口 /api/quantity/match
- M4 税额核验：页面 /tax，接口 /api/tax/check
- M5 多对多匹配：页面 /matching，接口 /api/matches/solve
- M6 差异批次：页面 /batches，接口 /api/reconcile

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。


## 税额约定（M4）

- 供应商约定在 `/tax` 页面切换：逐行税额（`line_by_line`，默认）或整票净额舍入（`whole_invoice`）。
- 发票登记时绑定当时的约定版本与净额依据；切换只影响新发票，旧票仍按原约定复核。
- 同票按税率分组核算，混用约定整票拒收；旧凭据迁移中断可恢复，证据不足时拒绝猜算。
- 接口：`/api/tax/supplier/convention`、`/api/tax/invoices/import-csv`、`/api/tax/invoices/{supplier}/{doc_no}/review`、`/api/tax/legacy/import`、`/api/tax/legacy/migrate`。
