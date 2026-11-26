# ClusterEnergy 超算能耗监控部署指南

本系统采用 **C（采集）→ A（汇聚）→ B（展示）** 三端架构，默认运行在 ARM64 Linux 登录节点，通过 mTLS 加密通信，默认每 5 秒采样与刷新。

> **全离线部署**：三个 `.tar.gz` 交付包均已内置 `linux/arm64` Docker 镜像和一键启停脚本，超算环境无需联网。**B 端默认采用内置 MySQL 8.0 容器存储**（同时支持配置切换外部 MySQL 或回退 SQLite）。

| 角色 | 作用 | 默认端口 | 默认存储 | 模拟模式 |
|---|---|---|---|---|
| **A 端** | 汇聚 C 端数据，或内置生成模拟节点数据 | `19200` | SQLite (WAL) | 必选 |
| **B 端** | 拉取 A 端数据并持久化，提供 Web 大屏 | `18088`（内部网关 `18081`、MySQL `3306`） | **MySQL 8.0（默认）** / SQLite | 必选 |
| **C 端** | 读取本机采集 JSON（`latest.json`）并缓存补拉 | `19100` | SQLite (WAL) | 无需部署 |

---

## 一、超算离线部署（4 步完成）

### 第 1 步：校验并解压

将对应角色的压缩包和 `.sha256` 文件传到目标节点，各角色解压到独立目录：

```bash
sha256sum -c clusterenergy-a-arm64.tar.gz.sha256
mkdir -p a && tar -xzf clusterenergy-a-arm64.tar.gz -C a
cd a
```

（B 端、C 端同理，分别解压到 `b/`、`c/` 目录。）

### 第 2 步：放置 mTLS 证书

在各角色解压目录下新建 `certs/`（或通过 `CE_CERTS_DIR` 指定路径），按角色放入对应证书：

| 角色 | `certs/` 所需文件 |
|---|---|
| **A 端** | `ca.crt`、`a_server.crt`、`a_server.key`（真实模式还需 `a_client.crt`、`a_client.key`） |
| **B 端** | `ca.crt`、`b_client.crt`、`b_client.key` |
| **C 端** | `ca.crt`、`c_server.crt`、`c_server.key` |

> **如何签发证书**（离线脚本，在已安装 `cryptography` 的本地机器上执行，SAN 必须匹配实际访问的主机名或 IP；CA 私钥留在本地，不要传上超算）：
> ```bash
> python3 scripts/gen_certs.py --output private-pki \
>   --a-host <A节点IP或主机名> --a-host 127.0.0.1 \
>   --c-server c1=<C1节点IP或主机名> --c-server c2=<C2节点IP或主机名>
> ```

### 第 3 步：创建 `site.env`

在各角色解压目录下新建 `site.env`（B 端默认自动拉起包内 ARM64 MySQL 8.0 容器，并按模拟/真实模式自动分库）：

**方案 A：纯模拟模式（仅部署 A + B，快速验收推荐）**

- A 端 `a/site.env`：
  ```bash
  CE_MODE=simulated
  CE_SIM_NODES=10
  ```
- B 端 `b/site.env`：
  ```bash
  CE_MODE=simulated
  CE_A_UPSTREAM=https://<A节点IP或主机名>:19200
  CE_MYSQL_PASSWORD=clusterenergy123
  ```

**方案 B：真实数据模式（部署 C + A + B）**

- C 端 `c/site.env`（多个采集点各自配置）：
  ```bash
  CE_C_ID=C1
  CE_SOURCE_DIR=/absolute/path/to/collector_dir
  CE_SOURCE_FILE=latest.json
  ```
- A 端 `a/site.env`：
  ```bash
  CE_MODE=real
  CE_C_TARGETS=C1=https://<C1节点IP>:19100,C2=https://<C2节点IP>:19100
  ```
- B 端 `b/site.env`：
  ```bash
  CE_MODE=real
  CE_A_UPSTREAM=https://<A节点IP>:19200
  CE_MYSQL_PASSWORD=clusterenergy123
  ```

### 第 4 步：一键启动与验证

按 **C（如有）→ A → B** 的顺序启动：

```bash
bash deploy.sh start ./site.env
bash deploy.sh status ./site.env
```

