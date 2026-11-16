# 深度登记 采购三方勾稽报告

- 规模：中型
- 核心规则：采购、分批验收和合并发票按货号与税率分组，数量和金额使用 Decimal 守恒。
- 使用者与工作流：应付会计导入三类单据，映射主数据，核对数量税额，多对多匹配并导出差异。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 单据采信 | 导入三类单据并隔离冲突 | POST /api/documents/import | `app/modules/m1_documents/parser.py` `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py` |
| M2 主数据映射 | 归一货号与旧字段 | POST /api/master/resolve | `app/modules/m2_master/aliases.py` `app/modules/m2_master/evolve.py` | `tests/test_m2_master.py` |
| M3 数量勾稽 | 统一包装单位并核对未到量 | POST /api/quantity/match | `app/modules/m3_quantity/units.py` `app/modules/m3_quantity/match.py` | `tests/test_m3_quantity.py` |
| M4 税额核验 | 按供应商约定重算净额、税额与含税额，导入真实发票并回看差异 | POST /api/tax/check · /api/tax/invoices/import-csv · /api/tax/supplier/convention | `app/modules/m4_tax/calculate.py` `app/modules/m4_tax/conventions.py` `app/modules/m4_tax/ledger.py` `app/modules/m4_tax/importer.py` | `tests/test_m4_tax.py` `tests/test_m4_tax_conventions.py` `tests/test_m4_tax_api.py` |
| M5 多对多匹配 | 在相同货号税率内组合匹配 | POST /api/matches/solve | `app/modules/m5_matching/candidates.py` `app/modules/m5_matching/solver.py` | `tests/test_m5_matching.py` |
| M6 差异批次 | 核对金额守恒并导出差异 | POST /api/reconcile | `app/modules/m6_batches/compare.py` `app/modules/m6_batches/exporter.py` | `tests/test_m6_batches.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-05 | 单据缺少字段 | 静默补空进入匹配 | 隔离并保留行号 | `app/modules/m1_documents/parser.py` | `tests/test_m1_documents.py::test_missing_field_quarantined` |
| TRAP-06 | 金额带千分位 | 直接转换失败 | 清理分隔符后使用 Decimal | `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py::test_thousands_amount_uses_decimal` |
| TRAP-07 | 空值哨兵 | 被当作零金额 | 空值集合统一拒绝 | `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py::test_missing_field_quarantined` |
| TRAP-08 | 供应商编码前导零 | 转整数后主数据失配 | 编码始终按字符串保形 | `app/modules/m2_master/aliases.py` | `tests/test_m2_master.py::test_supplier_code_preserves_leading_zero` |
| TRAP-10 | 税额浮点尾差 | 分项和总额差一分 | Decimal 半入到分 | `app/modules/m4_tax/calculate.py` | `tests/test_m4_tax.py::test_half_up_to_cent` |
| TRAP-14 | 重复单号值冲突 | 后写覆盖金额 | 单独隔离冲突版本 | `app/modules/m1_documents/parser.py` | `tests/test_m1_documents.py::test_conflicting_duplicate_detected` |
| TRAP-17 | 旧列名仍在使用 | 新版读取报错 | 受控字段迁移并报告冲突 | `app/modules/m2_master/evolve.py` | `tests/test_m2_master.py::test_old_columns_migrate` |
| TRAP-19 | 组合候选顺序变化 | 匹配结果随输入顺序变化 | 按差额、数量与编号稳定择一 | `app/modules/m5_matching/solver.py` | `tests/test_m5_matching.py::test_choice_is_order_independent` |
| TRAP-23 | 相邻税率边界 | 不同税率金额互相抵消 | 货号与税率共同分组 | `app/modules/m4_tax/verify.py` | `tests/test_m4_tax.py::test_different_tax_rates_never_offset` |
| TRAP-26 | 同批重复执行 | 重复新增对账结果 | 输入摘要作为运行唯一键 | `app/store.py` | `tests/test_m6_batches.py::test_idempotent_record` |
| TRAP-31 | 供应商切换整票计税 | 统一重算改掉已登记旧税凭据 | 发票登记时快照约定版本与净额依据，旧票按原约定复核 | `app/modules/m4_tax/ledger.py` | `tests/test_m4_tax_conventions.py::test_switch_only_affects_new_invoices` |
| TRAP-32 | 整票净额舍入 | 逐行先舍入再汇总，含税额差一分 | 同税率分组后先舍净额再算税额 | `app/modules/m4_tax/conventions.py` | `tests/test_m4_tax_conventions.py::test_whole_invoice_rounds_net_before_tax` |
| TRAP-33 | 同票混用约定 | 静默按一种约定计算 | 每行声明归并，混用整票拒收 | `app/modules/m4_tax/conventions.py` `app/modules/m4_tax/importer.py` | `tests/test_m4_tax_conventions.py::test_mixed_conventions_on_one_invoice_rejected` |
| TRAP-34 | 旧凭据证据不足 | 缺失净额补零后猜算 | 拒绝迁移并保留原因码 | `app/modules/m4_tax/ledger.py` | `tests/test_m4_tax_conventions.py::test_legacy_without_evidence_is_rejected_not_guessed` |
| TRAP-35 | 迁移中断 | 重跑覆盖或重复登记 | 逐条独立事务，未完成者保持 pending 可恢复，已迁移幂等 | `app/modules/m4_tax/ledger.py` | `tests/test_m4_tax_conventions.py::test_legacy_migration_resumes_after_interruption` |
| TRAP-36 | 并发切换约定 | 在途发票按切换后约定入账 | 登记与切换同用写事务，按接收版本绑定 | `app/modules/m4_tax/ledger.py` | `tests/test_m4_tax_conventions.py::test_in_flight_registration_keeps_original_version_under_switching` |
| TRAP-37 | 重试导入 | 同票重复登记 | 内容指纹判定，重复返回首次结果 | `app/modules/m4_tax/ledger.py` | `tests/test_m4_tax_api.py::test_retry_import_is_idempotent` |

## 对账规则

- 采购数量 = 净验收数量 + 未到数量。
- 净验收含税额 = 已开票含税额 + 未开票金额 + 已解释差异。
- 不同税率的行不可相互抵消。

## 计税约定

- 约定按供应商维护为只追加的版本历史：`line_by_line`（逐行净额与税额各自半入到分）与 `whole_invoice`（按税率分组汇总是净额依据，整组净额半入到分后再计税）。
- 发票在登记（接收）时绑定当时的约定版本与净额依据；之后供应商切换只影响新发票。
- 同一发票所有行必须声明同一约定，混用整票拒收；行声明的约定也必须与绑定约定一致。
- 旧凭据迁移一律按原逐行税额复核（版本 0），不受当前供应商约定影响；净额依据不足时拒绝猜算并保留原因码。
- 税额页的“回看差异”仅计算当前约定下的假设金额，不写回、不改变原凭据。

## 已知不覆盖

- 不处理外币汇兑，样例与输出均为人民币。

