<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";
import { sportLabel } from "@/types/sports";
import DecisionJourney from "@/components/admin/DecisionJourney.vue";

const route = useRoute();
const { t } = useI18n();
const api = useApi();

const loading = ref(false);
const error = ref(false);
const tip = ref<any | null>(null);

const matchId = computed(() => String(route.params.matchId || ""));

async function loadTrace() {
  if (!matchId.value) {
    error.value = true;
    return;
  }

  loading.value = true;
  error.value = false;
  try {
    tip.value = await api.get(`/quotico-tips/${encodeURIComponent(matchId.value)}`);
  } catch {
    tip.value = null;
    error.value = true;
  } finally {
    loading.value = false;
  }
}

onMounted(loadTrace);
</script>

<template>
  <div class="max-w-4xl mx-auto p-4 space-y-4">
    <div class="flex items-center justify-between gap-2">
      <h1 class="text-lg font-semibold text-text-primary">
        {{ t("qtipPerformance.adminTraceTitle") }}
      </h1>
      <router-link
        :to="{ name: 'admin' }"
        class="text-xs text-text-muted hover:text-text-secondary"
      >
        {{ t("common.back") }}
      </router-link>
    </div>

    <div v-if="loading" class="bg-surface-1 rounded-card p-4 text-sm text-text-muted">
      {{ t("qtipPerformance.loadingDetail") }}
    </div>

    <div v-else-if="error || !tip" class="bg-surface-1 rounded-card p-4 text-sm text-danger">
      {{ t("qtipPerformance.loadError") }}
    </div>

    <div v-else class="space-y-4">
      <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
        <div class="text-xs text-text-muted">{{ sportLabel(tip.league_id) }}</div>
        <div class="text-sm text-text-primary font-medium mt-1">
          {{ tip.home_team }} vs {{ tip.away_team }}
        </div>
        <div class="mt-3 grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
          <div class="bg-surface-2 rounded p-2">
            <div class="text-text-muted">{{ t("qtipPerformance.pick") }}</div>
            <div class="font-mono text-text-primary">{{ tip.recommended_selection }}</div>
          </div>
          <div class="bg-surface-2 rounded p-2">
            <div class="text-text-muted">{{ t("qtipPerformance.actual") }}</div>
            <div class="font-mono text-text-primary">{{ tip.actual_result || "--" }}</div>
          </div>
          <div class="bg-surface-2 rounded p-2">
            <div class="text-text-muted">{{ t("qtipPerformance.confidence") }}</div>
            <div class="font-mono text-text-primary">{{ ((tip.confidence ?? 0) * 100).toFixed(1) }}%</div>
          </div>
          <div class="bg-surface-2 rounded p-2">
            <div class="text-text-muted">{{ t("qtipPerformance.edge") }}</div>
            <div class="font-mono text-text-primary">{{ (tip.edge_pct ?? 0).toFixed(1) }}%</div>
          </div>
        </div>
      </div>

      <DecisionJourney :trace="tip.decision_trace" />
    </div>
  </div>
</template>
