<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

const api = useApi();
const toast = useToast();

interface AdminMatch {
  id: string;
  sport_key: string;
  home_team: string;
  away_team: string;
  match_date: string;
  status: string;
  odds: { h2h: Record<string, number>; totals?: Record<string, unknown>; spreads?: Record<string, unknown>; updated_at?: string | null };
  result: { home_score: number | null; away_score: number | null; outcome: string | null };
  bet_count: number;
}

const matches = ref<AdminMatch[]>([]);
const loading = ref(true);
const error = ref(false);
const statusFilter = ref("");

// Override modal
const overrideMatch = ref<AdminMatch | null>(null);
const overrideResult = ref("1");
const overrideHome = ref(0);
const overrideAway = ref(0);

async function fetchMatches() {
  loading.value = true;
  error.value = false;
  try {
    const params: Record<string, string> = {};
    if (statusFilter.value) params.status = statusFilter.value;
    matches.value = await api.get<AdminMatch[]>("/admin/matches", params);
  } catch {
    error.value = true;
  } finally {
    loading.value = false;
  }
}

function openOverride(match: AdminMatch) {
  overrideMatch.value = match;
  overrideResult.value = match.result.outcome || "1";
  overrideHome.value = match.result.home_score ?? 0;
  overrideAway.value = match.result.away_score ?? 0;
}

async function submitOverride() {
  if (!overrideMatch.value) return;
  try {
    await api.post(`/admin/matches/${overrideMatch.value.id}/override`, {
      result: overrideResult.value,
      home_score: overrideHome.value,
      away_score: overrideAway.value,
    });
    toast.success("Ergebnis überschrieben.");
    overrideMatch.value = null;
    await fetchMatches();
  } catch (e: any) {
    toast.error(e.message);
  }
}

const formatDate = (iso: string) =>
  new Date(iso).toLocaleDateString("de-DE", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });

import { sportLabel } from "@/types/sports";

onMounted(fetchMatches);
</script>

