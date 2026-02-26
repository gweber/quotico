<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";

const { t } = useI18n();
const api = useApi();

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface AuditCheck {
  key: string;
  label: string;
  value: string;
  status: "green" | "yellow" | "red";
  detail?: string | null;
}

interface AuditModule {
  checks: AuditCheck[];
  extras: Record<string, unknown>;
}

interface AuditResponse {
  season_id: number;
  generated_at: string;
  summary: { green: number; yellow: number; red: number; total: number };
  modules: {
    hardening: AuditModule;
    justice_preflight: AuditModule;
    entity_integrity: AuditModule;
    data_guard: AuditModule;
    performance: AuditModule;
  };
}

interface SeasonOption {
  season_id: number;
  season_name: string;
  league_name: string;
}

interface FlaggedMatch {
  match_id: number;
  home: string;
  away: string;
  status: string;
  start_at: string | null;
  score: { home: number | null; away: number | null };
  reasons: string[];
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const seasons = ref<SeasonOption[]>([]);
const selectedSeasonId = ref<number | null>(null);
const auditResult = ref<AuditResponse | null>(null);
const loading = ref(false);
const seasonsLoading = ref(true);
const error = ref("");
const fixLoading = ref<Record<string, boolean>>({});

const expanded = ref<Record<string, boolean>>({
  hardening: true,
  justice_preflight: false,
  entity_integrity: false,
  data_guard: false,
  performance: false,
});

// ---------------------------------------------------------------------------
// Module metadata
// ---------------------------------------------------------------------------

type ModuleKey = keyof AuditResponse["modules"];

const moduleConfig: { key: ModuleKey; titleKey: string; fixLabel?: string; fixAction?: string }[] = [
  { key: "hardening", titleKey: "admin.dataAudit.modules.hardening", fixLabel: "admin.dataAudit.fix.reIngest", fixAction: "re-ingest" },
  { key: "justice_preflight", titleKey: "admin.dataAudit.modules.justice", fixLabel: "admin.dataAudit.fix.repairMetrics", fixAction: "metrics-sync" },
  { key: "entity_integrity", titleKey: "admin.dataAudit.modules.entity", fixLabel: "admin.dataAudit.fix.refreshDiscovery", fixAction: "discovery" },
  { key: "data_guard", titleKey: "admin.dataAudit.modules.guard", fixLabel: "admin.dataAudit.fix.reIngest", fixAction: "re-ingest" },
  { key: "performance", titleKey: "admin.dataAudit.modules.performance" },
];

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function fetchSeasons() {
  seasonsLoading.value = true;
  try {
    const data = await api.get<{ items: Array<{ league_id: number; name: string; available_seasons: Array<{ id: number; name: string }> }> }>(
      "/admin/ingest/discovery"
    );
    const seen = new Set<number>();
    const opts: SeasonOption[] = [];
    for (const lg of data?.items ?? []) {
      for (const s of lg.available_seasons ?? []) {
        if (typeof s.id === "number" && !seen.has(s.id)) {
          seen.add(s.id);
          opts.push({ season_id: s.id, season_name: s.name || String(s.id), league_name: lg.name });
        }
      }
    }
    opts.sort((a, b) => b.season_id - a.season_id);
    seasons.value = opts;
  } catch {
    seasons.value = [];
  } finally {
    seasonsLoading.value = false;
  }
}

async function runAudit() {
  if (!selectedSeasonId.value) return;
  loading.value = true;
  error.value = "";
  auditResult.value = null;
  try {
    const data = await api.get<AuditResponse>(`/admin/ingest/season/${selectedSeasonId.value}/audit`);
    auditResult.value = data;
  } catch (e: unknown) {
    error.value = (e as { message?: string })?.message || "Audit failed.";
  } finally {
    loading.value = false;
  }
}

// ---------------------------------------------------------------------------
// Fix actions
// ---------------------------------------------------------------------------

async function triggerFix(action: string) {
  if (!selectedSeasonId.value) return;
  if (!confirm(t("admin.dataAudit.confirm"))) return;

  fixLoading.value = { ...fixLoading.value, [action]: true };
  try {
    if (action === "re-ingest") {
      await api.post(`/admin/ingest/season/${selectedSeasonId.value}`);
    } else if (action === "metrics-sync") {
      await api.post(`/admin/ingest/season/${selectedSeasonId.value}/metrics-sync`);
    } else if (action === "discovery") {
      await api.get("/admin/ingest/discovery?force=true");
    }
    alert(t("admin.dataAudit.jobStarted"));
  } catch (e: unknown) {
    alert((e as { message?: string })?.message || "Failed to start job.");
  } finally {
    fixLoading.value = { ...fixLoading.value, [action]: false };
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function worstStatus(checks: AuditCheck[]): "green" | "yellow" | "red" {
  if (checks.some((c) => c.status === "red")) return "red";
  if (checks.some((c) => c.status === "yellow")) return "yellow";
  return "green";
}

function dotClass(s: "green" | "yellow" | "red"): string {
  if (s === "green") return "bg-emerald-500";
  if (s === "yellow") return "bg-amber-500";
  return "bg-red-500";
}

function toggleModule(key: string) {
  expanded.value = { ...expanded.value, [key]: !expanded.value[key] };
}

const flaggedMatches = computed<FlaggedMatch[]>(() => {
  const extras = auditResult.value?.modules?.data_guard?.extras;
  return (extras?.flagged_matches as FlaggedMatch[]) ?? [];
});

const eventTypeDist = computed<Record<string, number>>(() => {
  const extras = auditResult.value?.modules?.hardening?.extras;
  return (extras?.event_type_distribution as Record<string, number>) ?? {};
});

const reasonsBreakdown = computed<Record<string, number>>(() => {
  const extras = auditResult.value?.modules?.data_guard?.extras;
  return (extras?.check_reasons_breakdown as Record<string, number>) ?? {};
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

onMounted(fetchSeasons);
</script>

<template>
  <div class="max-w-7xl mx-auto p-4 md:p-6 space-y-4">
    <!-- Header -->
    <div class="rounded-lg border border-surface-3/50 bg-surface-1 p-4 md:p-5">
      <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div>
          <h1 class="text-xl font-bold text-text-primary">{{ t("admin.dataAudit.title") }}</h1>
          <p class="text-sm text-text-muted mt-1">{{ t("admin.dataAudit.subtitle") }}</p>
        </div>
        <div class="flex items-center gap-2">
          <select
            v-model="selectedSeasonId"
            class="rounded-lg border border-surface-3 bg-surface-0 px-2 py-1.5 text-sm text-text-primary"
            :disabled="seasonsLoading"
          >
            <option :value="null" disabled>{{ t("admin.dataAudit.selectSeason") }}</option>
            <option v-for="s in seasons" :key="s.season_id" :value="s.season_id">
              {{ s.season_name }} ({{ s.league_name }})
            </option>
          </select>
          <button
            class="rounded-lg bg-primary px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-60 whitespace-nowrap"
            :disabled="loading || !selectedSeasonId"
            @click="runAudit"
          >
            {{ loading ? t("admin.dataAudit.running") : t("admin.dataAudit.runAudit") }}
          </button>
        </div>
      </div>
    </div>

    <!-- Pre-audit empty state -->
    <div
      v-if="!auditResult && !loading && !error"
      class="rounded-lg border border-surface-3/50 bg-surface-1 p-8 text-center"
    >
      <p class="text-sm text-text-muted">{{ t("admin.dataAudit.emptyState") }}</p>
    </div>

    <!-- Loading skeletons -->
    <template v-if="loading">
      <div class="h-20 rounded-lg bg-surface-1 border border-surface-3/50 animate-pulse" />
      <div v-for="i in 5" :key="i" class="h-16 rounded-lg bg-surface-1 border border-surface-3/50 animate-pulse" />
    </template>

    <!-- Error -->
    <div
      v-else-if="error"
      class="rounded-lg bg-danger-muted/10 border border-danger-muted/30 p-4 text-sm text-danger"
    >
      {{ error }}
    </div>

    <!-- Results -->
    <template v-else-if="auditResult">
      <!-- Summary bar -->
      <div class="rounded-lg border border-surface-3/50 bg-surface-1 p-4">
        <p class="text-sm font-semibold text-text-primary mb-3">{{ t("admin.dataAudit.overallHealth") }}</p>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div class="rounded-lg border border-surface-3/50 bg-surface-2/30 p-3 text-center">
            <div class="text-2xl font-bold tabular-nums text-emerald-400">{{ auditResult.summary.green }}</div>
            <div class="text-[11px] text-text-muted">{{ t("admin.dataAudit.green") }}</div>
          </div>
          <div class="rounded-lg border border-surface-3/50 bg-surface-2/30 p-3 text-center">
            <div class="text-2xl font-bold tabular-nums text-amber-400">{{ auditResult.summary.yellow }}</div>
            <div class="text-[11px] text-text-muted">{{ t("admin.dataAudit.yellow") }}</div>
          </div>
          <div class="rounded-lg border border-surface-3/50 bg-surface-2/30 p-3 text-center">
            <div class="text-2xl font-bold tabular-nums text-danger">{{ auditResult.summary.red }}</div>
            <div class="text-[11px] text-text-muted">{{ t("admin.dataAudit.red") }}</div>
          </div>
          <div class="rounded-lg border border-surface-3/50 bg-surface-2/30 p-3 text-center">
            <div class="text-2xl font-bold tabular-nums text-text-primary">{{ auditResult.summary.total }}</div>
            <div class="text-[11px] text-text-muted">{{ t("admin.dataAudit.total") }}</div>
          </div>
        </div>
        <p class="text-xs text-text-muted mt-3">
          {{ t("admin.dataAudit.generatedAt", { ts: new Date(auditResult.generated_at).toLocaleString() }) }}
        </p>
      </div>

      <!-- Module cards -->
      <div
        v-for="mod in moduleConfig"
        :key="mod.key"
        class="rounded-lg border border-surface-3/50 bg-surface-1 overflow-hidden"
      >
        <!-- Module header -->
        <div
          class="px-4 py-3 flex items-center justify-between cursor-pointer hover:bg-surface-2/30 transition-colors select-none"
          @click="toggleModule(mod.key)"
        >
          <span class="text-sm font-semibold text-text-primary">{{ t(mod.titleKey) }}</span>
          <div class="flex items-center gap-2">
            <span
              class="w-2.5 h-2.5 rounded-full"
              :class="dotClass(worstStatus(auditResult.modules[mod.key].checks))"
            />
            <svg
              class="w-4 h-4 text-text-muted transition-transform"
              :class="{ 'rotate-180': expanded[mod.key] }"
              fill="none" viewBox="0 0 24 24" stroke="currentColor"
            >
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
            </svg>
          </div>
        </div>

        <!-- Module body -->
        <div v-if="expanded[mod.key]" class="border-t border-surface-3/50">
          <!-- Check rows -->
          <div
            v-for="check in auditResult.modules[mod.key].checks"
            :key="check.key"
            class="flex items-center justify-between px-4 py-2 border-b border-surface-3/30 last:border-b-0"
          >
            <div class="flex-1 min-w-0">
              <span class="text-xs text-text-secondary">{{ check.label }}</span>
              <span v-if="check.detail" class="block text-[11px] text-text-muted truncate">{{ check.detail }}</span>
            </div>
            <div class="flex items-center gap-3 ml-4">
              <span class="text-xs font-mono tabular-nums text-text-primary whitespace-nowrap">{{ check.value }}</span>
              <span class="w-2 h-2 rounded-full flex-shrink-0" :class="dotClass(check.status)" />
            </div>
          </div>

          <!-- Module-specific extras -->

          <!-- Hardening: Event type distribution -->
          <div
            v-if="mod.key === 'hardening' && Object.keys(eventTypeDist).length > 0"
            class="px-4 py-2 border-t border-surface-3/30"
          >
            <p class="text-[11px] text-text-muted mb-1">Event type distribution:</p>
            <div class="flex flex-wrap gap-2">
              <span
                v-for="(count, type) in eventTypeDist"
                :key="type"
                class="px-2 py-0.5 text-[11px] rounded bg-surface-2/50 text-text-secondary font-mono"
              >
                {{ type }}: {{ count }}
              </span>
            </div>
          </div>

          <!-- Data Guard: Reasons breakdown -->
          <div
            v-if="mod.key === 'data_guard' && Object.keys(reasonsBreakdown).length > 0"
            class="px-4 py-2 border-t border-surface-3/30"
          >
            <p class="text-[11px] text-text-muted mb-1">Check reasons breakdown:</p>
            <div class="flex flex-wrap gap-2">
              <span
                v-for="(count, reason) in reasonsBreakdown"
                :key="reason"
                class="px-2 py-0.5 text-[11px] rounded bg-surface-2/50 text-text-secondary font-mono"
              >
                {{ reason }}: {{ count }}
              </span>
            </div>
          </div>

          <!-- Data Guard: Flagged matches table -->
          <div
            v-if="mod.key === 'data_guard' && flaggedMatches.length > 0"
            class="px-4 py-2 border-t border-surface-3/30 overflow-x-auto"
          >
            <p class="text-[11px] text-text-muted mb-2">{{ t("admin.dataAudit.flaggedMatches") }}</p>
            <table class="w-full text-xs">
              <thead>
                <tr class="bg-surface-2/60 border-b border-surface-3/50">
                  <th class="px-2 py-1 text-left text-text-secondary">{{ t("admin.dataAudit.matchId") }}</th>
                  <th class="px-2 py-1 text-left text-text-secondary">{{ t("admin.dataAudit.match") }}</th>
                  <th class="px-2 py-1 text-left text-text-secondary">Status</th>
                  <th class="px-2 py-1 text-left text-text-secondary">{{ t("admin.dataAudit.reasons") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="fm in flaggedMatches"
                  :key="fm.match_id"
                  class="border-b border-surface-3/30 last:border-b-0"
                >
                  <td class="px-2 py-1 font-mono text-text-muted">{{ fm.match_id }}</td>
                  <td class="px-2 py-1 text-text-primary">{{ fm.home }} - {{ fm.away }}</td>
                  <td class="px-2 py-1">
                    <span class="px-1.5 py-0.5 rounded-full text-[10px] bg-surface-2/50 text-text-secondary">
                      {{ fm.status }}
                    </span>
                  </td>
                  <td class="px-2 py-1 text-text-muted">{{ fm.reasons.join(", ") }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- Fix button -->
          <div
            v-if="mod.fixAction && worstStatus(auditResult.modules[mod.key].checks) !== 'green'"
            class="px-4 py-2 border-t border-surface-3/30 flex justify-end"
          >
            <button
              class="rounded-lg bg-warning/20 text-warning px-3 py-1 text-xs font-semibold hover:bg-warning/30 transition-colors disabled:opacity-60"
              :disabled="fixLoading[mod.fixAction]"
              @click="triggerFix(mod.fixAction)"
            >
              {{ fixLoading[mod.fixAction] ? t("admin.dataAudit.jobInProgress") : t(mod.fixLabel!) }}
            </button>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
