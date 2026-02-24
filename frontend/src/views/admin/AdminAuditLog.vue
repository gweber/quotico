<script setup lang="ts">
import { ref, onMounted, watch } from "vue";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

const api = useApi();
const toast = useToast();

interface AuditEntry {
  id: string;
  timestamp: string;
  actor_id: string;
  target_id: string;
  action: string;
  metadata: Record<string, unknown>;
  ip_truncated: string;
}

interface AuditResponse {
  total: number;
  offset: number;
  limit: number;
  items: AuditEntry[];
}

const entries = ref<AuditEntry[]>([]);
const total = ref(0);
const loading = ref(true);
const error = ref(false);
const exporting = ref(false);

// Filters
const actionFilter = ref("");
const actorFilter = ref("");
const targetFilter = ref("");
const dateFrom = ref("");
const dateTo = ref("");
const page = ref(0);
const pageSize = 50;

// Available actions for dropdown
const actions = ref<string[]>([]);

async function fetchActions() {
  try {
    actions.value = await api.get<string[]>("/admin/audit-logs/actions");
  } catch {
    // silently fail
  }
}

async function fetchLogs() {
  loading.value = true;
  error.value = false;
  try {
    const params: Record<string, string> = {
      limit: String(pageSize),
      offset: String(page.value * pageSize),
    };
    if (actionFilter.value) params.action = actionFilter.value;
    if (actorFilter.value) params.actor_id = actorFilter.value;
    if (targetFilter.value) params.target_id = targetFilter.value;
    if (dateFrom.value) params.date_from = dateFrom.value;
    if (dateTo.value) params.date_to = dateTo.value;

    const data = await api.get<AuditResponse>("/admin/audit-logs", params);
    entries.value = data.items;
    total.value = data.total;
  } catch {
    error.value = true;
  } finally {
    loading.value = false;
  }
}

async function exportCsv() {
  exporting.value = true;
  try {
    const params: Record<string, string> = {};
    if (actionFilter.value) params.action = actionFilter.value;
    if (actorFilter.value) params.actor_id = actorFilter.value;
    if (targetFilter.value) params.target_id = targetFilter.value;
    if (dateFrom.value) params.date_from = dateFrom.value;
    if (dateTo.value) params.date_to = dateTo.value;

    const queryStr = new URLSearchParams(params).toString();
    const url = `/api/admin/audit-logs/export${queryStr ? "?" + queryStr : ""}`;

    const resp = await fetch(url, { credentials: "include" });
    if (!resp.ok) throw new Error("Export fehlgeschlagen.");

    const blob = await resp.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = "quotico-audit-logs.csv";
    a.click();
    URL.revokeObjectURL(blobUrl);
    toast.success("CSV-Export heruntergeladen.");
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "Export fehlgeschlagen.";
    toast.error(msg);
  } finally {
    exporting.value = false;
  }
}

function applyFilters() {
  page.value = 0;
  fetchLogs();
}

