# ClusterEnergy 超算计算节点能耗监控

正式部署时，A、B 和 C 均在 ARM Linux 登录节点上运行。C 读取已存在的采集 JSON，A 主动通过 mTLS 拉取 C，B 主动通过 mTLS 拉取 A。B 的 Nginx 页面展示已接入计算节点的总功耗、单节点功耗、趋势和按机柜汇总的实时功耗。默认每 5 秒采样和拉取。模拟模式只需 A 和 B，数据库按模式分开。

## 构建离线包

在能联网的构建机上运行。前端生成静态文件后，镜像分层构建不需要在构建机上运行 ARM 程序。

```bash
python3 scripts/pack_offline.py --platform linux/arm64
```

生成三个角色包和各自的 SHA256 文件。构建脚本按镜像 manifest 核实架构。旧实验室 x86 服务器可用同一命令加 --platform linux/amd64 构建单独的包。重新构建前端可以在 b_node/frontend 执行 npm ci 和 npm run build。若静态文件已经存在，打包脚本直接使用现有静态文件。

## 一键部署

每个角色解压到独立目录。C1、C2 可用同一个 C 软件包，各自指定采集目录、端口和证书。

```bash
sha256sum -c clusterenergy-a-arm64.tar.gz.sha256
mkdir -p a
tar -xzf clusterenergy-a-arm64.tar.gz -C a
cd a
bash deploy.sh start ./site.env
bash deploy.sh status ./site.env
```

deploy.sh 支持 start、restart、stop、status、load。第一次启动会校验 Docker 运行时的平台并从本地归档导入镜像。默认使用命名卷存储 SQLite。CE_DATA_DIR 可指定已有的绝对路径或卷名。需要使用共享存储时，应在现场确认 SQLite 的文件锁和 WAL 适用性。

| 角色 | 模拟部署参数 | 正式部署参数 |
|---|---|---|
| A | CE_MODE=simulated，CE_SIM_NODES=8 | CE_MODE=real，CE_C_TARGETS=C1=https://c1.example:19100,C2=https://c2.example:19100 |
| B | CE_MODE=simulated，CE_A_UPSTREAM=https://a.example:19200 | CE_MODE=real，CE_A_UPSTREAM=https://a.example:19200 |
| C | A 内置模拟时无需启动 C | CE_SOURCE_DIR=/absolute/source，CE_SOURCE_FILE=latest.json，CE_C_ID=C1 |

site.env 中每行使用 shell 变量赋值。例如 B 的两行如下。

```bash
CE_MODE=simulated
CE_A_UPSTREAM=https://a.example:19200
```

证书默认放在各包的 certs 目录，可用 CE_CERTS_DIR 设置。CE_A_PORT、CE_C_PORT、CE_GATEWAY_PORT、CE_WEB_PORT 的默认值分别为 19200、19100、18081、18088。B 的网关只监听本机，Nginx 提供前端入口。Docker 使用 Linux host 网络模式。实验室旧拓扑使用反向隧道时，设置 B 的 CE_A_UPSTREAM 为反向端口对应的回环地址，A 的服务器证书须包含该回环地址。可直接用 A/C 原生启停脚本及 B 的 Python 网关验证同一协议。

在 A、B 时钟存在已测量偏差的模拟环境，可用 CE_SIM_CLOCK_OFFSET_SECONDS 设置 B 接入时应用的秒数修正。更改此配置时应使用新的模拟库，或先将已有模拟样本与统计迁到相同的时间基准。真实采集以同步后的源时钟为准。

### mTLS 证书

在签发机安装 cryptography 后生成证书。A 和 C 的证书 SAN 必须匹配拉取方使用的 DNS 名称或 IP。若需要回环访问，追加 --a-host 127.0.0.1。

```bash
python3 scripts/gen_certs.py --output private-pki \
  --a-host a.example --a-host 127.0.0.1 \
  --c-server c1=c1.example --c-server c2=c2.example
```

