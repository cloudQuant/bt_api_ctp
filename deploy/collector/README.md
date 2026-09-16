# CTP 全市场 tick 采集 —— 无人值守部署

本文档说明如何用**外部调度器**（systemd timer / launchd / cron）驱动采集脚本，
实现无人值守：调度器只负责在开盘前拉起进程，进程自己判断交易日与时段、跑到收盘、
清洗落盘并生成完整性报告后退出。

> 脚本采用「单时段采集」模式，不做常驻守护；跨时段靠调度器分别触发。
> 实测单进程可覆盖全市场约 1.7 万个合约（0 丢弃），无需分片即可无人值守。

---

## 1. 调度规则（务必先理解夜盘归属）

| 时段 | 开盘 | 调度启动 | 结束 | 触发条件 |
|------|------|----------|------|----------|
| 白盘 | 09:00 | 08:45 | 15:15（中金所） | 当日是交易日 |
| 夜盘 | 21:00 | 20:45 | 02:30（最晚品种） | **当晚有夜盘** |

**夜盘归属规则**（决定调度器的星期配置）：

- 夜盘在**交易日的前一晚**进行：**周五晚的夜盘 + 周一的日盘 = 一个完整交易日**。
- **周日晚没有夜盘**；周六晚也没有。
- 法定节假日前一交易日晚，夜盘**暂停**。
- 因此夜盘调度用 `Mon..Fri`（周一到周五），而不是周日到周四。

脚本内置判定，收到 `--night` 时会自行确认「今晚是否真的开夜盘」：
周日晚、节假日前夕会直接以退出码 3 结束，不会白白连接柜台。

**调度时刻与是否交易日是两件事**：调度器只按星期触发，节假日由脚本通过
`calendar.holidays_file` 判断。请务必提供节假日文件（见第 4 节）。

---

## 2. 目录与文件

```
deploy/collector/
├── README.md                       # 本文档
├── systemd/                        # Linux
│   ├── bt-api-ctp-collector-day.service
│   ├── bt-api-ctp-collector-day.timer
│   ├── bt-api-ctp-collector-night.service
│   └── bt-api-ctp-collector-night.timer
├── launchd/                        # macOS
│   ├── com.btapi.ctp.collector.day.plist
│   └── com.btapi.ctp.collector.night.plist
└── crontab.example                 # 通用 cron
```

---

## 3. 命令行语义

```bash
python -m bt_api_ctp.collector \
    --config /etc/bt-api-ctp/collector.yaml \
    --until-close --wait-open            # 白盘
    # --night --until-close --wait-open  # 夜盘（追加 --night）
```

| 参数 | 作用 |
|------|------|
| `--config` | YAML 配置（分片、缓冲、落盘、账号等） |
| `--until-close` | 跑到**本交易组收盘**后自动结束（白盘跑到 15:15，夜盘跑到 02:30） |
| `--wait-open` | 在开盘前启动时，先等待开盘再开始计时（配合 `--until-close`） |
| `--night` | 声明这是夜盘段运行：要求「当晚有夜盘」，否则立即退出 |
| `--close-grace-sec` | 收盘后额外采集秒数，默认 300 |
| `--validate-shards` | 校验多机分片配置（不采集） |
| `--check-calendar` | 打印指定日期的交易日/夜盘状态（不采集） |

**退出码**：`0` 成功；`1` 分片校验不通过；`2` 配置错误；`3` 非交易日/无夜盘；
`4` 采集失败（连接、查询、落盘等）。

无人值守可据退出码做告警：非 0 即为需要关注的事件（`3` 是正常跳过，可忽略）。

**运行期日志**（stdout/stderr，建议重定向到文件或 journald）：

```
2026-09-16 14:33:07 INFO bt_api_ctp.collector.engine subscribed 17386 instruments (shard=by_exchange, flush=5.0s)
2026-09-16 14:34:00 INFO bt_api_ctp.collector.engine heartbeat: received=12043 buffered=812 dropped=0 sub_ok=17386 sub_failed=0 elapsed=53s
2026-09-16 14:34:05 INFO bt_api_ctp.collector.engine flushed 1266 instruments / 4820 ticks
2026-09-16 15:17:11 INFO bt_api_ctp.collector.engine collection finished: trading_day=20260916 instruments=17386 received=191051 dropped=0
```