function resetFilters() {
  actionFilter.value = "";
  actorFilter.value = "";
  targetFilter.value = "";
  dateFrom.value = "";
  dateTo.value = "";
  page.value = 0;
  fetchLogs();
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleString("de-DE", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatMetadata(meta: Record<string, unknown>) {
  if (!meta || Object.keys(meta).length === 0) return "-";
  return Object.entries(meta)
    .map(([k, v]) => `${k}: ${v}`)
    .join(", ");
}

const totalPages = () => Math.ceil(total.value / pageSize);

watch(page, fetchLogs);

onMounted(() => {
  fetchActions();
  fetchLogs();
});
</script>

<template>
  <div class="max-w-screen-xl mx-auto px-4 py-8">
    <div class="flex items-center justify-between mb-6">
      <div>
        <h1 class="text-2xl font-bold text-text-primary">Audit-Log</h1>
        <p class="text-sm text-text-secondary mt-1">
          Systemaktivitäten durchsuchen und exportieren.
        </p>
      </div>
      <button
        :disabled="exporting"
        class="px-4 py-2 rounded-lg text-sm bg-secondary text-white hover:bg-secondary-hover transition-colors disabled:opacity-50"
        @click="exportCsv"
      >
        {{ exporting ? "Wird exportiert..." : "CSV exportieren" }}
      </button>
    </div>

    <!-- Filters -->
    <div class="bg-surface-1 rounded-card p-4 mb-6">
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3">
        <div>
          <label class="block text-xs text-text-secondary mb-1">Aktion</label>
          <select
            v-model="actionFilter"
            class="w-full px-3 py-2 bg-surface-2 border border-surface-3 rounded-lg text-text-primary text-sm focus:border-primary focus:ring-1 focus:ring-primary"
          >
            <option value="">Alle</option>
            <option v-for="a in actions" :key="a" :value="a">{{ a }}</option>
          </select>
        </div>
        <div>
          <label class="block text-xs text-text-secondary mb-1">Actor-ID</label>
          <input
            v-model="actorFilter"
            type="text"
            placeholder="User-ID"
            class="w-full px-3 py-2 bg-surface-2 border border-surface-3 rounded-lg text-text-primary text-sm placeholder-text-muted focus:border-primary focus:ring-1 focus:ring-primary"
          />
        </div>
        <div>
          <label class="block text-xs text-text-secondary mb-1">Target-ID</label>
          <input
            v-model="targetFilter"
            type="text"
            placeholder="Betroffene ID"
            class="w-full px-3 py-2 bg-surface-2 border border-surface-3 rounded-lg text-text-primary text-sm placeholder-text-muted focus:border-primary focus:ring-1 focus:ring-primary"
          />
        </div>
        <div>
          <label class="block text-xs text-text-secondary mb-1">Von</label>
          <input
            v-model="dateFrom"
            type="date"
            class="w-full px-3 py-2 bg-surface-2 border border-surface-3 rounded-lg text-text-primary text-sm focus:border-primary focus:ring-1 focus:ring-primary"
          />
        </div>
        <div>
          <label class="block text-xs text-text-secondary mb-1">Bis</label>
          <input
            v-model="dateTo"
            type="date"
            class="w-full px-3 py-2 bg-surface-2 border border-surface-3 rounded-lg text-text-primary text-sm focus:border-primary focus:ring-1 focus:ring-primary"
          />
        </div>
      </div>
      <div class="flex gap-2 mt-3">
        <button
          class="px-4 py-2 rounded-lg text-sm bg-primary text-surface-0 hover:bg-primary-hover transition-colors"
          @click="applyFilters"
        >
          Filtern
        </button>
        <button
          class="px-4 py-2 rounded-lg text-sm text-text-secondary hover:text-text-primary hover:bg-surface-2 transition-colors"
          @click="resetFilters"
        >
          Zurücksetzen
        </button>
      </div>
    </div>

    <!-- Results -->
    <div class="bg-surface-1 rounded-card overflow-hidden">
      <div v-if="loading" class="p-8 text-center text-text-muted text-sm">
        Lade Audit-Logs...
      </div>

      <div v-else-if="error" class="text-center py-12">
        <p class="text-text-muted mb-3">Error loading.</p>
        <button class="text-sm text-primary hover:underline" @click="fetchLogs">Try again</button>
      </div>

      <div v-else-if="entries.length === 0" class="p-8 text-center text-text-muted text-sm">
        Keine Einträge gefunden.
      </div>

      <div v-else class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-surface-3 text-text-secondary text-left">
              <th class="px-4 py-3 font-medium">Zeitpunkt</th>
              <th class="px-4 py-3 font-medium">Aktion</th>
              <th class="px-4 py-3 font-medium">Actor</th>
              <th class="px-4 py-3 font-medium">Target</th>
              <th class="px-4 py-3 font-medium">Details</th>
              <th class="px-4 py-3 font-medium">IP</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="entry in entries"
              :key="entry.id"
              class="border-b border-surface-3/50 hover:bg-surface-2/50 transition-colors"
            >
              <td class="px-4 py-3 text-text-primary whitespace-nowrap font-mono text-xs">
                {{ formatDate(entry.timestamp) }}
              </td>
              <td class="px-4 py-3">
                <span class="inline-block px-2 py-0.5 rounded text-xs font-medium bg-surface-2 text-text-primary">
                  {{ entry.action }}
                </span>
              </td>
              <td class="px-4 py-3 text-text-secondary font-mono text-xs truncate max-w-[120px]" :title="entry.actor_id">
                {{ entry.actor_id }}
              </td>
              <td class="px-4 py-3 text-text-secondary font-mono text-xs truncate max-w-[120px]" :title="entry.target_id">
                {{ entry.target_id }}
              </td>
              <td class="px-4 py-3 text-text-muted text-xs truncate max-w-[200px]" :title="formatMetadata(entry.metadata)">
                {{ formatMetadata(entry.metadata) }}
              </td>
              <td class="px-4 py-3 text-text-muted font-mono text-xs">
                {{ entry.ip_truncated || "-" }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Pagination -->
      <div v-if="total > pageSize" class="flex items-center justify-between px-4 py-3 border-t border-surface-3">
        <span class="text-xs text-text-muted">
          {{ total }} Einträge gesamt
        </span>
        <div class="flex gap-2">
          <button
            :disabled="page === 0"
            class="px-3 py-1 rounded text-xs text-text-secondary hover:bg-surface-2 disabled:opacity-40 transition-colors"
            @click="page--"
          >
            Zurück
          </button>
          <span class="px-2 py-1 text-xs text-text-muted">
            Seite {{ page + 1 }} / {{ totalPages() }}
          </span>
          <button
            :disabled="(page + 1) >= totalPages()"
            class="px-3 py-1 rounded text-xs text-text-secondary hover:bg-surface-2 disabled:opacity-40 transition-colors"
            @click="page++"
          >
            Weiter
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
