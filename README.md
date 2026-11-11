# cb-monitor：EC2 Capacity Blocks for ML 容量监控

cb-monitor 定时查询 AWS 美国和欧洲区域的 [EC2 Capacity Blocks for ML](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html) 报价。发现新的容量就发邮件通知，每天另发一封汇总邮件。

默认查询条件：

| 项目 | 默认值 |
|---|---|
| 实例类型 | `p6-b300.48xlarge`、`p6-b200.48xlarge` |
| 区域 | 所有美国、欧洲商业区域，自动去掉不支持的区域 |
| 开始时间 | 现在 +7 天 到 现在 +56 天（Capacity Block 最多提前 8 周预订） |
| 时长 | 1–14 天每天一档，21–175 天每 7 天一档，共 37 档 |
| 台数 | 单个 Capacity Block 1–64 台，报告每个组合的最大可订台数 |
| 粒度 | 按"开始周"分别报告 |
| 扫描频率 | 每 15 分钟 |
| 每日汇总 | 每天 09:00（北京时间） |

> **cb-monitor 只查询，不购买。** Capacity Block 购买后不能取消，邮件里只给出手动购买的命令。

---

## 目录

- [架构](#架构)
- [工作原理](#工作原理)
- [前置条件](#前置条件)
- [一键部署](#一键部署)
- [部署参数](#部署参数)
- [日常操作](#日常操作)
- [邮件示例](#邮件示例)
- [本地运行与测试](#本地运行与测试)
- [费用](#费用)
- [权限与安全](#权限与安全)
- [已知限制](#已知限制)
- [故障排查](#故障排查)
- [卸载](#卸载)
- [推送到 GitHub](#推送到-github)
- [项目结构](#项目结构)

---

## 架构

```
 EventBridge Scheduler                 Lambda: <stack>-monitor               各区域 EC2 API
 ┌──────────────────────┐   scan    ┌────────────────────────────┐   DescribeCapacityBlockOfferings
 │ <stack>-scan         │──────────►│ 1. 探测支持的区域/实例类型   │──────────────► us-east-1, us-west-2,
 │ rate(15 minutes)     │           │ 2. 按周找容量、测最大台数    │                eu-west-2, ...
 └──────────────────────┘           │ 3. 与上次结果比较、去重      │
 ┌──────────────────────┐  summary  │ 4. 发邮件 / 存状态           │
 │ <stack>-daily-summary│──────────►│                            │
 │ cron(0 9 * * ? *)    │           └──────┬───────────────┬─────┘
 └──────────────────────┘                  │               │
                                           ▼               ▼
                               DynamoDB（状态表）     SNS 主题 ──► 邮件订阅
```

CloudFormation 栈（默认名 `cb-monitor`）创建这些资源：

| 资源 | 名称 | 用途 |
|---|---|---|
| Lambda 函数 | `<stack>-monitor` | Python 3.12，arm64，512 MB，超时 14 分钟 |
| EventBridge Scheduler | `<stack>-scan`、`<stack>-daily-summary` | 定时扫描、每日汇总 |
| SNS 主题 | `<stack>-alerts` | 发邮件，使用 `alias/aws/sns` 加密 |
| DynamoDB 表 | 自动命名 | 保存状态（按需计费，带 TTL） |
| CloudWatch 日志组 | `/aws/lambda/<stack>-monitor` | 默认保留 30 天 |
| IAM 角色 | 自动命名 | Lambda 执行角色、Scheduler 调用角色，最小权限 |

`deploy.sh` 还会创建一个私有 S3 桶 `<stack>-artifacts-<账号>-<区域>`，用来存放 Lambda 代码包。桶里的对象 30 天后自动删除。

## 工作原理

### 1. 区域探测

每 24 小时探测一次，对每个候选区域、每个实例类型调用一次 `DescribeCapacityBlockOfferings`：

- 返回 `InvalidAction`：该区域不提供 Capacity Blocks，例如 eu-west-1、eu-central-1。
- 返回 "instance type ... is not supported"：该区域不提供这个实例类型。
- 区域未启用（not-opted-in）：跳过，不调用。

AWS 以后在新区域上线这两种实例时，程序会自动纳入。

### 2. 按周切片

查询窗口是 [现在+7 天, 现在+56 天]，按 UTC 自然周（周一 00:00 开始）切成 8 段。第一周和最后一周不完整。周的标识是该周周一的日期，所以窗口随时间移动时，同一周的结果前后可以对得上。

### 3. 每周的"阶梯"：每个时长最多能订几台

逐个尝试 37 档时长 × 64 种台数，每周就要 2,368 次调用，而这个 API 很贵（见下一节）。程序利用容量的两个性质：

- 能订 N 台，就能订更少的台数；
- 能订 D 天，就能订同一天开始的更短时长。

所以随着时长变长，最大台数只会不变或下降，结果是一个"阶梯"，例如"1–14 天最多 64 台，21–63 天最多 16 台，70 天以上没有"。程序用几次二分查找测出这个阶梯。下次测量时用上次的阶梯做提示，容量没变化时每周只需 3–6 次调用。

每次扫描，对每个"区域 × 实例类型"做两步：

1. **找容量（每次扫描都做）**：对没有近期阶梯的周，用"1 台、最短时长"从这一周查到窗口末尾。API 返回最早可开始的报价，所以它之前的周都没有容量。然后从命中那一周的下一周继续查。没有容量的周不需要额外调用。
2. **测阶梯**：新发现容量的周立即测。已有阶梯的周每 `DetailRefreshMinutes`（默认 60 分钟）重测一次，最久没测的先测。

所以**全新出现的容量最迟一个扫描周期（15 分钟）发现**。已有容量的周里台数增加，最迟约 60 分钟发现。

### 4. API 限流

AWS 文档给出的 `DescribeCapacityBlockOfferings` 令牌桶（[EC2 API 限流](https://docs.aws.amazon.com/ec2/latest/devguide/ec2-api-throttling.html)）：每个账号、每个区域容量 10，每秒补充 0.15 个，约每分钟 9 次。另外实测还有一个账号级的 describe 配额，超出后返回 `CapacityBlockDescribeLimitExceeded`，几十分钟内都查不了。

程序的对策：

- 每个区域单独限速，默认每分钟 6 次（`ApiCallsPerMinute`），给控制台上的人工查询留出余量。
- 遇到 `RequestLimitExceeded` 先退避再重试。
- 遇到 `CapacityBlockDescribeLimitExceeded`，该区域暂停 60 分钟。
- 没能刷新的周继续沿用上次的阶梯（最多 6 小时），邮件里会标注"沿用 … 的结果"。

### 5. 账号查询限制自适应

有些账号能查的范围比官方文档窄。开发时测到的一个账号：开始时间只能在 2 天内，结束时间只能在 9 天内，时长最多 7 天。程序会从 API 的报错里解析出这些上限，自动收窄后续请求。如果收窄后和"现在+7 天以后开始"没有重叠，就把这些周标成"受账号限制"，并写进每日汇总。AWS 放开限制后，程序自动恢复完整查询，不需要改任何配置。需要放开时，请联系 AWS 客户经理或 Support。

### 6. 什么时候发邮件

**新容量通知**：以"实例类型 | 区域 | 开始周 | 时长"为一个组合，满足任一条件就通知：

- 第一次发现这个组合有容量；
- 最大台数超过了之前通知过的最大值；
- 消失超过 2 小时（`REAPPEAR_AFTER_MINUTES`）后又出现。

台数下降、容量消失都不通知。一次扫描的所有新组合合并成一封邮件。如果 SNS 主题上还没有已确认的订阅，通知会先保留，等有人确认订阅后再发。

**每日汇总**：每天固定时间发一封，内容包括：

- 最近一次扫描的时间和是否完整；
- 过去 24 小时的扫描次数、API 调用和错误次数；
- 监控范围和被排除的区域；
- 账号查询限制；
- 当前全部可用容量。

监控停止时，汇总里会有提示。

## 前置条件

- AWS CLI v2，已配置凭证（`aws sts get-caller-identity` 能正常返回）。
- 凭证需要权限创建：CloudFormation 栈、IAM 角色、Lambda、EventBridge Scheduler、SNS、DynamoDB、CloudWatch Logs、S3 桶。
- bash。`scripts/invoke.sh` 用 python3 格式化输出，没有 python3 也能用。
- 不需要安装 SAM CLI，也不需要 Docker。

## 一键部署

```bash
git clone https://github.com/lijingfz/cb-monitor.git
cd cb-monitor
./deploy.sh -e you@example.com
```

`deploy.sh` 依次做三件事：

1. 第一次运行时创建私有 S3 桶，用来存放代码包；
2. 打包 `src/`，上传到桶里（`aws cloudformation package`）；
3. 创建或更新 CloudFormation 栈（`aws cloudformation deploy`）。

部署完成后：

1. 打开收到的 **"AWS Notification - Subscription Confirmation"** 邮件，点 **Confirm subscription**。确认之前，SNS 不会投递任何邮件。
2. 发一封测试邮件：`./scripts/invoke.sh test`
3. 立即扫描一次，不用等 15 分钟：`./scripts/invoke.sh scan`

部署到其他区域或使用其他栈名：

```bash
./deploy.sh -r us-east-1 -s cb-monitor-prod -e you@example.com
```

Lambda 部署在哪个区域都可以，它会跨区域查询。

**更新代码或参数**时再运行一次 `deploy.sh`。没有传的参数保留当前值：

```bash
./deploy.sh                                  # 只更新代码
./deploy.sh -p ApiCallsPerMinute=4           # 修改参数
./deploy.sh -p InstanceTypes=p6-b200.48xlarge -p MinStartOffsetDays=3
```

## 部署参数

参数通过 `./deploy.sh -p 名称=值` 传入，也可以在 CloudFormation 控制台修改。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `NotificationEmail` | 空 | 通知邮箱，`-e` 是它的简写。为空时不创建订阅 |
| `InstanceTypes` | `p6-b300.48xlarge,p6-b200.48xlarge` | 逗号分隔 |
| `CandidateRegions` | 全部美国、欧洲区域 | 逗号分隔；不支持的区域会被自动排除 |
| `MinStartOffsetDays` | `7` | 最早开始时间：现在 + N 天 |
| `MaxAdvanceDays` | `56` | 最晚开始时间：现在 + N 天（最多 56） |
| `DurationsDays` | `1-14,21-175/7` | 时长（天）。`A-B` 是范围，`/7` 是步长。合法值为 1–14，以及 7 的倍数直到 182 |
| `MaxInstanceCount` | `64` | 单个 Capacity Block 查到多少台为止 |
| `WeeklySlices` | `true` | `true` 按开始周报告；`false` 只报告整个窗口 |
| `ExcludeLocalZones` | `true` | 排除 Local Zone（如亚特兰大 us-east-1-atl-2a） |
| `ScanIntervalMinutes` | `15` | 扫描间隔，最小 15 |
| `DetailRefreshMinutes` | `60` | 已有容量的周多久重测一次阶梯 |
| `ApiCallsPerMinute` | `6` | 每个区域每分钟最多调用几次，最大 9 |
| `SummaryScheduleExpression` | `cron(0 9 * * ? *)` | 每日汇总时间（[Scheduler 表达式](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html)） |
| `SummaryTimezone` | `Asia/Shanghai` | 上一项的时区 |
| `DisplayTimezone` | `Asia/Shanghai` | 邮件中在 UTC 旁边显示的时区 |
| `LogRetentionDays` | `30` | 日志保留天数 |

## 日常操作

手动调用（默认栈名 `cb-monitor`；其他栈用 `-s`，其他区域用 `-r`）：

```bash
./scripts/invoke.sh test       # 测试邮件
./scripts/invoke.sh summary    # 立即发每日汇总
./scripts/invoke.sh scan       # 立即扫描；有新容量就发邮件
./scripts/invoke.sh dry-run    # 扫描，但不发邮件、不保存状态，邮件内容打印在日志里
```

`scan` 和 `dry-run` 可能运行十几分钟，所以脚本异步调用 Lambda，再从日志里等待结果并打印出来。如果正好有一次定时扫描在运行，手动扫描会因为锁直接跳过，日志里显示 `another scan holds the lock`。

查看日志：

```bash
aws logs tail /aws/lambda/cb-monitor-monitor --follow
```

增加收件人：

```bash
TOPIC=$(aws cloudformation describe-stacks --stack-name cb-monitor \
  --query "Stacks[0].Outputs[?OutputKey=='TopicArn'].OutputValue" --output text)
aws sns subscribe --topic-arn "$TOPIC" --protocol email --notification-endpoint someone@example.com
```

新收件人同样要点确认邮件。

查看订阅状态：

```bash
aws sns list-subscriptions-by-topic --topic-arn "$TOPIC" \
  --query "Subscriptions[].[Endpoint,SubscriptionArn]" --output table
```

`SubscriptionArn` 显示 `PendingConfirmation` 表示还没确认。

## 邮件示例

下面是一封真实的新容量通知。为了在测试账号里拿到数据，这次把 `MinStartOffsetDays` 临时设成了 0，所以开始日期在当周。

```
主题：[CB监控] 发现新容量: p6-b300 us-east-1,us-west-2,eu-west-2; p6-b200 us-east-1,eu-north-1

发现新的 EC2 Capacity Blocks for ML 容量。
扫描时间：2026-11-11 21:01 北京时间（2026-11-11 13:01 UTC）
查询条件：p6-b300.48xlarge, p6-b200.48xlarge；开始时间在 现在+0 天 ~ 现在+56 天；时长 1–175 天（14 天以上按 7 天步长）；单个 Capacity Block 1–64 台

每一行表示：在该周内可以开始、该时长的 Capacity Block，单个 Block 最多能订多少台。
相邻时长的最多台数相同时合并成一行。“例”是其中最短时长、最早开始的一条报价。

━━ p6-b300.48xlarge @ us-west-2 ━━
开始日期 11-11(三) ~ 11-15(日)（UTC）
  · 1–3 天：最多 6 台 | 最早开始 11-13 11:30 UTC（11-13 19:30 北京时间） | us-west-2b | $129.17/台·时
    例：cb-051cff75aaef39530 = 6 台 × 1 天，11-13 11:30 → 11-14 11:30 UTC，us-west-2b，预付 $18,600.19

━━ p6-b200.48xlarge @ eu-north-1 ━━
开始日期 11-11(三) ~ 11-15(日)（UTC）
  · 1 天：最多 3 台 | 最早开始 11-12 11:30 UTC（11-12 19:30 北京时间） | eu-north-1b | $113.67/台·时
    例：cb-08418902ca5e4468f = 3 台 × 1 天，11-12 11:30 → 11-13 11:30 UTC，eu-north-1b，预付 $8,183.95
  · 2–4 天：最多 2 台 | 最早开始 11-12 11:30 UTC（11-12 19:30 北京时间） | eu-north-1b | $113.67/台·时
    例：cb-017e508ea34f3c1a3 = 2 台 × 2 天，11-12 11:30 → 11-14 11:30 UTC，eu-north-1b，预付 $10,911.94
  · 5–7 天：最多 1 台 | 最早开始 11-12 11:30 UTC（11-12 19:30 北京时间） | eu-north-1b | $113.67/台·时
    例：cb-0a3e39eff2ef4bb49 = 1 台 × 5 天，11-12 11:30 → 11-17 11:30 UTC，eu-north-1b，预付 $13,639.92

（其余区域略）

如何购买（请手动执行；Capacity Block 购买后不能取消）：
  1. 用想要的台数和时长再查一次，确认报价仍然可用，并拿到报价 ID：
     aws ec2 describe-capacity-block-offerings --region <区域> \
       --instance-type <实例类型> --instance-count <台数> \
       --capacity-duration-hours <小时数> \
       --start-date-range <最早开始, UTC> --end-date-range <最晚结束, UTC>
  2. 购买：
     aws ec2 purchase-capacity-block --region <区域> \
       --capacity-block-offering-id <cb-...> --instance-platform Linux/UNIX
```

先到先得，报价随时可能被别人订走，购买前一定要再查一次。

## 本地运行与测试

本地运行使用和 Lambda 相同的代码，状态存在本地 JSON 文件里，邮件内容打印到终端：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

# 单元测试（模拟 EC2，不调用 AWS）
python -m pytest -q

# 用当前 AWS 凭证做一次真实扫描，不发邮件、不保存状态
PYTHONPATH=src python -m cbmon scan --dry-run

# 只查部分区域、改开始时间
PYTHONPATH=src python -m cbmon scan --dry-run --regions us-east-1,us-west-2 --min-start-offset-days 3

# 查看区域探测结果
PYTHONPATH=src python -m cbmon discover
```

本地运行读取和 Lambda 相同的环境变量：`INSTANCE_TYPES`、`CANDIDATE_REGIONS`、`MIN_START_OFFSET_DAYS`、`MAX_ADVANCE_DAYS`、`DURATIONS_DAYS`、`MAX_INSTANCE_COUNT`、`WEEKLY_SLICES`、`EXCLUDE_LOCAL_ZONES`、`DETAIL_REFRESH_MINUTES`、`API_CALLS_PER_MINUTE`、`DISPLAY_TIMEZONE`。命令行参数会覆盖其中一部分，完整列表见 `python -m cbmon --help`。

本地扫描和部署好的 Lambda 共用同一个 API 配额。两边同时跑时，请把 `--regions` 限制在少数区域。

## 费用

在 us-west-2 部署，按每 15 分钟扫描一次估算：

| 服务 | 估算 |
|---|---|
| Lambda（arm64，512 MB） | 程序大部分时间在等 API 限速。首次测量要几分钟；之后大多数扫描几秒到 1–3 分钟（实测稳定后 2 秒、7 次 API 调用），每月约 $1–3；最坏情况每次跑满 13 分钟，每月约 $15。Lambda 免费额度可以抵掉一部分 |
| DynamoDB（按需） | 每次扫描几次读写，每月不到 $0.1 |
| SNS 邮件 | 每月前 1,000 封免费 |
| EventBridge Scheduler | 每月前 1,400 万次调用免费 |
| CloudWatch Logs、S3 | 每月不到 $0.1 |

## 权限与安全

- Lambda 角色只有这些权限：
  - `ec2:DescribeCapacityBlockOfferings`、`ec2:DescribeRegions`（只读）；
  - 本栈 SNS 主题的 `sns:Publish` 和 `sns:GetTopicAttributes`；
  - 通过 SNS 使用 KMS 密钥（带 `kms:ViaService` 条件）；
  - 本栈 DynamoDB 表的读写；
  - 写本栈的日志组。
- **没有任何购买权限**，代码里也不会调用 `PurchaseCapacityBlock`。
- Scheduler 角色只能调用这一个函数，信任策略限定本账号（`aws:SourceAccount`）。
- 仓库里没有账号 ID、邮箱或密钥。邮箱只作为部署参数传入。
- SNS 主题用 AWS 托管密钥加密。S3 桶禁止公开访问，并默认加密。

## 已知限制

- **阶梯假设**："台数越多越难订、时长越长越难订"。正常情况下成立；万一 AWS 某个容量池不满足，最大台数可能被低估。
- **刷新延迟**：受 API 配额限制，已有容量的周里台数增加，最迟约 `DetailRefreshMinutes` 后才发现。全新出现的容量每次扫描都会检查。
- **报价 ID 会失效**：邮件里的报价 ID 只是例子，购买前请用想要的台数和时长重新查询。
- **单个 Block 64 台**：AWS 规定每个 Capacity Block 最多 64 台，每个组织同一天最多 256 台。64 台的规格不是每个区域都支持，不支持时按"没有"处理。
- **不覆盖** GovCloud（需要单独的 GovCloud 账号）和 UltraServer（如 `u-p6e-gb200x72`）。
- 所有时间按 UTC 计算。Capacity Block 都在 UTC 11:30 结束。

## 故障排查

**没收到确认邮件**：检查垃圾邮件；或用上面"查看订阅状态"的命令确认状态。需要的话删掉订阅，重新 `./deploy.sh -e ...`。

**每日汇总里出现"账号查询限制"**：说明这个账号能查的范围比官方文档窄（例如开始时间只能在 2 天内）。这不是程序或权限的问题，需要联系 AWS 客户经理或 Support 放开。

**汇总里出现 `CapacityBlockDescribeLimitExceeded`**：账号级 describe 配额用完了，该区域会暂停 60 分钟。如果经常出现，调低 `ApiCallsPerMinute`，或者减少 `CandidateRegions` 和 `InstanceTypes`。

**汇总提示最近一次扫描是几小时前**：查看日志 `aws logs tail /aws/lambda/cb-monitor-monitor --since 3h`，并确认两个 Schedule 处于 ENABLED 状态。

**想看原始状态**：DynamoDB 表里有 `state`、`snapshot` 和 `stats#<小时>` 几类项，前两项是 gzip 压缩的 JSON。

## 卸载

```bash
./destroy.sh                       # 删除栈，需要输入栈名确认
./destroy.sh -y --purge-bucket     # 同时清空并删除代码包 S3 桶
```

删除栈会删除 Lambda、Schedule、SNS 主题和订阅、DynamoDB 表和日志组。

## 推送到 GitHub

`.gitignore` 已经排除了构建目录 `.build/`、本地状态 `.state/`、虚拟环境和缓存。

```bash
git init
git add .
git commit -m "cb-monitor: EC2 Capacity Blocks for ML monitor"
git branch -M main
git remote add origin https://github.com/<you>/cb-monitor.git
git push -u origin main
```

## 项目结构

```
.
├── README.md
├── template.yaml          CloudFormation 模板（全部 AWS 资源）
├── deploy.sh              一键部署 / 更新
├── destroy.sh             卸载
├── scripts/invoke.sh      手动调用 test / scan / dry-run / summary
├── requirements-dev.txt   本地开发依赖（Lambda 运行时自带 boto3）
├── src/cbmon/
│   ├── handler.py         Lambda 入口
│   ├── cli.py             本地命令行入口（python -m cbmon）
│   ├── app.py             扫描、汇总、测试三个任务
│   ├── scanner.py         按周找容量、测阶梯
│   ├── ec2query.py        API 调用、限速、限制解析、区域探测
│   ├── report.py          去重规则和邮件内容
│   ├── notifier.py        SNS 发送
│   ├── state.py           DynamoDB / 本地文件状态
│   ├── windows.py         周切片
│   ├── config.py          配置
│   └── timeutil.py        时间工具
└── tests/                 单元测试（模拟 EC2）
```
