<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

const api = useApi();
const toast = useToast();

interface Provider {
  label: string;
  status: string;
  requests_used?: number | string;
  requests_remaining?: number | string | null;
}

interface Worker {
  id: string;
  label: string;
  provider: string | null;
  triggerable: boolean;
  last_synced: string | null;
  last_metrics: Record<string, number> | null;
  next_run: string | null;
}

interface StatusResponse {
  providers: Record<string, Provider>;
  workers: Worker[];
}

interface HeartbeatConfig {
  xg_crawler_tick_seconds: number;
  _source: Record<string, string>;
}

const data = ref<StatusResponse | null>(null);
const loading = ref(true);
const error = ref(false);
const syncing = ref<string | null>(null); // worker_id currently being triggered

// Heartbeat runtime config
const heartbeatConfig = ref<HeartbeatConfig | null>(null);
const hbTickInput = ref(2);
const hbSaving = ref(false);

async function fetchHeartbeatConfig() {
  try {
    const cfg = await api.get<HeartbeatConfig>("/admin/heartbeat/config");
    heartbeatConfig.value = cfg;
    hbTickInput.value = cfg.xg_crawler_tick_seconds;
  } catch {
    // non-critical
  }
}

async function saveHeartbeatConfig() {
  hbSaving.value = true;
  try {
    await api.patch("/admin/heartbeat/config", {
      xg_crawler_tick_seconds: hbTickInput.value,
    });
    toast.success(`xG crawler tick set to ${hbTickInput.value}s`);
    await fetchHeartbeatConfig();
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : "Save failed");
  } finally {
    hbSaving.value = false;
  }
}

async function fetchStatus() {
  loading.value = true;
  error.value = false;
  try {
    data.value = await api.get<StatusResponse>("/admin/provider-status");
  } catch {
    error.value = true;
  } finally {
    loading.value = false;
  }
}

async function triggerSync(workerId: string) {
  syncing.value = workerId;
  try {
    const result = await api.post<{ message: string; duration_ms: number }>(
      "/admin/trigger-sync",
      { worker_id: workerId }
    );
    toast.success(`${result.message} (${(result.duration_ms / 1000).toFixed(1)}s)`);
    await fetchStatus();
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "Sync fehlgeschlagen";
    toast.error(msg);
  } finally {
    syncing.value = null;
  }
}

