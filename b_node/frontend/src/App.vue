<template>
  <div class="dashboard">
    <header class="topbar">
      <div class="title-group">
        <h1>超算集群能耗实时监控大屏</h1>
        <div class="subtitle">集群节点实时功耗监测</div>
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
          <span>最新采样: {{ formatBeijingClock(snapshot.sampled_at ?? snapshot.updated_at) }}</span>
        </div>
        <button class="badge log-btn" @click="openLogsModal">
          <span class="dot" :class="abnormalCount > 0 ? 'err' : 'ok'"></span>
          <span>{{ (cluster.today_alerts ?? 0) > 0 ? `运行日志 (${cluster.today_alerts})` : "运行日志 · 正常" }}</span>
        </button>
        <button class="btn" :class="{ active: autoRefresh }" @click="autoRefresh = !autoRefresh">
          {{ autoRefresh ? `自动刷新: 开 (${REFRESH_SECONDS}s)` : "自动刷新: 暂停" }}
        </button>
      </div>
    </header>

    <section class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">集群总功耗</div>
        <div class="kpi-val">{{ fmt(cluster.total_power) }}<span class="kpi-unit">W</span></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">今日已用电量</div>
        <div class="kpi-val">{{ cluster.today_kwh == null ? "—" : fmtKwh(cluster.today_kwh) }}<span class="kpi-unit">kWh</span></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">近 7 天用电量</div>
        <div class="kpi-val">{{ cluster.energy_7d_days ? fmtKwh(cluster.energy_7d_kwh) : "—" }}<span class="kpi-unit">kWh</span></div>
        <div class="kpi-note" v-if="cluster.energy_7d_days">有采样 {{ cluster.energy_7d_days }} / 7 天</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">近 5 分钟功耗峰值</div>
        <div class="kpi-val">{{ recentPeak ? fmtKw(recentPeak.cluster_total) : "—" }}<span class="kpi-unit">kW</span></div>
        <div class="kpi-note" v-if="recentPeak">{{ formatBeijingClock(recentPeak.ts) }} 北京时间</div>
      </div>
      <div class="kpi-card" v-if="topRack">
        <div class="kpi-label">当前功耗最高机柜</div>
        <div class="kpi-val">{{ fmtKw(topRack.watts) }}<span class="kpi-unit">kW</span></div>
        <div class="kpi-note">{{ topRack.rack_id }} · {{ topRack.online_nodes }} 个节点</div>
      </div>
      <div class="kpi-card" v-else>
        <div class="kpi-label">采样过期节点</div>
        <div class="kpi-val" :class="{ 'kpi-warn': gatewayOk && staleCount > 0 }">{{ gatewayOk ? staleCount : "—" }}<span class="kpi-unit">个</span></div>
      </div>
    </section>

    <section ref="overviewEl" class="panel" style="margin-bottom: 12px">
      <div class="panel-header" style="flex-wrap: wrap; gap: 8px">
        <div class="panel-title">
          {{ overviewByRack ? `机柜运行总览（${filteredRacks.length} 组）` : `节点运行总览（共 ${filteredNodes.length} 个节点 · 当前选中: ${selectedNodeId || "--"}）` }}
        </div>
        <div class="toolbar">
          <div class="toolbar" role="group" aria-label="运行总览维度">
            <button type="button" class="btn" :class="{ active: overviewByRack }" :aria-pressed="overviewByRack" @click="switchOverview(true)">按机柜</button>
            <button type="button" class="btn" :class="{ active: !overviewByRack }" :aria-pressed="!overviewByRack" @click="switchOverview(false)">按节点</button>
          </div>
          <button v-if="!overviewByRack && selectedRackId !== undefined" type="button" class="btn active"
            aria-label="清除机柜筛选" @click="switchOverview(false)">{{ rackLabel(selectedRackId) }} ×</button>
          <input
            v-model.trim="searchQuery"
            class="search-input"
            type="text"
            :placeholder="overviewByRack ? '搜索机柜编号...' : '搜索节点名...'"
          />
          <button
            class="btn"
            :class="{ active: onlyAbnormal }"
            @click="onlyAbnormal = !onlyAbnormal"
          >
            仅看异常 ({{ overviewAbnormalCount }})
          </button>
          <button class="btn" :disabled="currentPage <= 1" @click="currentPage--">上一页</button>
          <span class="page-info">{{ currentPage }} / {{ totalPages }} 页</span>
          <button class="btn" :disabled="currentPage >= totalPages" @click="currentPage++">下一页</button>
        </div>
      </div>
      <div v-if="pagedOverview.length === 0" class="subtitle" style="padding: 18px 0; text-align: center">
        {{ overviewByRack ? "暂无匹配的机柜数据" : "暂无匹配的节点数据" }}
      </div>
      <div v-else-if="overviewByRack" class="nodes-matrix">
        <button v-for="rack in pagedOverview" :key="rack.id ?? ''" type="button"
          class="node-tile rack-tile" @click="openRack(rack.id)">
          <span class="tile-top">
            <span class="tile-name">{{ rackLabel(rack.id) }}</span>
            <span class="tile-status"><span class="dot" :class="!gatewayOk ? 'err' : rack.online === rack.total ? 'ok' : 'warn'"></span>
              {{ !gatewayOk ? "连接中断" : rack.online === rack.total ? "采样正常" : "有异常" }}</span>
          </span>
          <span class="tile-power">{{ gatewayOk && rack.watts != null ? fmtKw(rack.watts) : "—" }} <span class="kpi-unit">kW</span></span>
          <span class="tile-sub">有效采样 {{ gatewayOk ? rack.online : "—" }} / {{ rack.total }} 节点</span>
        </button>
      </div>
      <div v-else class="nodes-matrix">
        <div
          v-for="node in pagedOverview"
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
             <div v-if="leftMode === 'rank' && rankedRows.length === 0" class="subtitle">暂无有效的实时功耗数据</div>
             <div v-else-if="leftMode === 'rank' && rankByRack" class="subtitle">点击机柜查看所属节点</div>
          </div>
          <div class="toolbar">
            <div v-if="leftMode === 'rank'" class="toolbar" role="group" aria-label="功耗排行维度">
              <button type="button" class="btn" :class="{ active: rankByRack }" :aria-pressed="rankByRack" @click="rankByRack = true">按机柜</button>
              <button type="button" class="btn" :class="{ active: !rankByRack }" :aria-pressed="!rankByRack" @click="rankByRack = false">按节点</button>
            </div>
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
                <option v-for="nodeId in logNodeOptions" :key="nodeId" :value="nodeId">{{ nodeId }}</option>
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
            暂无符合条件的运行事件。
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
import { beijingDate, dayPowerSeries, formatBeijingClock } from "./dashboardTime.js";
import { powerSeries } from "./powerSeries.js";

