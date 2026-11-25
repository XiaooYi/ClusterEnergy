# ClusterEnergy 超算集群能耗监控系统

面向离线/隔离超算环境的集群能耗实时采集、安全汇聚与可视化监控系统。采用**全链路 mTLS 双向认证主动拉取**架构，超算节点零外部网络依赖（纯 Python 3.8+ 标准库离线部署），展示端采用 Docker 容器化部署（Python 网关 + SQLite 持久化 + Vue 3 / ECharts 监控大屏）。

---

## 1. 系统架构与节点分工

```text
[C1: ln2 (25.8.100.22)] ◀── mTLS HTTPS (:19100, 2s) ──┐
                                                       ├── [A: ln3 (25.8.100.23)] ◀── SSH 反向隧道 (:19200) ◀── [B: 192.168.251.226]
[C2: ln4 (25.8.100.24)] ◀── mTLS HTTPS (:19100, 2s) ──┘      汇聚计算 + API (:19200)                             ├─ b-gateway (:18081, SQLite)
                                                                                                                 └─ b-web (Nginx+Vue, :18088)
```

| 角色 | 节点 / 地址 | 运行环境 | 核心职责 |
| :--- | :--- | :--- | :--- |
| **C 端（采集）** | `ln2` (`25.8.100.22`)<br>`ln4` (`25.8.100.24`) | Ubuntu 20.04 (`aarch64`)<br>Python 3.8.10 标准库 | 采集 28 路功耗指标并原子写入本地隔离目录 `latest.json`，通过 mTLS HTTPS (`:19100`) 暴露给 A 端拉取。 |
| **A 端（汇聚）** | `ln3` (`25.8.100.23`) | Ubuntu 20.04 (`aarch64`)<br>Python 3.8.10 标准库 | 每 2 秒并发拉取各 C 端数据，计算分域汇总与能效比，维护近 5 分钟缓存，暴露 mTLS API (`127.0.0.1:19200`) 并维持到 B 端的自动重连 SSH 反向隧道。 |
| **B 端（展示）** | `192.168.251.226` | Ubuntu 20.04 (`x86_64`)<br>Docker 24.0.7 | `cluster-energy-gateway` 每 2 秒通过隧道 mTLS 拉取 A 端并按分钟持久化至 SQLite；`cluster-energy-web` 托管 Vue 3 + ECharts 大屏 (`:18088`)。 |

> **共享文件系统隔离说明**：超算节点共享 `/vol8` Lustre 文件系统，`c_node/ctl.sh` 会自动按 `hostname -s`（`ln2` / `ln4`）将 `latest.json`、PID 和日志隔离到 `runtime/<node_id>/` 下，避免多节点冲突。

---

## 2. 目录结构

```text
ClusterEnergy/
├── common/
│   ├── config.py             # 28 路功耗字段定义、默认端口与周期常量
│   └── tls.py                # 基于标准库 ssl 的 mTLS 服务端/客户端上下文封装
├── c_node/
│   ├── collector.py          # 模拟采集器（每 2s 原子写入 latest.json）
│   ├── exporter.py           # mTLS HTTPS 数据导出服务 (:19100)
│   └── ctl.sh                # C 端启停与状态检查脚本 (start/stop/restart/status)
├── a_node/
│   ├── aggregator.py         # 并发拉取器、能耗汇总计算、历史缓存与 mTLS API (:19200)
│   ├── tunnel.py             # 自动重连 SSH 反向隧道守护进程 (A:19200 -> B:19200)
│   └── ctl.sh                # A 端启停与状态检查脚本
├── b_node/
│   ├── docker-compose.yml    # 容器编排配置（参考）
│   ├── start_docker.sh       # Docker 镜像构建与容器启动脚本
│   ├── gateway/
│   │   ├── Dockerfile
│   │   └── gateway.py        # mTLS 拉取网关 + SQLite 持久化 (/data/energy_history.db)
│   └── frontend/
│       ├── Dockerfile        # Node 构建 + Nginx 托管多阶段镜像
│       ├── nginx.conf        # :18088 静态资源与 /api/ 反向代理
│       └── src/
│           ├── App.vue       # 监控大屏主页面（分页矩阵、多时间维度、通道柱状图）
│           └── style.css
└── scripts/
    ├── gen_certs.py          # 离线生成私有 CA 及各节点 mTLS 证书
    ├── pack_offline.py       # 打包三端离线部署包 (.tar.gz + .sha256)
    └── deploy.py             # SFTP 分发、校验、配置隧道密钥与远程启动脚本
```

