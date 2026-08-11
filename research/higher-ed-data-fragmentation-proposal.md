# 高校部门数据碎片化：领域数据产品与整理层提议

## 结论

把招生、教务、助学金、LMS、学生支持等部门数据库看成独立服务数据库，这个出发点是对的。它明确了业务所有权，也承认各部门的发布节奏、术语和技术栈不同。

但 database per service 不能推导出“分析系统直接连接每个部门的业务库”。直接跨库查询会把可用性、权限、schema 变化、时间差和身份键差异同时暴露给每个下游团队。更可行的目标是：

1. 部门继续拥有业务数据库和本领域事实。
2. 每个部门向学校数据平台发布版本化的 source-aligned data product。
3. 学校级平台负责身份映射、契约检查、目录、访问策略、质量观测和历史保存。
4. 跨部门分析通过 conformed core 或用途型 data product 完成，不回查业务库拼接。
5. 治理由中央规则和领域 steward 共同承担，既不是完全中心化，也不是各部门自行其是。

这是一种混合式 data mesh：领域所有权来自微服务，跨域整理层保留数据仓库或湖仓擅长的历史、校准和批量分析能力。

## 为什么不是纯微服务式数据访问

[Laigner 等人的微服务数据管理研究](papers/01-laigner-2021-data-management-in-microservices.pdf)显示，database-per-service 带来的主要困难不是数据库类型选择，而是跨服务一致性、查询、事务拆分和数据传播。[Helland](papers/04-helland-2007-life-beyond-distributed-transactions.pdf)进一步说明，大范围分布式事务不是可靠的默认方案；系统需要用消息、幂等操作、版本和补偿处理长期运行的跨域流程。

高校场景会把这些问题放大：

- 一个“学生”在招生系统、SIS、助学金、LMS 和 CRM 中可能有不同 ID。
- “在读”“注册”“active”“withdrawn”等状态由不同部门在不同时间确认。
- 业务库通常只保留当前状态，而机构研究需要按学期重现历史。
- 同一字段可能有业务含义不同的空值、默认值或代码表。
- 数据访问不仅取决于角色，还取决于用途、学生群体、敏感等级和保留期限。
- SaaS 产品升级可能在没有通知分析团队的情况下改变字段或导出格式。

高校研究也直接报告了 silo、质量不一致、数据文化与安全能力不足等问题，见[中国高校数据治理系统综述](papers/05-shen-2025-chinese-higher-ed-data-governance-review.pdf)和[高校 Linked Data 互操作案例](papers/06-garcia-juanes-2014-linked-data-higher-education.pdf)。EDUCAUSE 的高校数据治理行动计划同样把 persistent identifiers、统一字典、目录、MDM/RDM、隐私和明确 steward 列为基础能力。

## 推荐架构

~~~mermaid
flowchart LR
    subgraph D["部门业务域"]
      A["招生 / CRM"]
      R["教务 / SIS"]
      F["助学金"]
      L["LMS"]
      S["学生支持 / Advising"]
    end

    A --> I["Outbox / CDC / 版本化快照"]
    R --> I
    F --> I
    L --> I
    S --> I

    I --> P["Source-aligned data products\n数据 + schema + 语义 + SLO + owner"]
    P --> C["学校自助数据平台\n契约检查、目录、质量、lineage、策略"]
    C --> X["Identity & reference data\ncanonical ID + crosswalk + code sets"]
    X --> K["Conformed core\nPerson / Student / Term / Course / Enrollment / Award"]
    K --> M["用途型 data products\n留存、干预、合规报送、科研"]
    M --> U["BI / SQL / ML / Text-to-SQL"]

    G["联邦治理委员会\n中央规则 + 领域 steward"] -.-> P
    G -.-> C
    G -.-> X
    G -.-> K
~~~

### 三类数据产品

| 类型 | 谁负责 | 内容 | 允许做什么 |
|---|---|---|---|
| Source-aligned | 领域部门 | 贴近业务事件和快照，保留来源 ID 与来源语义 | 追溯、重放、领域内部分析 |
| Conformed | 学校数据平台与领域共同负责 | canonical identity、学期、课程、注册、奖助等跨域核心实体 | 稳定跨域 join、历史分析、质量对账 |
| Consumer-aligned | 具体用例团队 | 留存干预、财务报告、认证报送、研究 cohort | 只暴露用途需要的字段和粒度 |