const PAGE_SIZE = 10;
const REFRESH_SECONDS = 5;
const REFRESH_MS = REFRESH_SECONDS * 1000;
const leftModes = [
  { id: "trend", label: "实时趋势" },
  { id: "rank", label: "功耗排行" },
  { id: "today", label: "当天" },
  { id: "7d", label: "近 7 天" },
  { id: "30d", label: "近 30 天" },
];

const snapshot = ref({ cluster: {}, nodes: [], alerts: [], gateway: {} });
const history = ref({ points: [] });
const nodeHistory = ref({ node_id: "", samples: [] });
const statsData = ref({ range: "today", total_kwh: 0, avg_power: 0, peak_power: 0, points: [], days: [] });
const autoRefresh = ref(true);
const selectedNodeId = ref("");
const leftMode = ref("trend");
const overviewByRack = ref(false);
const rankByRack = ref(true);
const selectedRackId = ref(undefined);
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
const logNodeOptions = computed(() => {
  const ids = new Set([...(alertsPage.value.nodes ?? []), ...nodes.value.map((node) => node.node_id)]);
  if (logNodeFilter.value !== "all") ids.add(logNodeFilter.value);
  return [...ids].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
});

const leftEl = ref(null);
const overviewEl = ref(null);

let leftChart = null;
let timer = null;
let statsTick = 0;
let nodeHistoryRequest = null;

