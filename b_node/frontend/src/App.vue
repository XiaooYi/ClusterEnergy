<template>
  <div class="dashboard">
    <header class="topbar">
      <div class="title-group">
        <h1>超算集群能耗实时监控大屏</h1>
        <div class="subtitle">集群节点实时功耗与能效运行监测</div>
      </div>
      <div class="status-bar">
        <div class="badge">
          <span class="dot" :class="gatewayOk ? 'ok' : 'err'"></span>
          <span>数据连接: {{ gatewayOk ? "正常" : "中断" }}</span>
        </div>
        <div class="badge">
          <span>在线节点: {{ cluster.online_nodes ?? 0 }} / {{ cluster.total_nodes ?? 0 }}</span>
        </div>
        <div class="badge">
          <span>今日累计: {{ fmtKwh(cluster.today_kwh) }} kWh</span>
        </div>
        <div class="badge">
          <span>最新采样: {{ formatClock(snapshot.updated_at) }}</span>
        </div>
        <button class="badge log-btn" @click="openLogsModal">
          <span class="dot" :class="abnormalCount > 0 ? 'err' : 'ok'"></span>
          <span>{{ (cluster.today_alerts ?? 0) > 0 ? `运行日志 (${cluster.today_alerts})` : "运行日志 · 正常" }}</span>
        </button>
        <button class="btn" :class="{ active: autoRefresh }" @click="autoRefresh = !autoRefresh">
          {{ autoRefresh ? "自动刷新: 开 (2s)" : "自动刷新: 暂停" }}
        </button>
      </div>
    </header>

    <section class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">集群总功耗</div>
        <div class="kpi-val">{{ fmt(cluster.total_power) }}<span class="kpi-unit">W</span></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">集群能效比</div>
        <div class="kpi-val">{{ fmtPct(cluster.efficiency_ratio) }}<span class="kpi-unit">%</span></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">计算核心功耗 (CORE)</div>
        <div class="kpi-val">{{ fmt(cluster.core_power) }}<span class="kpi-unit">W</span></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">内存接口功耗 (DDQ)</div>
        <div class="kpi-val">{{ fmt(cluster.ddq_power) }}<span class="kpi-unit">W</span></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">网络互连功耗 (NI)</div>
        <div class="kpi-val">{{ fmt(cluster.ni_out_power) }}<span class="kpi-unit">W</span></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">高带宽存储功耗 (HVM)</div>
        <div class="kpi-val">{{ fmt(cluster.hvm_out_power) }}<span class="kpi-unit">W</span></div>
      </div>
    </section>

    <section class="panel" style="margin-bottom: 12px">
      <div class="panel-header" style="flex-wrap: wrap; gap: 8px">
        <div class="panel-title">
          节点运行总览（共 {{ filteredNodes.length }} 个节点 · 当前选中: {{ selectedNodeId || "--" }}）
        </div>
        <div class="toolbar">
          <input
            v-model.trim="searchQuery"
            class="search-input"
            type="text"
            placeholder="搜索节点名..."
          />
          <button
            class="btn"
            :class="{ active: onlyAbnormal }"
            @click="onlyAbnormal = !onlyAbnormal"
          >
            仅看异常 ({{ abnormalCount }})
          </button>
          <button class="btn" :disabled="currentPage <= 1" @click="currentPage--">上一页</button>
          <span class="page-info">{{ currentPage }} / {{ totalPages }} 页</span>
          <button class="btn" :disabled="currentPage >= totalPages" @click="currentPage++">下一页</button>
        </div>
      </div>
      <div v-if="pagedNodes.length === 0" class="subtitle" style="padding: 18px 0; text-align: center">
        暂无匹配的节点数据
      </div>
      <div v-else class="nodes-matrix">
        <div
          v-for="node in pagedNodes"
          :key="node.node_id"
          class="node-tile"
          :class="{ selected: selectedNodeId === node.node_id, offline: node.status === 'offline' }"
          @click="selectedNodeId = node.node_id"
        >
          <div class="tile-top">
            <span class="tile-name">{{ node.node_id }}</span>
            <span class="tile-status">
              <span class="dot" :class="statusDot(node.status)"></span>
              <span>{{ statusText(node.status) }}</span>
            </span>
          </div>
          <div class="tile-power">{{ fmt(node.summary?.total_power) }} <span class="kpi-unit">W</span></div>
          <div class="tile-sub">
            <span>能效比 {{ fmtPct(node.summary?.efficiency_ratio) }}%</span>
            <span>占比 {{ sharePct(node.summary?.total_power) }}%</span>
          </div>
          <div class="share-bar">
            <div class="share-fill" :style="{ width: shareBarWidth(node.summary?.total_power) }"></div>
          </div>
        </div>
      </div>
    </section>

    <section class="main-grid">
      <div class="panel">
        <div class="panel-header" style="flex-wrap: wrap; gap: 8px">
          <div>
            <div class="panel-title">{{ leftTitle }}</div>
            <div v-if="statsSubtitle" class="subtitle">{{ statsSubtitle }}</div>
          </div>
          <div class="toolbar">
            <button
              v-for="m in leftModes"
              :key="m.id"
              class="btn"
              :class="{ active: leftMode === m.id }"
              @click="switchLeftMode(m.id)"
            >
              {{ m.label }}
            </button>
          </div>
        </div>
        <div ref="leftEl" class="chart-tall"></div>
      </div>

      <div class="right-stack">
        <div class="panel">
          <div class="panel-header">
            <div class="panel-title">计算核心与内存通道功耗 (N0 ~ N7 · {{ selectedNodeId || "--" }})</div>
          </div>
          <div ref="domain1El" class="chart-half"></div>
        </div>

        <div class="panel">
          <div class="panel-header">
            <div class="panel-title">互连与存储输出通道功耗 (NI / HVM · {{ selectedNodeId || "--" }})</div>
          </div>
          <div ref="domain2El" class="chart-half"></div>
        </div>
      </div>
    </section>

    <div v-if="logsOpen" class="modal-mask" @click.self="logsOpen = false">
      <div class="modal-card">
        <div class="modal-head">
          <div>
            <div class="panel-title">集群运行日志</div>
            <div class="subtitle">持久化保存最近 30 天节点异常、延迟与恢复事件</div>
          </div>
          <button class="btn" style="font-size: 13px; padding: 6px 14px" @click="logsOpen = false">关闭</button>
        </div>
        <div class="modal-body">
          <div class="panel-header" style="margin-bottom: 4px; flex-wrap: wrap; gap: 10px">
            <div class="toolbar">
              <button
                v-for="r in logRanges"
                :key="r.id"
                class="btn"
                style="font-size: 13px; padding: 6px 12px"
                :class="{ active: logRange === r.id }"
                @click="changeLogRange(r.id)"
              >
                {{ r.label }}
              </button>
            </div>
            <div class="toolbar">
              <select v-model="logNodeFilter" class="select-input" @change="onLogFilterChange">
                <option value="all">全部节点</option>
                <option v-for="n in sortedNodes" :key="n.node_id" :value="n.node_id">{{ n.node_id }}</option>
              </select>
              <select v-model="logLevelFilter" class="select-input" @change="onLogFilterChange">
                <option value="all">全部级别</option>
                <option value="error">异常</option>
                <option value="warn">延迟</option>
                <option value="info">恢复</option>
              </select>
            </div>
          </div>

          <div v-if="alertsPage.items.length === 0" class="subtitle" style="padding: 64px 0; text-align: center; font-size: 14px">
            暂无符合条件的运行事件，集群运行平稳。
          </div>
          <table v-else class="log-table">
            <thead>
              <tr>
                <th style="width: 210px">时间</th>
                <th style="width: 120px">节点</th>
                <th style="width: 100px">类型</th>
                <th>事件详情</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(a, idx) in alertsPage.items" :key="idx">
                <td class="subtitle" style="font-size: 14px">{{ formatDateTime(a.ts) }}</td>
                <td><strong>{{ a.node }}</strong></td>
                <td><span class="alert-lvl" :class="a.level">{{ levelText(a.level) }}</span></td>
                <td>{{ a.msg }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="modal-footer">
          <span class="subtitle" style="font-size: 13px">共 {{ alertsPage.total }} 条记录</span>
          <div class="toolbar">
            <button class="btn" style="font-size: 13px; padding: 5px 12px" :disabled="logPage <= 1" @click="goLogPage(logPage - 1)">
              上一页
            </button>
            <span class="page-info" style="font-size: 13px; min-width: 72px">{{ logPage }} / {{ alertsPage.total_pages }} 页</span>
            <button class="btn" style="font-size: 13px; padding: 5px 12px" :disabled="logPage >= alertsPage.total_pages" @click="goLogPage(logPage + 1)">
              下一页
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as echarts from "echarts";

const PAGE_SIZE = 10;
const leftModes = [
  { id: "trend", label: "实时趋势" },
  { id: "rank", label: "节点排行" },
  { id: "today", label: "当天" },
  { id: "7d", label: "近 7 天" },
  { id: "30d", label: "近 30 天" },
];

const snapshot = ref({ cluster: {}, nodes: [], alerts: [], gateway: {} });
const history = ref({ points: [] });
const statsData = ref({ range: "today", total_kwh: 0, avg_power: 0, peak_power: 0, points: [], days: [] });
const autoRefresh = ref(true);
const selectedNodeId = ref("");
const leftMode = ref("trend");
const searchQuery = ref("");
const onlyAbnormal = ref(false);
const currentPage = ref(1);
const logRanges = [
  { id: "today", label: "当天" },
  { id: "7d", label: "近 7 天" },
  { id: "30d", label: "近 30 天" },
];
const logsOpen = ref(false);
const logRange = ref("7d");
const logNodeFilter = ref("all");
const logLevelFilter = ref("all");
const logPage = ref(1);
const alertsPage = ref({ total: 0, page: 1, page_size: 12, total_pages: 1, items: [] });

const leftEl = ref(null);
const domain1El = ref(null);
const domain2El = ref(null);

let leftChart = null;
let domain1Chart = null;
let domain2Chart = null;
let timer = null;
let statsTick = 0;

const cluster = computed(() => snapshot.value.cluster || {});
const nodes = computed(() => snapshot.value.nodes || []);
const alerts = computed(() => snapshot.value.alerts || []);
const gatewayOk = computed(() => Boolean(snapshot.value.gateway?.upstream_ok));

const abnormalCount = computed(() => nodes.value.filter((n) => n.status !== "online").length);

const sortedNodes = computed(() => {
  const rank = { offline: 0, stale: 1, online: 2 };
  return [...nodes.value].sort((a, b) => {
    const ra = rank[a.status] ?? 9;
    const rb = rank[b.status] ?? 9;
    if (ra !== rb) return ra - rb;
    return String(a.node_id).localeCompare(String(b.node_id), undefined, { numeric: true });
  });
});

const filteredNodes = computed(() => {
  const q = searchQuery.value.toLowerCase();
  return sortedNodes.value.filter((n) => {
    if (onlyAbnormal.value && n.status === "online") return false;
    if (q && !String(n.node_id).toLowerCase().includes(q)) return false;
    return true;
  });
});

const totalPages = computed(() => Math.max(1, Math.ceil(filteredNodes.value.length / PAGE_SIZE)));

const pagedNodes = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE;
  return filteredNodes.value.slice(start, start + PAGE_SIZE);
});

