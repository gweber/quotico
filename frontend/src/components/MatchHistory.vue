<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { useApi } from "@/composables/useApi";
import { useMatchHistory, type HistoricalMatch, type MatchContext } from "@/composables/useMatchHistory";
import { isBasketball, scoreUnitLabel } from "@/types/sports";

const props = defineProps<{
  homeTeam: string;
  awayTeam: string;
  sportKey: string;
  context?: MatchContext | null;  // Pre-loaded data from parent (Matchday path)
}>();

const isBball = computed(() => isBasketball(props.sportKey));
const unit = computed(() => scoreUnitLabel(props.sportKey));

const api = useApi();
const expanded = ref(false);
const h2hScrollRef = ref<HTMLElement | null>(null);
const h2hScrolledToEnd = ref(false);
const h2hLoadingMore = ref(false);
const h2hAllLoaded = ref(false);
const { data, loading, error, fetch: fetchHistory } = useMatchHistory();

async function onH2HScroll() {
  const el = h2hScrollRef.value;
  if (!el) return;
  const nearBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 8;
  h2hScrolledToEnd.value = nearBottom;
  if (nearBottom && !h2hLoadingMore.value && !h2hAllLoaded.value) {
    await loadMoreH2H();
  }
}

async function loadMoreH2H() {
  if (!data.value?.h2h?.matches.length || !data.value.home_team_key) return;
  h2hLoadingMore.value = true;
  try {
    const resp = await api.get<{ matches: HistoricalMatch[]; count: number }>(
      "/historical/h2h",
      {
        sport_key: props.sportKey,
        home_team_key: data.value.home_team_key,
        away_team_key: data.value.away_team_key,
        skip: String(data.value.h2h.matches.length),
        limit: "10",
      },
    );
    if (resp.matches.length === 0) {
      h2hAllLoaded.value = true;
    } else {
      data.value.h2h.matches.push(...resp.matches);
    }
  } catch {
    // Silently fail — user still sees what's loaded
  } finally {
    h2hLoadingMore.value = false;
  }
}

// If context is embedded from the API response, use it directly.
// Otherwise fall back to fetching (Dashboard path via bulk prefetch).
onMounted(() => {
  if (props.context) {
    data.value = props.context;
  } else {
    fetchHistory(props.homeTeam, props.awayTeam, props.sportKey);
  }
});

function formResult(match: HistoricalMatch, teamKey: string): "W" | "D" | "L" {
  if (match.result.outcome === "X") return "D";
  const isHome = match.home_team_key === teamKey;
  if (match.result.outcome === "1") return isHome ? "W" : "L";
  return isHome ? "L" : "W";
}

function resultColor(r: "W" | "D" | "L") {
  if (r === "W") return "bg-primary";
  if (r === "D") return "bg-warning";
  return "bg-danger";
}

function h2hWinnerClass(match: HistoricalMatch, side: "home" | "away"): string {
  const homeWon = match.result.home_score > match.result.away_score;
  const awayWon = match.result.away_score > match.result.home_score;
  if (side === "home" && homeWon) return "text-primary font-semibold";
  if (side === "away" && awayWon) return "text-primary font-semibold";
  return "text-text-secondary";
}

function formScore(matches: HistoricalMatch[], teamKey: string): number {
  return matches.reduce((sum, m) => {
    const r = formResult(m, teamKey);
    return sum + (r === "W" ? 3 : r === "D" ? 1 : 0);
  }, 0);
}

function formScoreColor(score: number): string {
  if (score >= 22) return "text-primary";
  if (score >= 12) return "text-warning";
  return "text-danger";
}

function formatDate(dateStr: string) {
  return new Date(dateStr).toLocaleDateString("de-DE", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
  });
}

/** Returns gap in full years between two consecutive H2H matches, or 0 if < 1 year. */
function yearGap(olderDate: string, newerDate: string): number {
  const diff = new Date(newerDate).getTime() - new Date(olderDate).getTime();
  const years = diff / (365.25 * 24 * 60 * 60 * 1000);
  return years >= 1 ? Math.floor(years) : 0;
}
</script>

