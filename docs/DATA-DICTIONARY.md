# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。
- convention：供应商计税约定，`line_by_line`（逐行税额）或 `whole_invoice`（整票净额舍入）。
- policy_revision：供应商约定版本号；0 表示从未配置的默认约定或迁移前的旧凭据版本，切换后从 1 递增。
- expected_revision：调用方接收发票时看到的约定版本；登记时据此绑定，不随之后的切换改变。
- net_basis：发票接收时留档的净额依据，含约定、舍入方式与各税率分组的净额、税额、含税额及来源行号。
- basis_source：每行净额的依据来源，`net_amount` 或 `quantity_unit_price`；两者皆缺时不猜算。
- source：发票来源，`import` 为页面/接口导入，`legacy` 为旧凭据迁移。
- fingerprint：供应商、发票号、含税额与规范化行内容的 SHA-256，用于识别重复登记。
- legacy state：`pending` 待迁移、`migrated` 已迁移、`rejected` 证据不足或冲突拒绝；`rejected` 仅在原始内容更正后重新排队。
- 数值序列化：净额、税额、含税额与差异均为字符串；差异按分半入后再输出。