function timeAgo(iso: string | null): string {
  if (!iso) return "--";
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 0) return "gleich";
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ${mins % 60}m`;
  return `${Math.floor(hours / 24)}d`;
}

function timeUntil(iso: string | null): string {
  if (!iso) return "--";
  const diff = new Date(iso).getTime() - Date.now();
  if (diff <= 0) return "jetzt";
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

function statusColor(status: string): string {
  if (status === "ok") return "bg-emerald-500/20 text-emerald-400";
  if (status === "circuit_open") return "bg-danger-muted/20 text-danger";
  return "bg-surface-3 text-text-muted";
}

function syncAgeColor(iso: string | null): string {
  if (!iso) return "text-text-muted";
  const mins = (Date.now() - new Date(iso).getTime()) / 60000;
  if (mins < 35) return "text-emerald-400";
  if (mins < 65) return "text-amber-400";
  return "text-danger";
}

onMounted(() => {
  fetchStatus();
  fetchHeartbeatConfig();
});
</script>

<template>
  <div class="max-w-4xl mx-auto p-4">
    <!-- Header -->
    <div class="flex items-center justify-between mb-6">
      <h1 class="text-xl font-bold text-text-primary">Provider & Worker Status</h1>
      <button
        class="px-3 py-1.5 text-sm rounded-lg border border-surface-3 text-text-secondary hover:bg-surface-2 transition-colors"
        :disabled="loading"
        @click="fetchStatus"
      >
        {{ loading ? "..." : "Refresh" }}
      </button>
    </div>

    <!-- Loading skeleton -->
    <div v-if="loading && !data" class="space-y-4">
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div v-for="n in 4" :key="n" class="bg-surface-1 rounded-card h-24 animate-pulse" />
      </div>
      <div class="bg-surface-1 rounded-card h-64 animate-pulse" />
    </div>

    <div v-else-if="error" class="text-center py-12">
      <p class="text-text-muted mb-3">Error loading.</p>
      <button class="text-sm text-primary hover:underline" @click="fetchStatus">Try again</button>
    </div>

    <template v-if="data">
      <!-- Provider Cards -->
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <div
          v-for="(p, key) in data.providers"
          :key="key"
          class="bg-surface-1 rounded-card p-4 border border-surface-3/50"
        >
          <div class="flex items-center justify-between mb-2">
            <p class="text-xs text-text-muted">{{ p.label }}</p>
            <span
              class="text-[10px] px-1.5 py-0.5 rounded-full font-medium uppercase tracking-wide"
              :class="statusColor(p.status)"
            >
              {{ p.status === "circuit_open" ? "Down" : "OK" }}
            </span>
          </div>
          <template v-if="p.requests_used !== undefined">
            <p class="text-lg font-bold text-text-primary tabular-nums font-mono">
              {{ p.requests_remaining ?? "?" }}
            </p>
            <p class="text-[10px] text-text-muted">
              remaining ({{ p.requests_used }} used)
            </p>
          </template>
          <template v-else>
            <p class="text-sm text-text-secondary mt-1">Free API</p>
          </template>
        </div>
      </div>

      <!-- Workers Table -->
      <div class="bg-surface-1 rounded-card border border-surface-3/50 overflow-hidden">
        <div class="px-4 py-3 border-b border-surface-3/50">
          <h2 class="text-sm font-semibold text-text-primary">Background Workers</h2>
        </div>
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="text-left text-xs text-text-muted border-b border-surface-3/30">
                <th class="px-4 py-2 font-medium">Worker</th>
                <th class="px-4 py-2 font-medium">Last Sync</th>
                <th class="px-4 py-2 font-medium">Last Run</th>
                <th class="px-4 py-2 font-medium">Next Run</th>
                <th class="px-4 py-2 font-medium text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="w in data.workers"
                :key="w.id"
                class="border-b border-surface-3/20 last:border-0"
              >
                <td class="px-4 py-2.5">
                  <span class="text-text-primary font-medium">{{ w.label }}</span>
                </td>
                <td class="px-4 py-2.5 font-mono tabular-nums">
                  <span
                    :class="syncAgeColor(w.last_synced)"
                    :title="w.last_synced ?? 'Never'"
                  >
                    {{ timeAgo(w.last_synced) }} ago
                  </span>
                </td>
                <td class="px-4 py-2.5 font-mono tabular-nums text-text-muted text-xs">
                  <template v-if="w.last_metrics">
                    <span class="text-text-secondary">{{ w.last_metrics.matches ?? 0 }}</span>
                    <span> matches, </span>
                    <span :class="(w.last_metrics.odds_changed ?? 0) > 0 ? 'text-amber-400' : 'text-text-muted'">
                      {{ w.last_metrics.odds_changed ?? 0 }} changed
                    </span>
                  </template>
                  <span v-else class="text-text-muted">--</span>
                </td>
                <td class="px-4 py-2.5 font-mono tabular-nums text-text-muted">
                  <span :title="w.next_run ?? ''">
                    in {{ timeUntil(w.next_run) }}
                  </span>
                </td>
                <td class="px-4 py-2.5 text-right">
                  <button
                    v-if="w.triggerable"
                    class="px-3 py-1 text-xs rounded-lg font-medium transition-colors"
                    :class="syncing === w.id
                      ? 'bg-surface-3 text-text-muted cursor-wait'
                      : 'bg-primary/10 text-primary hover:bg-primary/20'"
                    :disabled="syncing !== null"
                    @click="triggerSync(w.id)"
                  >
                    <template v-if="syncing === w.id">
                      <span class="inline-flex items-center gap-1.5">
                        <svg class="animate-spin h-3 w-3" viewBox="0 0 24 24">
                          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none" />
                          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                        </svg>
                        Syncing...
                      </span>
                    </template>
                    <template v-else>Sync</template>
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>

    <!-- Heartbeat Runtime Settings -->
    <div v-if="heartbeatConfig" class="bg-surface-1 rounded-card border border-surface-3/50 mt-6">
      <div class="px-4 py-3 border-b border-surface-3/50">
        <h2 class="text-sm font-semibold text-text-primary">Heartbeat Settings</h2>
      </div>
      <div class="p-4 space-y-4">
        <div class="flex items-center gap-4">
          <label class="text-sm text-text-secondary w-48 shrink-0">
            xG Crawler Tick Interval
          </label>
          <div class="flex items-center gap-2">
            <input
              v-model.number="hbTickInput"
              type="number"
              min="1"
              max="300"
              class="w-20 h-9 text-center text-sm font-mono bg-surface-2 border border-surface-3 rounded-lg text-text-primary focus:border-primary focus:ring-1 focus:ring-primary transition-colors [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none"
            />
            <span class="text-xs text-text-muted">seconds</span>
            <span
              v-if="heartbeatConfig._source?.xg_crawler_tick_seconds"
              class="text-[10px] px-1.5 py-0.5 rounded-full"
              :class="heartbeatConfig._source.xg_crawler_tick_seconds === 'runtime'
                ? 'bg-primary/10 text-primary'
                : 'bg-surface-3 text-text-muted'"
            >
              {{ heartbeatConfig._source.xg_crawler_tick_seconds }}
            </span>
          </div>
          <button
            class="px-3 py-1.5 text-xs rounded-lg font-medium transition-colors"
            :class="hbSaving
              ? 'bg-surface-3 text-text-muted cursor-wait'
              : 'bg-primary/10 text-primary hover:bg-primary/20'"
            :disabled="hbSaving || hbTickInput === heartbeatConfig.xg_crawler_tick_seconds"
            @click="saveHeartbeatConfig"
          >
            {{ hbSaving ? "Saving..." : "Save" }}
          </button>
        </div>
        <p class="text-[11px] text-text-muted leading-relaxed">
          Delay between xG crawler ticks (fresh + deep pointer). Lower = faster crawling but more API calls.
          Takes effect on next tick without restart. (Min: 1s, Max: 300s)
        </p>
      </div>
    </div>
  </div>
</template>