<template>
  <div class="mt-2 border-t border-surface-3/30 pt-2">
    <!-- Toggle bar -->
    <button
      class="w-full flex items-center gap-2 text-xs text-text-muted hover:text-text-secondary transition-colors py-1 group"
      @click="expanded = !expanded"
      :aria-expanded="expanded"
      aria-label="Historische Daten anzeigen"
    >
      <svg
        class="w-3.5 h-3.5 shrink-0 transition-transform duration-200"
        :class="{ 'rotate-90': expanded }"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        stroke-width="2"
      >
        <path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7" />
      </svg>

      <template v-if="data?.h2h?.summary">
        <span>
          H2H:
          <span class="text-text-secondary font-medium">{{ homeTeam }}</span>
          {{ data.h2h.summary.home_wins }} – {{ data.h2h.summary.draws }} – {{ data.h2h.summary.away_wins }}
          <span class="text-text-secondary font-medium">{{ awayTeam }}</span>
        </span>
        <span class="ml-auto text-text-muted/60">
          {{ data.h2h.summary.total }} Spiele
        </span>
      </template>
      <template v-else>
        <span>Statistik &amp; H2H</span>
      </template>
    </button>

    <!-- Expanded content -->
    <Transition name="expand">
      <div v-if="expanded" class="overflow-hidden">
        <!-- Loading -->
        <div v-if="loading" class="space-y-2 py-3">
          <div class="h-3 bg-surface-2 rounded animate-pulse w-3/4" />
          <div class="h-3 bg-surface-2 rounded animate-pulse w-1/2" />
          <div class="h-3 bg-surface-2 rounded animate-pulse w-2/3" />
        </div>

        <!-- Error -->
        <p v-else-if="error" class="text-xs text-text-muted py-2">
          Daten konnten nicht geladen werden.
        </p>

        <!-- No data -->
        <p v-else-if="!data?.h2h && !data?.home_form?.length" class="text-xs text-text-muted py-2">
          Keine historischen Daten verfügbar.
        </p>

        <!-- Data -->
        <div v-else class="space-y-4 py-3">
          <!-- H2H Summary -->
          <div v-if="data?.h2h?.summary" class="space-y-2">
            <h4 class="text-xs font-medium text-text-muted">
              Direkter Vergleich
            </h4>

            <!-- Win bar -->
            <div class="flex h-2 rounded-full overflow-hidden bg-surface-2">
              <div
                class="bg-primary transition-all"
                :style="{ width: `${(data.h2h.summary.home_wins / data.h2h.summary.total) * 100}%` }"
              />
              <div
                class="bg-warning transition-all"
                :style="{ width: `${(data.h2h.summary.draws / data.h2h.summary.total) * 100}%` }"
              />
              <div
                class="bg-danger transition-all"
                :style="{ width: `${(data.h2h.summary.away_wins / data.h2h.summary.total) * 100}%` }"
              />
            </div>

            <!-- Stats row -->
            <div class="flex justify-between text-xs text-text-muted">
              <span>⌀ {{ data.h2h.summary.avg_goals }} {{ unit }}</span>
              <template v-if="!isBball">
                <span>Über 2.5: {{ Math.round(data.h2h.summary.over_2_5_pct * 100) }}%</span>
                <span>Beide treffen: {{ Math.round(data.h2h.summary.btts_pct * 100) }}%</span>
              </template>
              <span
                v-if="data.h2h.summary.avg_home_xg != null"
                class="tabular-nums"
                title="Durchschnittliche Expected Goals pro Spiel"
              >⌀ xG {{ data.h2h.summary.avg_home_xg }} – {{ data.h2h.summary.avg_away_xg }}</span>
            </div>

            <!-- Recent meetings -->
            <div v-if="data.h2h.matches.length" class="relative">
              <div
                ref="h2hScrollRef"
                class="space-y-1 overflow-y-auto overscroll-contain scrollbar-thin"
                :class="data.h2h.matches.length > 5 ? 'max-h-[7.5rem]' : ''"
                @scroll="onH2HScroll"
              >
                <template
                  v-for="(m, i) in data.h2h.matches"
                  :key="i"
                >
                  <!-- Year gap divider between non-consecutive meetings -->
                  <div
                    v-if="i > 0 && yearGap(m.match_date, data.h2h.matches[i - 1].match_date) >= 1"
                    class="flex items-center gap-2 py-1"
                  >
                    <div class="flex-1 border-t border-dashed border-surface-3/60" />
                    <span class="text-[10px] text-text-muted/50 whitespace-nowrap">
                      {{ yearGap(m.match_date, data.h2h.matches[i - 1].match_date) === 1
                        ? '1 Jahr Pause'
                        : `${yearGap(m.match_date, data.h2h.matches[i - 1].match_date)} Jahre Pause`
                      }}
                    </span>
                    <div class="flex-1 border-t border-dashed border-surface-3/60" />
                  </div>

                  <div class="grid grid-cols-[3.5rem_1fr_3.5rem_1fr_3.5rem] items-center gap-x-1 text-xs py-0.5">
                    <span class="text-text-muted/60 tabular-nums text-left">
                      {{ formatDate(m.match_date) }}
                    </span>
                    <span class="text-right truncate" :class="h2hWinnerClass(m, 'home')">
                      {{ m.home_team }}
                    </span>
                    <span class="font-mono text-center tabular-nums">
                      <span class="font-bold text-text-primary">{{ m.result.home_score }}:{{ m.result.away_score }}</span>
                      <span
                        v-if="m.result.home_xg != null"
                        class="block text-[9px] font-normal text-text-muted/50 leading-tight"
                        :title="`xG: ${m.result.home_xg} – ${m.result.away_xg}`"
                      >{{ m.result.home_xg.toFixed(1) }}–{{ m.result.away_xg!.toFixed(1) }}</span>
                    </span>
                    <span class="truncate" :class="h2hWinnerClass(m, 'away')">
                      {{ m.away_team }}
                    </span>
                    <span />
                  </div>
                </template>
                <!-- Loading more spinner -->
                <div v-if="h2hLoadingMore" class="flex justify-center py-1">
                  <div class="w-4 h-4 border-2 border-surface-3 border-t-primary rounded-full animate-spin" />
                </div>
              </div>
              <!-- Bottom fade hint -->
              <div
                v-if="data.h2h.matches.length > 5 && !h2hScrolledToEnd && !h2hAllLoaded"
                class="absolute bottom-0 inset-x-0 h-6 bg-gradient-to-t from-surface-1 to-transparent pointer-events-none"
              />
            </div>
          </div>

          <!-- Form guides -->
          <div
            v-if="data?.home_form?.length || data?.away_form?.length"
            class="space-y-2"
          >
            <h4 class="text-xs font-medium text-text-muted">
              Form
            </h4>

            <!-- Home team form -->
            <div v-if="data?.home_form?.length" class="flex items-center gap-2">
              <span class="text-xs text-text-secondary w-28 truncate shrink-0">
                {{ homeTeam }}
              </span>
              <div class="flex gap-1 flex-wrap">
                <span
                  v-for="(m, i) in data.home_form"
                  :key="i"
                  class="w-5 h-5 rounded-full text-[10px] font-bold text-white flex items-center justify-center"
                  :class="resultColor(formResult(m, data.home_team_key))"
                  :title="`${m.home_team} ${m.result.home_score}:${m.result.away_score} ${m.away_team}`"
                >
                  {{ formResult(m, data.home_team_key) }}
                </span>
              </div>
              <span
                class="text-[10px] font-bold ml-auto tabular-nums"
                :class="formScoreColor(formScore(data.home_form, data.home_team_key))"
                :title="`Letzte ${data.home_form.length} Spiele: ${formScore(data.home_form, data.home_team_key)} von ${data.home_form.length * 3} Punkten (S=3, U=1, N=0)`"
              >
                {{ formScore(data.home_form, data.home_team_key) }}<span class="text-text-muted">/{{ data.home_form.length * 3 }}</span>
              </span>
            </div>

            <!-- Away team form -->
            <div v-if="data?.away_form?.length" class="flex items-center gap-2">
              <span class="text-xs text-text-secondary w-28 truncate shrink-0">
                {{ awayTeam }}
              </span>
              <div class="flex gap-1 flex-wrap">
                <span
                  v-for="(m, i) in data.away_form"
                  :key="i"
                  class="w-5 h-5 rounded-full text-[10px] font-bold text-white flex items-center justify-center"
                  :class="resultColor(formResult(m, data.away_team_key))"
                  :title="`${m.home_team} ${m.result.home_score}:${m.result.away_score} ${m.away_team}`"
                >
                  {{ formResult(m, data.away_team_key) }}
                </span>
              </div>
              <span
                class="text-[10px] font-bold ml-auto tabular-nums"
                :class="formScoreColor(formScore(data.away_form, data.away_team_key))"
                :title="`Letzte ${data.away_form.length} Spiele: ${formScore(data.away_form, data.away_team_key)} von ${data.away_form.length * 3} Punkten (S=3, U=1, N=0)`"
              >
                {{ formScore(data.away_form, data.away_team_key) }}<span class="text-text-muted">/{{ data.away_form.length * 3 }}</span>
              </span>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.expand-enter-active,
.expand-leave-active {
  transition: all 0.2s ease;
  max-height: 400px;
}
.expand-enter-from,
.expand-leave-to {
  max-height: 0;
  opacity: 0;
}

.scrollbar-thin {
  scrollbar-width: thin;
  scrollbar-color: #334155 transparent;
}
.scrollbar-thin::-webkit-scrollbar {
  width: 4px;
}
.scrollbar-thin::-webkit-scrollbar-thumb {
  background: #334155;
  border-radius: 2px;
}
</style>