不要把 conformed core 做成新的万能业务系统。它是分析和交换边界，不接管部门交易，也不要求部门把内部 schema 改成统一模型。

## 部门边界与事实权威

| 领域 | 通常的 system of record | 应发布的数据产品 | 常见碎片化风险 |
|---|---|---|---|
| Identity / IAM | 人员主数据与账号目录 | canonical person ID、source ID crosswalk、合并/拆分事件 | 重复人员、改名、账号复用、错误合并 |
| Admissions | 招生 CRM | application、admit decision、intended program | applicant ID 尚未转 student ID、代码版本变化 |
| Registrar / SIS | 教务系统 | student、term、course、section、enrollment、completion | effective dating 不完整、撤课回写、学期定义不同 |
| Financial Aid | 助学金系统 | award、disbursement、eligibility status | 部门本地 ID、状态延迟、空值语义 |
| LMS | 学习平台 | activity、submission、grade event | 高频事件、课程映射、时区、机器人活动 |
| Advising / CRM | 学生支持系统 | case、appointment、intervention outcome | 自由文本、敏感信息、重复 case |
| HR / Faculty | 人力资源系统 | employment、appointment、organizational unit | person/student 双重身份、组织层级变化 |
| Research Admin | 科研管理系统 | proposal、award、compliance status | 项目级权限、跨机构 ID、保留期限 |

事实权威应按“字段 + 时间 + 用途”定义，而不是笼统规定某张表永远正确。例如 SIS 可以是正式注册状态的权威，LMS 只说明学生是否在课程空间有活动，二者发生冲突时不能简单取最新一条覆盖。

## 每个数据产品必须携带的契约

最小契约至少包括：

- product ID、版本、owner、steward 和支持渠道。
- 实体命名空间；每个 ID 明确是 canonical 还是 source-local。
- schema、字段定义、代码表版本、单位、时区和空值语义。
- event time、effective from/to、observed at、published at。
- 更新方式：append、upsert、snapshot 或 correction event。
- 唯一性、完整性、参照完整性和可接受延迟。
- 数据分类、允许用途、访问策略、保留与删除要求。
- 上游 lineage、质量结果和已知限制。
- 兼容性规则、弃用期和消费者清单。

契约应由机器检查。schema registry 只解决结构变化；业务定义、代码表和时间语义也必须版本化，否则“字段还在”仍可能产生静默错误。