`heartbeat` 行是判断「进程是否健康」的核心：`received` 持续增长、`dropped` 长期为 0
即正常。`sub_failed` 大于 0 说明有合约订阅被柜台拒绝，需关注。

---

## 4. 配置准备

**配置文件**（示例见 `examples/collector.example.yaml`）：

```yaml
data_root: /data/tick
asset_types: [future, option, spot_option]   # 含中金所股指期权
calendar:
  holidays_file: /etc/bt-api-ctp/holidays.json   # 必填：否则节假日会被当成交易日
ctp:
  md_front: tcp://182.254.243.31:30011
  td_front: tcp://182.254.243.31:30001
  # broker_id/user_id/password 留空则从环境变量 CTP_* 读取
```

**节假日文件**（`holidays.json`，JSON 数组）：

```json
["20261001", "20261002", "20261005", "20261006", "20261007"]
```

需要每年按交易所公告更新。缺少该文件时，工作日会被一律当作交易日，
节假日将误启动（脚本能连上但收不到行情，退出码为 0，属静默失败）。

**账号**：放 `.env`，字段 `CTP_BROKER_ID` / `CTP_USER_ID` / `CTP_PASSWORD` /
`CTP_MD_FRONT` / `CTP_TD_FRONT`。

---

## 5. Linux（systemd）

```bash
sudo mkdir -p /etc/bt-api-ctp /var/log/bt-api-ctp
sudo cp deploy/collector/systemd/*.service /etc/systemd/system/
sudo cp deploy/collector/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bt-api-ctp-collector-day.timer
sudo systemctl enable --now bt-api-ctp-collector-night.timer

# 查看调度
systemctl list-timers 'bt-api-ctp-*'
# 查看日志
journalctl -u bt-api-ctp-collector-day -f
# 手动演练（不等到开盘时刻）
sudo systemctl start bt-api-ctp-collector-day.service
```

`Type=oneshot` + `TimeoutStartSec=infinity`：一个采集进程可以合法运行数小时。

---

## 6. macOS（launchd）

```bash
mkdir -p ~/bt-api-ctp/logs
cp deploy/collector/launchd/*.plist ~/Library/LaunchAgents/
# 编辑 plist：把 /Users/USER 换成真实路径，填入账号
launchctl load ~/Library/LaunchAgents/com.btapi.ctp.collector.day.plist
launchctl load ~/Library/LaunchAgents/com.btapi.ctp.collector.night.plist

# 查看
launchctl list | grep btapi
tail -f ~/bt-api-ctp/logs/day.log
# 手动触发一次
launchctl start com.btapi.ctp.collector.day
```

---

## 7. 通用 cron

见 `crontab.example`：

```cron
45 8  * * 1-5  ... --until-close --wait-open            >> .../day.log   2>&1
45 20 * * 1-5  ... --night --until-close --wait-open    >> .../night.log 2>&1
```

cron 不会加载 `.env`，示例中用 `. /opt/bt_api_py/.env;` 显式加载。

---

## 8. 无人值守检查清单

1. **节假日文件已配置并每年更新**（否则节假日误启动）。
2. **日志已落盘并可轮转**（journald 或 logrotate；30 分钟全市场约 300 MB 数据）。
3. **磁盘容量充足**（全市场单日约 10–100 GB，按订阅范围估算）。
4. **`data_root` 目录权限**对运行用户可写。
5. **调度器时区正确**（`timedatectl` / 系统时区需为 Asia/Shanghai）。
6. **告警接入**：退出码非 0 或心跳 `received` 长时间不增长时告警。
7. **不要对同一 `data_root` 启动重叠的分片进程**：写入虽已加文件锁，但重叠分片
   等于重复采集（同一文件被反复读改写，浪费 IO）。

---

## 9. 数据落盘位置

```
<data_root>/<trading_day>/<exchange>/<instrument_id>.parquet
<data_root>/<trading_day>/report.json
```

- 目录日期是 **trading_day（交易日）**：周五晚的夜盘数据与下周一的日盘数据
  都写入**下周一**的目录，与交易所口径一致。
- `report.json` 记录每个合约的行数、起止时间与**交易时段内**的缺口
  （小节休息、午休、隔夜不计为缺口）。