const activeNode = computed(() => {
  return nodes.value.find((n) => n.node_id === selectedNodeId.value) || nodes.value[0] || { metrics: {} };
});

const leftTitle = computed(() => {
  if (leftMode.value === "trend") return `实时功耗趋势（集群 & ${selectedNodeId.value || "--"}）`;
  if (leftMode.value === "rank") return "各节点实时功耗排行";
  if (leftMode.value === "today") return "当天集群功耗走势（分钟级）";
  if (leftMode.value === "7d") return "近 7 天集群每日用电与功率";
  return "近 30 天集群每日用电与功率";
});

const statsSubtitle = computed(() => {
  if (leftMode.value === "trend" || leftMode.value === "rank") return "";
  const s = statsData.value;
  return `累计用电: ${fmtKwh(s.total_kwh)} kWh · 平均功率: ${fmt(s.avg_power)} W · 峰值功率: ${fmt(s.peak_power)} W`;
});

function fmt(val) {
  return Number(val || 0).toFixed(1);
}

function fmtKwh(val) {
  return Number(val || 0).toFixed(3);
}

function fmtPct(val) {
  return (Number(val || 0) * 100).toFixed(1);
}

function sharePct(power) {
  const total = Number(cluster.value.total_power || 0);
  if (total <= 0) return "0.0";
  return ((Number(power || 0) / total) * 100).toFixed(1);
}