高校交换格式可以优先映射到 [CEDS](https://ceds.ed.gov/)、[1EdTech Edu-API](https://www.1edtech.org/standards/edu-api) 和 [PESC](https://pesc.org/) 的已有概念，但这些标准不能替代本校的 system-of-record 决策、ID crosswalk 和用途控制。

## 身份整理

身份是跨部门整理的第一优先级，不应隐藏在每条分析 SQL 里。

推荐流程：

1. 保留每个来源的原始 ID 和 namespace，例如 registrar:S0001、financial-aid:88421。
2. 由 Identity Data Product 发布 canonical person ID 与 source ID 的带版本映射。
3. 精确匹配优先使用学校签发的稳定标识；概率匹配只处理没有可靠键的历史数据。
4. 合并和拆分都发布事件，不能原地覆盖历史。
5. crosswalk 按高敏感数据管理，普通分析者只获得已解析的 canonical ID，不获得匹配用身份属性。
6. 每次 join 记录 match method、confidence 和 mapping version，指标同时报告 unmatched、ambiguous 和 false-link 风险。

[Schnell 等人的 Bloom-filter PPRL 方法](papers/07-schnell-2009-privacy-preserving-record-linkage.pdf)可作为没有共同明文 ID 时的研究起点，但不能直接作为生产安全方案。[后续密码分析](papers/08-christen-2018-bloom-filter-cryptanalysis.pdf)证明基础 Bloom-filter 编码可能被重识别。生产设计需要独立隐私与安全评审，并优先减少需要概率链接的数据。

## 一致性与跨部门流程

- 部门交易只在本域数据库内提交。
- 可靠传播采用 transactional outbox、CDC 或带 manifest 的批量快照。
- 消费者按 event ID 幂等处理，保存 source version 和 ingestion time。
- 跨部门业务流程用 saga/状态机和明确补偿，不用跨所有部门数据库的 2PC。
- 分析层接受有界延迟，但必须显示 freshness watermark。
- 每日或每学期执行 reconciliation，将源系统总量、哈希、关键状态和 conformed 数据对账。
- 修正使用 correction event 或 bitemporal 记录，不能悄悄覆盖已经用于报表的历史。

## 治理组织

推荐四类责任：

- 领域 data owner/steward：定义业务含义、质量阈值和变更计划。
- Data platform team：提供 ingestion、catalog、contract testing、lineage、policy enforcement 和 observability。
- Identity/reference-data team：维护 canonical IDs、学期、组织、课程分类与代码表。
- Federated governance council：批准学校级规则、仲裁冲突、决定高风险用途和跨域产品。

[Data Mesh 综述](papers/02-goedegebuure-2023-data-mesh-review.pdf)支持领域所有权、data as a product、自助平台和联邦治理四个原则；[自助平台设计研究](papers/03-kumara-2024-self-serve-data-platform.pdf)也说明，去中心化只有在平台提供标准化“铺路”能力时才不会变成新的孤岛。因此不建议一开始为每个部门组建完整独立数据工程团队，先建设共享平台和少量领域 steward 更现实。

## 本项目的实验路线

当前基准已经覆盖四类变体和以下损伤：

- drop_row：来源记录缺失。
- null_aid_amount：关键金额为空。
- null_aid_status：关键状态为空。
- identifier_mismatch：助学金部门改用本地 ID。
- identity_crosswalk.csv：治理后的 canonical-to-local 映射。
- 直接 join 与 crosswalk-resolved query：比较“不整理”和“经过身份层整理”的结果差异。

下一阶段建议按优先级增加：

1. **时间碎片化**：发布延迟、乱序事件、不同 as-of time、迟到修正。
2. **语义碎片化**：active、eligible、disbursed 等代码含义漂移，代码表版本不一致。
3. **实体碎片化**：重复 person、错误 merge/split、crosswalk 缺失或置信度不足。
4. **结构碎片化**：字段重命名、类型变化、必填转可选、嵌套结构变化。
5. **权威冲突**：SIS、LMS、CRM 对同一状态给出不同事实。
6. **访问碎片化**：字段因用途或角色被屏蔽，导致结果并非技术缺失而是授权不可见。
7. **粒度碎片化**：课程级、section 级、学生学期级数据被错误聚合后连接。
8. **修复策略对照**：raw join、crosswalk、contract enforcement、reconciliation、人工 steward review。

每个实验除了现有 miss_rate、jaccard 和 weighted_miss_loss，还应记录：

- false-link rate 与 ambiguous-link rate。
- freshness lag 和 stale-decision rate。
- schema/contract violation count。
- reconciliation gap。
- 数据修复成本和查询延迟。
- 敏感字段暴露量。
- 每种修复策略恢复了多少学生，同时引入多少误报。

## 实施顺序

建议先选一个真正跨域且风险可解释的用例，例如“学业风险 + 助学金中断干预”，不要先做全校统一模型。

1. 列出该决策使用的字段、权威来源、更新频率和允许用途。
2. 给 Registrar 与 Financial Aid 各定义一个 source-aligned product。
3. 建 canonical identity crosswalk 和版本化 code set。
4. 发布一个只含用例所需字段的 conformed product。
5. 对 source -> conformed -> intervention mart 全链路加 contract、lineage 和质量指标。
6. 用本项目故障注入验证缺行、ID 漂移、迟到和语义变化会漏掉哪些学生。
7. 结果稳定后再复制模式到 Admissions、LMS 和 Advising。

## 不建议做的事

- 不让 BI、LLM 或研究人员直接连接部门生产库。
- 不让所有部门共享一个可任意写入的全校数据库。
- 不把“统一数据湖”理解为没有 owner、契约和用途边界的文件堆。
- 不用同名字段推断同一语义，也不用邮箱作为长期 canonical ID。
- 不把消息总线当作数据目录或历史仓库。
- 不为报表一致性强行引入跨部门分布式事务。
- 不在没有攻击评估时把 Bloom-filter PPRL 宣称为匿名化。
- 不以“数据越多越好”为原则复制敏感学生数据。

这个方案保留了你提出的微服务直觉，但把数据库边界转换成可治理的数据产品边界。部门继续对业务事实负责，学校平台负责让这些事实可以安全、可追溯、按时间重现地组合。