各角色都需要 ca.crt。A 使用 a_server.crt/key，正式模式还使用 a_client.crt/key。B 使用 b_client.crt/key。每个 C 只放对应的 C 证书和私钥，部署时命名为 c_server.crt/key。CA 私钥留在签发机。路径、证书、登录凭据由部署方配置。

## 真实采集与机柜台账

C 以只读方式挂载原采集文件所在目录，支持写端通过临时文件和原子重命名更新 JSON。默认支持单节点对象、节点数组，以及带 nodes 字段的批量对象。例如：

```json
{
  "collected_at": "2026-11-25T16:00:00+08:00",
  "nodes": [
    {"node_id": "cn001", "node_type": "compute", "power_w": 487.2, "metrics": {"N0_CORE": 35.0}}
  ]
}
```

时间戳须包含时区，也支持 Unix 秒或毫秒值。power_w 是整机功耗字段。当真实数据使用不同字段时，在 C 包 config/schema.json 设置映射，或将 CE_CONFIG_DIR 指向配置目录。例如：

```json
{
  "nodes": "data",
  "node": "name",
  "timestamp": "sample_time",
  "power": "readings.total",
  "unit": "kW",
  "kind": "role",
  "compute_kinds": ["compute"],
  "include_pattern": "cn[0-9]+",
  "metrics": "readings"
}
```

若实际 JSON 缺少节点类型，按已确认的数据来源设置 include_pattern 或补充类型字段。默认仅接受明确提供的整机功耗值。现有分项数据保存在 metrics 中，部分功耗字段不会被当作整机功耗求和。A 从 config/racks.json 读取机柜台账，例如 {"cn001":"R01","cn002":"R01"}。源数据已经携带 rack_id 时也可使用。缺少映射的节点仍计入集群功耗，页面单独提示未映射节点数量。

## 保留期和数据恢复

| 角色 | 默认保留 | 调整方式 |
|---|---:|---|
| C | 1 天样本冷备 | CE_C_RETENTION_DAYS，可填写 0.5 到 3 |
| A | 15 天热数据 | CE_A_RETENTION_DAYS，可按约定调整至 30 |
| B | 30 天全量节点样本 | CE_B_RETENTION_DAYS 与 CE_B_HOT_DAYS |

C/A 提供按持久化游标读取的分页接口。A/B 在保存新样本后推进游标，进程重启后从数据库恢复。B 保留整机功耗及源配置选中的 metrics 字段。断线超过上游保留窗口时，API 和页面提示可能有缺口。C 的原始数据源若只写最新文件，C 离线期间已经被覆盖的样本无法从该文件恢复。

B 历史样本可通过 /api/node-history 按节点和时间段查询，支持分段读取。将 B 保留期调到 30 天以上后，超过 30 天的数据按节点和小时分段 gzip 压缩，超过 60 天的归档使用更高压缩级别。读取时按查询节点和时间范围解压相关分段。集群分钟统计可通过 /api/stats 查询。真实节点规模下的磁盘占用、归档积压和查询延迟需用现场样本压测。

## Slurm 作业

在可执行 Slurm 命令的登录节点运行 B 包附带的 poll_slurm.py，每 60 秒原子发布作业 JSON。B 配置 CE_JOBS_DIR 指向输出目录。低版本可使用 --format legacy。若现场使用 yhqueue，用 --command yhqueue。

```bash
python3 poll_slurm.py --output /absolute/jobs/jobs.json --command squeue
```

页面显示运行中作业。点击作业查看最近一小时、且晚于作业开始时间的节点能耗估算。估算只累计间隔可确认的采样区间，排除当前同时分配给多个运行作业的节点。结果是所查询节点的整机用电估算，依赖可见的 Slurm 作业与已接入功耗节点。已结束作业的完整归因需要历史分配记录。

## 验证

```bash
python3 -m unittest discover -s tests -v
cd b_node/frontend
npm ci
npm run build
```

集成测试使用临时端口、临时数据库和独立 CA，覆盖 A/B 纯模拟、C→A→B mTLS、B 重启补拉、无客户端证书拒绝，以及归档压缩回读和作业估算。运行集成测试的机器需安装 cryptography。