function shareBarWidth(power) {
  if (nodes.value.length === 0) return "0%";
  const maxPower = Math.max(...nodes.value.map((n) => Number(n.summary?.total_power || 0)), 1);
  return `${Math.min(100, (Number(power || 0) / maxPower) * 100).toFixed(1)}%`;
}

function formatClock(iso) {
  if (!iso) return "--:--:--";
  return iso.replace("T", " ").slice(11, 19);
}

function formatDateTime(iso) {
  if (!iso) return "--";
  return iso.replace("T", " ").slice(0, 19);
}

function levelText(lvl) {
  if (lvl === "error") return "异常";
  if (lvl === "warn") return "延迟";
  return "恢复";
}

function statusDot(status) {
  if (status === "online") return "ok";
  if (status === "stale") return "warn";
  return "err";
}

function statusText(status) {
  if (status === "online") return "在线";
  if (status === "stale") return "延迟";
  return "离线";
}

function renderLeftChart() {
  if (!leftChart) return;
  if (leftMode.value === "trend") {
    const pts = history.value.points || [];
    const times = pts.map((p) => formatClock(p.ts));
    const sel = selectedNodeId.value;
    leftChart.setOption(
      {
        backgroundColor: "transparent",
        tooltip: { trigger: "axis" },
        legend: { data: ["集群总功耗", sel || "选中节点"], textStyle: { color: "#cbd5e1" } },
        grid: { left: 54, right: 54, top: 38, bottom: 28 },
        xAxis: { type: "category", data: times, axisLabel: { color: "#94a3b8" } },
        yAxis: [
          { type: "value", name: "集群 (W)", axisLabel: { color: "#94a3b8" }, splitLine: { lineStyle: { color: "#1e293b" } } },
          { type: "value", name: "单节点 (W)", axisLabel: { color: "#94a3b8" }, splitLine: { show: false } },
        ],
        series: [
          {
            name: "集群总功耗",
            type: "line",
            smooth: true,
            showSymbol: false,
            yAxisIndex: 0,
            lineStyle: { width: 2.5, color: "#38bdf8" },
            data: pts.map((p) => p.cluster_total ?? 0),
          },
          {
            name: sel || "选中节点",
            type: "line",
            smooth: true,
            showSymbol: false,
            yAxisIndex: 1,
            lineStyle: { width: 2, color: "#22c55e" },
            data: pts.map((p) => p.nodes?.[sel] ?? 0),
          },
        ],
      },
      true
    );
  } else if (leftMode.value === "rank") {
    const sorted = [...nodes.value].sort((a, b) => (a.summary?.total_power ?? 0) - (b.summary?.total_power ?? 0));
    leftChart.setOption(
      {
        backgroundColor: "transparent",
        tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
        grid: { left: 64, right: 26, top: 20, bottom: 26 },
        xAxis: { type: "value", name: "W", axisLabel: { color: "#94a3b8" }, splitLine: { lineStyle: { color: "#1e293b" } } },
        yAxis: { type: "category", data: sorted.map((n) => n.node_id), axisLabel: { color: "#e2e8f0" } },
        series: [
          {
            name: "节点总功耗",
            type: "bar",
            barMaxWidth: 20,
            data: sorted.map((n) => ({
              value: n.summary?.total_power ?? 0,
              itemStyle: { color: n.node_id === selectedNodeId.value ? "#38bdf8" : "#0284c7" },
            })),
          },
        ],
      },
      true
    );
  } else if (leftMode.value === "today") {
    const pts = statsData.value.points || [];
    leftChart.setOption(
      {
        backgroundColor: "transparent",
        tooltip: { trigger: "axis" },
        legend: { data: ["分钟平均功耗", "分钟峰值功耗"], textStyle: { color: "#cbd5e1" } },
        grid: { left: 54, right: 20, top: 38, bottom: 28 },
        xAxis: { type: "category", data: pts.map((p) => p.ts.slice(11, 16)), axisLabel: { color: "#94a3b8" } },
        yAxis: { type: "value", name: "W", axisLabel: { color: "#94a3b8" }, splitLine: { lineStyle: { color: "#1e293b" } } },
        series: [
          { name: "分钟平均功耗", type: "line", smooth: true, showSymbol: false, data: pts.map((p) => p.avg_power), itemStyle: { color: "#38bdf8" } },
          { name: "分钟峰值功耗", type: "line", smooth: true, showSymbol: false, data: pts.map((p) => p.max_power), itemStyle: { color: "#f59e0b" } },
        ],
      },
      true
    );
  } else {
    const days = statsData.value.days || [];
    leftChart.setOption(
      {
        backgroundColor: "transparent",
        tooltip: { trigger: "axis" },
        legend: { data: ["日耗电量 (kWh)", "日均功率 (W)"], textStyle: { color: "#cbd5e1" } },
        grid: { left: 54, right: 54, top: 38, bottom: 28 },
        xAxis: { type: "category", data: days.map((d) => d.date.slice(5)), axisLabel: { color: "#94a3b8" } },
        yAxis: [
          { type: "value", name: "kWh", axisLabel: { color: "#94a3b8" }, splitLine: { lineStyle: { color: "#1e293b" } } },
          { type: "value", name: "W", axisLabel: { color: "#94a3b8" }, splitLine: { show: false } },
        ],
        series: [
          { name: "日耗电量 (kWh)", type: "bar", barMaxWidth: 28, yAxisIndex: 0, data: days.map((d) => d.energy_kwh), itemStyle: { color: "#22c55e" } },
          { name: "日均功率 (W)", type: "line", yAxisIndex: 1, data: days.map((d) => d.avg_power), itemStyle: { color: "#38bdf8" } },
        ],
      },
      true
    );
  }
}