- 常用运维命令：`bash deploy.sh [start|restart|stop|status|load] ./site.env`。
- 首次启动会自动校验宿主机 `linux/arm64` 架构并通过 `docker load` 导入本地镜像（容器采用 `--network host` 与 `--pull never`）。
- B 端首次启动 MySQL 8.0 会初始化数据目录（约 15–30 秒），`deploy.sh` 会自动等待 MySQL 就绪后再拉起网关与 Web 服务，正常状态下 `status` 显示 `clusterenergy-b-mysql`、`clusterenergy-b-gateway`、`clusterenergy-b-web` 均为 `running`。

启动后在浏览器访问 `http://<B节点IP>:18088`，确认：
1. 页面每 5 秒自动更新，集群总功耗与节点/机柜合计一致；
2. 「运行总览」与「功耗排行」支持按机柜 / 按节点切换与联动；
3. 暂停 A 端后页面出现连接异常提示，恢复后自动恢复并写入运行日志。

---

## 二、B 端存储切换与常用可选配置

### B 端存储模式切换（`b/site.env`）

| 场景 | 配置方式 | 说明 |
|---|---|---|
| **默认：内置 MySQL 容器** | 无需额外配置（默认 `CE_STORAGE=mysql`） | 自动加载并启动包内 `mysql:8.0` ARM64 容器，仅监听 `127.0.0.1:3306` |
| **连接外部已有 MySQL** | 设置 `CE_MYSQL_HOST=<远端IP>`、`CE_MYSQL_PORT`、`CE_MYSQL_USER`、`CE_MYSQL_PASSWORD`、`CE_MYSQL_DB` | 当 `CE_MYSQL_HOST` 非本机地址时，自动复用外部 MySQL，不启动本地容器 |
| **回退为轻量 SQLite** | 设置 `CE_STORAGE=sqlite` | 不启动 MySQL 容器，使用本地 SQLite 文件存储 |

### 其他常用参数

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CE_A_PORT` / `CE_C_PORT` / `CE_WEB_PORT` | `19200` / `19100` / `18088` | 各端监听端口（B 网关 `CE_GATEWAY_PORT=18081`，MySQL `CE_MYSQL_PORT=3306`） |
| `CE_CERTS_DIR` / `CE_CONFIG_DIR` | `./certs` / `./config` | 证书目录与可选配置目录 |
| `CE_MYSQL_DATA_DIR` / `CE_DATA_DIR` | `clusterenergy-b-mysql-data` / `clusterenergy-<角色>-data` | MySQL 或 SQLite 持久化数据卷名 / 宿主机绝对路径 |
| `CE_MYSQL_DB` / `CE_MYSQL_USER` | `clusterenergy_<模式>` / `clusterenergy` | 模拟模式（`clusterenergy_simulated`）与真实模式（`clusterenergy_real`）自动分库 |
| `CE_C_RETENTION_DAYS` / `CE_A_RETENTION_DAYS` / `CE_B_RETENTION_DAYS` | `1` / `15` / `30` | C 冷备（0.5–3 天）、A 热存、B 归档保留天数（B 超过 `CE_B_HOT_DAYS=30` 自动按小时 gzip 分段压缩） |
| `CE_JOBS_DIR` | 未启用 | B 端 Slurm 作业 JSON 目录（配合 `poll_slurm.py` 使用） |

- **机柜映射台账（A 端）**：在 `a/config/racks.json` 中写入 `{"cn001": "R01", "cn002": "R01"}` 即可按机柜聚合；未映射节点仍计入集群总功耗。
- **自定义源字段映射（C 端）**：若真实 `latest.json` 字段名与默认（`node_id` / `node_type` / `power_w` / `collected_at`）不同，在 `c/config/schema.json` 配置字段路径和单位（如 `"unit": "kW"`、`include_pattern`）。
- **Slurm 作业能耗（B 端可选）**：在可执行 `squeue` 的登录节点运行 `python3 poll_slurm.py --output /abs/jobs/jobs.json --command squeue`，并在 B 端设置 `CE_JOBS_DIR=/abs/jobs`。

---

## 三、开发与离线打包（构建机）

在可联网的构建机上重新打包（若 `b_node/frontend/dist` 已存在，将直接复用）：

```bash
# 重新编译前端（仅改动前端代码时需要）
cd b_node/frontend && npm ci && npm run build && cd ../..

# 构建 ARM64 离线包及 SHA256（B 包默认含 MySQL 8.0；x86 改用 --platform linux/amd64）
python3 scripts/pack_offline.py --platform linux/arm64 --output dist/arm64

# 运行自动化测试
python3 -m unittest discover -s tests -v
node --test b_node/frontend/tests/*.test.js
```