const cluster = computed(() => snapshot.value.cluster || {});
const nodes = computed(() => snapshot.value.nodes || []);
const alerts = computed(() => snapshot.value.alerts || []);
const gatewayOk = computed(() => Boolean(snapshot.value.gateway?.upstream_ok));
const topRack = computed(() => gatewayOk.value && cluster.value.online_nodes > 0 &&
  cluster.value.mapped_online_nodes === cluster.value.online_nodes ? snapshot.value.racks?.[0] : null);
const recentPeak = computed(() => (history.value.points ?? []).reduce((peak, point) => {
  if (!Number.isFinite(point.cluster_total)) return peak;
  return !peak || point.cluster_total >= peak.cluster_total ? point : peak;
}, null));
const staleCount = computed(() => nodes.value.filter((node) => node.status === "stale").length);

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

function rackLabel(id) {
  return id ?? "未映射节点";
}

const rackGroups = computed(() => {
  const wattsByRack = new Map((snapshot.value.racks ?? []).map((rack) => [rack.rack_id, rack.watts]));
  const groups = new Map();
  for (const node of nodes.value) {
    const id = node.rack_id || null;
    if (!groups.has(id)) groups.set(id, { id, watts: null, online: 0, total: 0 });
    const group = groups.get(id);
    group.total++;
    const watts = node.summary?.total_power;
    if (node.status === "online" && Number.isFinite(watts)) {
      group.online++;
      group.watts = (group.watts ?? 0) + watts;
    }
  }
  return [...groups.values()]
    .map((group) => ({ ...group, watts: wattsByRack.get(group.id) ?? group.watts }))
    .sort((a, b) => a.id === null ? 1 : b.id === null ? -1 : a.id.localeCompare(b.id, undefined, { numeric: true }));
});

const scopedNodes = computed(() => selectedRackId.value === undefined ? sortedNodes.value :
  sortedNodes.value.filter((node) => (node.rack_id || null) === selectedRackId.value));

const overviewAbnormalCount = computed(() => overviewByRack.value
  ? rackGroups.value.filter((rack) => rack.online < rack.total).length
  : scopedNodes.value.filter((node) => node.status !== "online").length);

const filteredRacks = computed(() => {
  const query = searchQuery.value.toLowerCase();
  return rackGroups.value.filter((rack) => (!onlyAbnormal.value || rack.online < rack.total) &&
    rackLabel(rack.id).toLowerCase().includes(query));
});

const filteredNodes = computed(() => {
  const q = searchQuery.value.toLowerCase();
  return scopedNodes.value.filter((n) => {
    if (onlyAbnormal.value && n.status === "online") return false;
    if (q && !String(n.node_id).toLowerCase().includes(q)) return false;
    return true;
  });
});

const overviewRows = computed(() => overviewByRack.value ? filteredRacks.value : filteredNodes.value);
const totalPages = computed(() => Math.max(1, Math.ceil(overviewRows.value.length / PAGE_SIZE)));

const pagedOverview = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE;
  return overviewRows.value.slice(start, start + PAGE_SIZE);
});

const rankedRows = computed(() => {
  if (!gatewayOk.value) return [];
  const rows = rankByRack.value ? rackGroups.value
    .filter((rack) => rack.id !== null && Number.isFinite(rack.watts))
    .map((rack) => ({ ...rack, name: rack.id, value: rack.watts / 1000 }))
    : scopedNodes.value.filter((node) => node.status === "online" && Number.isFinite(node.summary?.total_power))
      .map((node) => ({ name: node.node_id, value: node.summary.total_power }));
  return rows.sort((a, b) => b.value - a.value || a.name.localeCompare(b.name, undefined, { numeric: true })).slice(0, 10);
});