function renderRightCharts() {
  const m = activeNode.value.metrics || {};
  if (domain1Chart) {
    const labels = Array.from({ length: 8 }, (_, i) => `N${i}`);
    domain1Chart.setOption({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis" },
      legend: { data: ["CORE", "DDQ"], textStyle: { color: "#cbd5e1" } },
      grid: { left: 42, right: 16, top: 32, bottom: 24 },
      xAxis: { type: "category", data: labels, axisLabel: { color: "#94a3b8" } },
      yAxis: { type: "value", name: "W", axisLabel: { color: "#94a3b8" }, splitLine: { lineStyle: { color: "#1e293b" } } },
      series: [
        { name: "CORE", type: "bar", data: labels.map((l) => m[`${l}_CORE`] ?? 0), itemStyle: { color: "#38bdf8" } },
        { name: "DDQ", type: "bar", data: labels.map((l) => m[`${l}_DDQ`] ?? 0), itemStyle: { color: "#a855f7" } },
      ],
    });
  }

  if (domain2Chart) {
    const labels = Array.from({ length: 6 }, (_, i) => `#${i}`);
    domain2Chart.setOption({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis" },
      legend: { data: ["NI", "HVM"], textStyle: { color: "#cbd5e1" } },
      grid: { left: 42, right: 16, top: 32, bottom: 24 },
      xAxis: { type: "category", data: labels, axisLabel: { color: "#94a3b8" } },
      yAxis: { type: "value", name: "W", axisLabel: { color: "#94a3b8" }, splitLine: { lineStyle: { color: "#1e293b" } } },
      series: [
        { name: "NI", type: "bar", data: Array.from({ length: 6 }, (_, i) => m[`NI${i}_OUT`] ?? 0), itemStyle: { color: "#22c55e" } },
        { name: "HVM", type: "bar", data: Array.from({ length: 6 }, (_, i) => m[`HVM${i}_OUT`] ?? 0), itemStyle: { color: "#f59e0b" } },
      ],
    });
  }
}