---

## 3. 数据格式规范

C 端输出的 `latest.json` 采用“临时文件 + `os.replace` 原子替换”写入，包含以下结构：

* **元信息**：`schema_version`、`node_id`、`host_ip`、`seq`（递增采样序号）、`collected_at`
* **分表时间戳**：`sources.table1_ts`、`sources.table2_ts`
* **28 路功耗指标 (`metrics`)**：
  * `N0_DDQ` ~ `N7_DDQ`（8 路内存接口功耗）
  * `N0_CORE` ~ `N7_CORE`（8 路计算核心功耗）
  * `NI0_OUT` ~ `NI5_OUT`（6 路网络互连输出功耗）
  * `HVM0_OUT` ~ `HVM5_OUT`（6 路高带宽存储输出功耗）

---

## 4. 快速部署与常用运维

### 4.1 本地证书生成、打包与一键部署

```bash
# 1. 生成离线 CA 与各节点 mTLS 证书（输出至 certs/）
python scripts/gen_certs.py

# 2. 打包 C/A/B 三端离线包及 sha256 校验文件（输出至 dist/）
python scripts/pack_offline.py

# 3. 通过 SFTP/SSH 分发到三台超算及 B 端并启动服务
python scripts/deploy.py
```

### 4.2 各节点日常启停

* **C 端（`ln2` / `ln4`）**：
  ```bash
  /vol8/home/smqin/cluster_energy/c_node/ctl.sh status
  /vol8/home/smqin/cluster_energy/c_node/ctl.sh restart
  ```
* **A 端（`ln3`）**：
  ```bash
  /vol8/home/jbzhang/cluster_energy/a_node/ctl.sh status
  /vol8/home/jbzhang/cluster_energy/a_node/ctl.sh restart
  ```
* **B 端（`192.168.251.226`）**：
  ```bash
  docker ps --filter "name=cluster-energy"
  bash /home/user/cluster_energy/start_docker.sh
  ```

### 4.3 访问与接口说明

* **监控大屏页面**：`http://192.168.251.226:18088/`
* **B 端网关接口**（由 Nginx 代理 `/api/`）：
  * `GET /api/snapshot`：集群及各节点最新实时快照、今日累计电量 (`today_kwh`)、运行事件
  * `GET /api/history`：最近 5 分钟（最多 150 个点，2s 粒度）实时曲线数据
  * `GET /api/stats?range=today|7d|30d`：当天分钟级统计 / 近 7 天、近 30 天每日耗电量 (`kWh`) 与平均/峰值功率

---

## 5. 二次开发与生产接入指南

1. **接入真实 MySQL / 采集数据源**：
   * 只需让现有采集程序按第 3 节格式原子写入 `/vol8/home/smqin/cluster_energy/runtime/<node_id>/latest.json`，并停掉 `c_node/ctl.sh` 中的模拟 `collector.py`（保留 `exporter.py` 即可）。
2. **增减真实 C 端节点 / 关闭模拟节点**：
   * 编辑 `a_node/ctl.sh` 中的 `aggregator.py` 启动参数：
     * 增加真实节点：`--target <node_id>=<ip>:19100`（并在 `scripts/gen_certs.py` 为新节点签发对应 IP SAN 证书）。
     * 关闭多节点测试模拟：将 `--sim-nodes 8` 改为 `--sim-nodes 0`（或移除该参数）。
3. **调整能效比计算公式**：
   * 当前 `a_node/aggregator.py` 中 `compute_node_summary()` 使用 `core_power / total_power` 作为能效比占位计算，可按实际业务吞吐指标直接替换。