function switchOverview(byRack) {
  overviewByRack.value = byRack;
  selectedRackId.value = undefined;
  searchQuery.value = "";
  currentPage.value = 1;
}

function openRack(id) {
  overviewByRack.value = false;
  selectedRackId.value = id;
  searchQuery.value = "";
  onlyAbnormal.value = false;
  currentPage.value = 1;
  if (!scopedNodes.value.some((node) => node.node_id === selectedNodeId.value)) {
    selectedNodeId.value = scopedNodes.value[0]?.node_id ?? "";
  }
}

const leftTitle = computed(() => {
  if (leftMode.value === "trend") return `实时功耗趋势（集群 & ${selectedNodeId.value || "--"}）`;
  if (leftMode.value === "rank") {
    if (rankByRack.value) return "机柜实时功耗排行（前 10）";
    return selectedRackId.value === undefined ? "节点实时功耗排行（前 10）" : `${rackLabel(selectedRackId.value)} · 节点实时功耗排行（前 10）`;
  }
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

function fmtKw(watts) {
  return (watts / 1000).toFixed(2);
}

function formatPowerTooltip(points) {
  if (!points.length) return "";
  const rows = points.filter((point) => Number.isFinite(point.value[1]))
    .map((point) => `${point.seriesName}  ${fmt(point.value[1])} W`);
  return [formatBeijingClock(points[0].axisValue), ...rows].join("\n");
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
    const sel = selectedNodeId.value;
    const samples = nodeHistory.value.node_id === sel ? nodeHistory.value.samples : [];
    leftChart.setOption(
      {
        backgroundColor: "transparent",
        tooltip: { trigger: "axis", renderMode: "richText", formatter: formatPowerTooltip },
        legend: { data: ["集群总功耗", sel || "选中节点"], textStyle: { color: "#cbd5e1" } },
        grid: { left: 54, right: 54, top: 38, bottom: 28 },
        useUTC: true,
        xAxis: { type: "time", axisLabel: { color: "#94a3b8", formatter: formatBeijingClock } },
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
            connectNulls: false,
            data: powerSeries(pts, "ts", "cluster_total", REFRESH_MS),
          },
          {
            name: sel || "选中节点",
            type: "line",
            smooth: true,
            showSymbol: false,
            yAxisIndex: 1,
            lineStyle: { width: 2, color: "#22c55e" },
            connectNulls: false,
            data: powerSeries(samples, "collected_at", "watts", REFRESH_MS),
          },
        ],
      },
      true
    );
  } else if (leftMode.value === "rank") {
    const byRack = rankByRack.value;
    const rows = rankedRows.value;
    leftChart.setOption(
      {
        backgroundColor: "transparent",
        tooltip: byRack ? {
          trigger: "item", renderMode: "richText",
          formatter: ({ data }) => `${data.name}\n实时总功耗 ${data.value.toFixed(2)} kW\n有效采样 ${data.online} / ${data.total} 节点`,
        } : { trigger: "axis", axisPointer: { type: "shadow" } },
        grid: { left: 20, right: 26, top: 20, bottom: 26, containLabel: true },
        xAxis: { type: "value", name: byRack ? "kW" : "W", axisLabel: { color: "#94a3b8" }, splitLine: { lineStyle: { color: "#1e293b" } } },
        yAxis: { type: "category", inverse: true, data: rows.map((row) => row.name), axisLabel: { color: "#e2e8f0" } },
        series: [
          {
            name: byRack ? "机柜总功耗" : "节点总功耗",
            type: "bar",
            barMaxWidth: 20,
            data: rows.map((row) => ({
              ...row,
              itemStyle: { color: row.name === (byRack ? selectedRackId.value : selectedNodeId.value) ? "#38bdf8" : "#0284c7" },
            })),
          },
        ],
      },
      true
    );
  } else if (leftMode.value === "today") {
    const pts = statsData.value.points || [];
    const day = dayPowerSeries(statsData.value.date ?? beijingDate(Date.now()), pts);
    leftChart.setOption(
      {
        backgroundColor: "transparent",
        tooltip: { trigger: "axis", renderMode: "richText", formatter: formatPowerTooltip },
        legend: { data: ["分钟平均功耗", "分钟峰值功耗"], textStyle: { color: "#cbd5e1" } },
        grid: { left: 54, right: 20, top: 38, bottom: 28 },
        useUTC: true,
        xAxis: {
          type: "time", min: day.start, max: day.end, splitNumber: 8,
          axisLabel: { color: "#94a3b8", showMinLabel: true, showMaxLabel: true,
            formatter: (time) => time === day.end ? "24:00" : formatBeijingClock(time).slice(0, 5) },
        },
        yAxis: { type: "value", name: "W", axisLabel: { color: "#94a3b8" }, splitLine: { lineStyle: { color: "#1e293b" } } },
        series: [
          { name: "分钟平均功耗", type: "line", smooth: true, showSymbol: false, connectNulls: false, data: day.average, itemStyle: { color: "#38bdf8" } },
          { name: "分钟峰值功耗", type: "line", smooth: true, showSymbol: false, connectNulls: false, data: day.peak, itemStyle: { color: "#f59e0b" } },
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
    if (mode === "trend") fetchNodeHistory();
  }
}

async function fetchNodeHistory() {
  const node = selectedNodeId.value;
  const points = history.value.points ?? [];
  if (leftMode.value !== "trend" || !node || !points.length) return;

  const query = new URLSearchParams({ node, from: points[0].ts, to: points[points.length - 1].ts });
  const path = `/api/node-history?${query}`;
  if (nodeHistoryRequest?.path === path) return;
  nodeHistoryRequest?.controller.abort();
  const controller = new AbortController();
  nodeHistoryRequest = { path, controller };
  try {
    const response = await fetch(path, { signal: controller.signal });
    if (!response.ok) return;
    const result = await response.json();
    if (nodeHistoryRequest?.controller === controller && selectedNodeId.value === node) {
      nodeHistory.value = result;
      if (leftMode.value === "trend") renderLeftChart();
    }
  } catch (_e) {
    // Keep this node's last history when a request is interrupted.
  } finally {
    if (nodeHistoryRequest?.controller === controller) nodeHistoryRequest = null;
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
    renderLeftChart();
    fetchNodeHistory();
    if (logsOpen.value) fetchAlerts();
  } catch (_e) {
    // Keep last state on transient glitch.
  }
}

function handleResize() {
  leftChart?.resize();
}

function handleRankingClick({ componentType, data }) {
  if (leftMode.value !== "rank" || !rankByRack.value || componentType !== "series" || !data?.id) return;
  openRack(data.id);
  overviewEl.value?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

watch([searchQuery, onlyAbnormal], () => {
  currentPage.value = 1;
});

watch(totalPages, (tp) => {
  if (currentPage.value > tp) currentPage.value = tp;
});

watch(selectedNodeId, () => {
  renderLeftChart();
  fetchNodeHistory();
});

watch([rankByRack, selectedRackId], () => {
  if (leftMode.value === "rank") renderLeftChart();
});

onMounted(() => {
  leftChart = echarts.init(leftEl.value);
  leftChart.on("click", handleRankingClick);
  window.addEventListener("resize", handleResize);
  fetchData();
  timer = setInterval(() => {
    if (autoRefresh.value) fetchData();
  }, REFRESH_MS);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", handleResize);
  if (timer) clearInterval(timer);
  nodeHistoryRequest?.controller.abort();
  leftChart?.dispose();
});
</script>