function renderAll() {
  renderLeftChart();
  renderRightCharts();
}

async function fetchAlerts() {
  try {
    const qs = new URLSearchParams({
      range: logRange.value,
      node: logNodeFilter.value,
      level: logLevelFilter.value,
      page: String(logPage.value),
      page_size: "12",
    });
    const res = await fetch(`/api/alerts?${qs.toString()}`);
    if (res.ok) alertsPage.value = await res.json();
  } catch (_e) {
    // Keep last state on transient glitch.
  }
}

function openLogsModal() {
  logsOpen.value = true;
  fetchAlerts();
}

function changeLogRange(r) {
  logRange.value = r;
  logPage.value = 1;
  fetchAlerts();
}

function onLogFilterChange() {
  logPage.value = 1;
  fetchAlerts();
}

function goLogPage(p) {
  logPage.value = p;
  fetchAlerts();
}

async function fetchStats(rangeName) {
  try {
    const res = await fetch(`/api/stats?range=${encodeURIComponent(rangeName)}`);
    if (res.ok) {
      statsData.value = await res.json();
      renderLeftChart();
    }
  } catch (_e) {
    // Keep last stats on transient glitch.
  }
}

function switchLeftMode(mode) {
  leftMode.value = mode;
  if (mode === "today" || mode === "7d" || mode === "30d") {
    fetchStats(mode);
  } else {
    renderLeftChart();
  }
}

