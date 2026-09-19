# CTP 数据清洗 —— 定时调度部署

本目录说明如何用外部调度器在每个交易日 **16:00** 驱动清洗脚本，完成
"拉取分片数据 → 校验 → 合并去重 → 合成 K 线 → 回收远程已合并目录"。

脚本本身不含常驻调度：调度器只负责在 16:00 拉起一次进程，进程跑完自行退出并用
退出码汇报结果（见第 4 节）。

---

## 1. 为什么是 16:00

一个交易日 `T` 的目录（`<data_root>/T/`）= **T−1 晚夜盘 + T 白盘**：

| 时间 | 事件 |
|------|------|
| T−1 21:00 → T 02:30 | 夜盘，写入 `T/` 目录 |
| T 09:00 → 15:15 | 白盘，写入 `T/` 目录 |
| T 15:15 + close-grace（默认 300s） | 采集停止 |
| T ~15:20 → ~15:5x | 收盘清洗、写 `report.json`（`T/` 目录 finalize） |
| **T 16:00** | **清洗脚本运行：`T/` 已完整，且没有进程再写它** |
| T 21:00 | 当晚夜盘开始，写入 `T+1/` 目录（与本轮无关） |

所以 16:00 拉取并删除 `T/` 是安全的：当晚夜盘写的是 `T+1/`。周五 16:00 处理周五目录，
周五晚夜盘归下周一目录，同样不冲突。

调度器只按**星期**触发；节假日由脚本内置交易日历判断，非交易日直接以退出码 3 结束。
请务必在 `cleaner.yaml` 配好 `calendar.holidays_file`。

---

## 2. 目录与文件

```
deploy/cleaner/
├── README.md                       # 本文档
├── crontab.example                 # 通用 cron
└── launchd/
    └── com.btapi.ctp.cleaner.plist # macOS
```

---

## 3. 首次部署（重要）

1. **先只拉不删**：在 `cleaner.yaml` 设
   ```yaml
   pull:
     delete_remote_after_verify: false
   ```
2. 连续空跑 **≥ 1 个交易日**，核对合并结果与 `cleaner/reports/clean-<day>.json`
   无异常（尤其 `anomalies` 为空）。
3. 再用 `--dry-run` 预览删除清单：
   ```bash
   python -m bt_api_ctp.cleaner --config ctp_data/cleaner.yaml run --dry-run
   ```
4. 确认无误后把 `delete_remote_after_verify` 改回 `true`。

删除有三重保护：manifest 状态必须为 `merged`（已拉取+校验+合并）、配置开关开启、
目标路径必须是 `remote_data_root` 下的合法 `YYYYMMDD` 目录。**未拉取或校验未通过的
目录绝不会被删除。**

---

## 4. 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功（可能无数据可处理） |
| 1 | 配置错误 |
| 2 | 连接/传输/校验失败（远程数据保留） |
| 3 | 非交易日，正常跳过 |
| 4 | 合并或 K 线合成失败 |

无人值守可据退出码告警：非 0 且非 3 即需关注。**退出码 2/4 时远程数据一律保留**，
下次运行会自动重试。

---

## 5. macOS（launchd）

```bash
cp deploy/cleaner/launchd/com.btapi.ctp.cleaner.plist ~/Library/LaunchAgents/
# 编辑 plist：把 /Users/USER 换成真实路径
launchctl load ~/Library/LaunchAgents/com.btapi.ctp.cleaner.plist

launchctl list | grep btapi
# 手动触发一次（建议先加 --dry-run 观察）
launchctl start com.btapi.ctp.cleaner
```

日志：plist 里 `StandardOutPath` / `StandardErrorPath` 指定的文件，另有
`<tick_root>/logs/cleaner-<日期>.log`。

## 6. Linux（cron）

```cron
0 16 * * 1-5 cd /opt/bt_api_py && /opt/bt_api_py/.venv/bin/python -m bt_api_ctp.cleaner \
    --config /etc/bt-api-ctp/cleaner.yaml run \
    >> /opt/bt_api_py/ctp_data/logs/cron-cleaner.log 2>&1
```

cron 不加载 `.env`；本脚本不读 CTP 账号，只读 `cleaner.yaml`，因此无需 `.env`。
（`sftp` 后端如需密钥文件，请在配置或 SSH 默认路径中提供。）

## 7. 常用命令

```bash
# 全流程
python -m bt_api_ctp.cleaner --config ctp_data/cleaner.yaml run
# 只拉取+校验（不合并、不删除）
python -m bt_api_ctp.cleaner --config ctp_data/cleaner.yaml pull
# 合并已校验目录并补齐缺失 K 线
python -m bt_api_ctp.cleaner --config ctp_data/cleaner.yaml merge
# 历史回填 K 线（首次部署或补算）
python -m bt_api_ctp.cleaner --config ctp_data/cleaner.yaml kline --backfill
# 指定交易日
python -m bt_api_ctp.cleaner --config ctp_data/cleaner.yaml run --day 20260918
# 主机连通性自检
python -m bt_api_ctp.cleaner --config ctp_data/cleaner.yaml check-hosts
```

## 8. 输出位置

```
<tick_root>/<交易日>/<交易所>/<合约>.parquet     # 权威 tick 库（合并结果）
<kline_root>/<交易所>/<品种>/<合约>_<N>min.parquet # K 线（跨日追加，不按日期分目录）
<tick_root>/cleaner/manifest.json                # 拉取/校验/合并/删除 状态留痕
<tick_root>/cleaner/reports/clean-<交易日>.json  # 每次运行的清洗报告
<tick_root>/cleaner/staging/<主机>/<交易日>/      # 拉取过渡区（合并成功后自动清理）
```