<template>
  <div class="max-w-5xl mx-auto p-4">
    <div class="mb-6">
      <RouterLink to="/admin" class="text-xs text-text-muted hover:text-text-primary">&larr; Dashboard</RouterLink>
      <h1 class="text-xl font-bold text-text-primary">Match Management</h1>
    </div>

    <!-- Filters -->
    <div class="flex gap-2 mb-4">
      <button
        v-for="s in ['', 'scheduled', 'live', 'final']"
        :key="s"
        class="px-3 py-1.5 text-xs rounded-lg transition-colors"
        :class="statusFilter === s ? 'bg-primary text-surface-0' : 'bg-surface-2 text-text-secondary hover:bg-surface-3'"
        @click="statusFilter = s; fetchMatches()"
      >
        {{ s === '' ? 'Alle' : s === 'scheduled' ? 'Geplant' : s === 'live' ? 'Live' : 'Beendet' }}
      </button>
    </div>

    <!-- Error -->
    <div v-if="error" class="text-center py-12">
      <p class="text-text-muted mb-3">Error loading.</p>
      <button class="text-sm text-primary hover:underline" @click="fetchMatches">Try again</button>
    </div>

    <!-- Table -->
    <div v-else class="bg-surface-1 rounded-card border border-surface-3/50 overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="text-xs text-text-muted border-b border-surface-3">
            <th class="text-left px-4 py-3 font-medium">Spiel</th>
            <th class="text-left px-4 py-3 font-medium">Liga</th>
            <th class="text-left px-4 py-3 font-medium">Datum</th>
            <th class="text-center px-4 py-3 font-medium">Status</th>
            <th class="text-center px-4 py-3 font-medium">Ergebnis</th>
            <th class="text-right px-4 py-3 font-medium">Tipps</th>
            <th class="text-right px-4 py-3 font-medium">Aktionen</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="7" class="px-4 py-8 text-center text-text-muted">Laden...</td>
          </tr>
          <tr
            v-for="m in matches"
            :key="m.id"
            class="border-b border-surface-3/20 last:border-0"
          >
            <td class="px-4 py-3">
              <span class="text-text-primary">{{ m.home_team }}</span>
              <span class="text-text-muted"> vs </span>
              <span class="text-text-primary">{{ m.away_team }}</span>
            </td>
            <td class="px-4 py-3 text-text-muted text-xs">{{ sportLabel(m.sport_key) }}</td>
            <td class="px-4 py-3 text-text-muted text-xs">{{ formatDate(m.match_date) }}</td>
            <td class="px-4 py-3 text-center">
              <span
                class="text-xs px-2 py-0.5 rounded-full font-medium"
                :class="{
                  'bg-primary-muted/20 text-primary': m.status === 'scheduled',
                  'bg-danger-muted/20 text-danger': m.status === 'live',
                  'bg-surface-3 text-text-muted': m.status === 'final',
                }"
              >{{ m.status }}</span>
            </td>
            <td class="px-4 py-3 text-center font-mono tabular-nums text-text-primary">
              <template v-if="m.result.home_score != null">{{ m.result.home_score }}-{{ m.result.away_score }}</template>
              <span v-else class="text-text-muted">-</span>
            </td>
            <td class="px-4 py-3 text-right tabular-nums text-text-muted">{{ m.bet_count }}</td>
            <td class="px-4 py-3 text-right">
              <button
                class="text-xs px-2 py-1 rounded bg-surface-2 hover:bg-surface-3 text-text-secondary transition-colors"
                @click="openOverride(m)"
              >
                {{ m.status === 'final' ? 'Override' : 'Force Settle' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Override Modal -->
    <Teleport to="body">
      <Transition name="fade">
        <div
          v-if="overrideMatch"
          class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60"
          @click.self="overrideMatch = null"
        >
          <div class="bg-surface-1 rounded-card p-6 w-full max-w-sm border border-surface-3">
            <h2 class="text-lg font-semibold text-text-primary mb-1">Ergebnis setzen</h2>
            <p class="text-xs text-text-muted mb-4">
              {{ overrideMatch.home_team }} vs {{ overrideMatch.away_team }}
            </p>
            <form @submit.prevent="submitOverride" class="space-y-4">
              <div>
                <label class="block text-sm text-text-secondary mb-1">Ergebnis</label>
                <select
                  v-model="overrideResult"
                  class="w-full px-3 py-2 rounded-lg bg-surface-2 border border-surface-3 text-text-primary text-sm focus:ring-2 focus:ring-primary focus:outline-none"
                >
                  <option value="1">1 &mdash; {{ overrideMatch.home_team }} gewinnt</option>
                  <option v-if="overrideMatch.odds.h2h['X'] !== undefined" value="X">X &mdash; Unentschieden</option>
                  <option value="2">2 &mdash; {{ overrideMatch.away_team }} gewinnt</option>
                </select>
              </div>
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="block text-sm text-text-secondary mb-1">{{ overrideMatch.home_team }}</label>
                  <input
                    v-model.number="overrideHome"
                    type="number"
                    min="0"
                    class="w-full px-3 py-2 rounded-lg bg-surface-2 border border-surface-3 text-text-primary text-sm focus:ring-2 focus:ring-primary focus:outline-none"
                  />
                </div>
                <div>
                  <label class="block text-sm text-text-secondary mb-1">{{ overrideMatch.away_team }}</label>
                  <input
                    v-model.number="overrideAway"
                    type="number"
                    min="0"
                    class="w-full px-3 py-2 rounded-lg bg-surface-2 border border-surface-3 text-text-primary text-sm focus:ring-2 focus:ring-primary focus:outline-none"
                  />
                </div>
              </div>
              <div class="flex gap-2 justify-end">
                <button
                  type="button"
                  class="px-4 py-2 text-sm rounded-lg text-text-secondary hover:bg-surface-2"
                  @click="overrideMatch = null"
                >Abbrechen</button>
                <button
                  type="submit"
                  class="px-4 py-2 text-sm rounded-lg bg-danger text-white hover:bg-danger/80"
                >Ergebnis setzen</button>
              </div>
            </form>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.fade-enter-active { transition: opacity 0.15s ease-out; }
.fade-leave-active { transition: opacity 0.1s ease-in; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