async function fetchData() {
  try {
    const [sRes, hRes] = await Promise.all([fetch("/api/snapshot"), fetch("/api/history")]);
    if (sRes.ok) snapshot.value = await sRes.json();
    if (hRes.ok) history.value = await hRes.json();
    if (!selectedNodeId.value && nodes.value.length > 0) {
      selectedNodeId.value = nodes.value[0].node_id;
    }
    statsTick += 1;
    if ((leftMode.value === "today" || leftMode.value === "7d" || leftMode.value === "30d") && statsTick % 5 === 0) {
      await fetchStats(leftMode.value);
    }
    renderAll();
  } catch (_e) {
    // Keep last state on transient glitch.
  }
}

function handleResize() {
  leftChart?.resize();
  domain1Chart?.resize();
  domain2Chart?.resize();
}

watch([searchQuery, onlyAbnormal], () => {
  currentPage.value = 1;
});

watch(totalPages, (tp) => {
  if (currentPage.value > tp) currentPage.value = tp;
});

watch(selectedNodeId, () => renderAll());

onMounted(() => {
  leftChart = echarts.init(leftEl.value);
  domain1Chart = echarts.init(domain1El.value);
  domain2Chart = echarts.init(domain2El.value);
  window.addEventListener("resize", handleResize);
  fetchData();
  timer = setInterval(() => {
    if (autoRefresh.value) fetchData();
  }, 2000);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", handleResize);
  if (timer) clearInterval(timer);
  leftChart?.dispose();
  domain1Chart?.dispose();
  domain2Chart?.dispose();
});
</script>
