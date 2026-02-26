<script setup lang="ts">
import { onMounted, computed, ref } from "vue";
import { useRoute, useRouter, RouterLink } from "vue-router";
import { useI18n } from "vue-i18n";
import { useTeam, teamSlug } from "@/composables/useTeam";
import { useOddsTimeline, type OddsTimelineResponse } from "@/composables/useOddsTimeline";
import OddsTimelineChart from "@/components/OddsTimelineChart.vue";
import { sportLabel } from "@/types/sports";

const { t } = useI18n();

const route = useRoute();
const router = useRouter();
const { data: team, loading, error, fetch: fetchTeam } = useTeam();

function reload() {
  const slug = route.params.teamSlug as string;
  const sport = (route.query.sport as string) || undefined;
  fetchTeam(slug, sport);
}

onMounted(() => reload());

// Form dot colors
function formColor(result: string): string {
  if (result === "W") return "bg-emerald-500";
  if (result === "D") return "bg-amber-500";
  return "bg-red-500";
}

function formLabel(result: string): string {
  if (result === "W") return "Sieg";
  if (result === "D") return "Unentschieden";
  return "Niederlage";
}

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString("de-DE", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
  });
}

function formatDateTime(dateStr: string): string {
  return new Date(dateStr).toLocaleString("de-DE", {
    weekday: "short",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Result indicator for a match relative to this team
function matchResult(m: { home_team_id?: string; result: { home_score: number; away_score: number } }): string {
  if (!team.value) return "";
  const isHome = m.home_team_id === team.value.team_key;
  const hg = m.result.home_score;
  const ag = m.result.away_score;
  if (hg === ag) return "D";
  if ((hg > ag && isHome) || (ag > hg && !isHome)) return "W";
  return "L";
}

function opponent(m: { home_team?: string; away_team?: string; home_team_id?: string }): string {
  if (!team.value) return "";
  return m.home_team_id === team.value.team_key ? (m.away_team || "") : (m.home_team || "");
}

function isHome(m: { home_team_id?: string }): boolean {
  return m.home_team_id === team.value?.team_key;
}

function opponentSlug(m: { home_team?: string; away_team?: string; home_team_id?: string }): string {
  return teamSlug(opponent(m));
}

// Stats
const stats = computed(() => team.value?.season_stats);
const gdSign = computed(() => {
  if (!stats.value) return "";
  return stats.value.goal_difference > 0 ? "+" : "";
});

// Upcoming match odds timeline expansion
const expandedTimeline = ref<string | null>(null);
const timelineCache = ref<Map<string, OddsTimelineResponse>>(new Map());
const timelineLoading = ref<string | null>(null);

async function toggleTimeline(matchId: string) {
  if (expandedTimeline.value === matchId) {
    expandedTimeline.value = null;
    return;
  }
  expandedTimeline.value = matchId;

  if (!timelineCache.value.has(matchId)) {
    timelineLoading.value = matchId;
    const tl = useOddsTimeline();
    await tl.fetchForMatch(matchId);
    if (tl.data.value) {
      timelineCache.value.set(matchId, tl.data.value);
    }
    timelineLoading.value = null;
  }
}

function getOddsEntries(m: { home_team: string; away_team: string; odds: Record<string, unknown> }) {
  const h2h = ((m.odds as any)?.h2h || {}) as Record<string, number>;
  const entries = [{ key: "1", label: m.home_team, value: h2h["1"] }];
  if (h2h["X"] !== undefined) {
    entries.push({ key: "X", label: "X", value: h2h["X"] });
  }
  entries.push({ key: "2", label: m.away_team, value: h2h["2"] });
  return entries;
}
</script>

<template>
  <div class="max-w-2xl mx-auto px-4 py-6 space-y-4">
    <!-- Back button -->
    <button
      @click="router.back()"
      class="text-sm text-text-muted hover:text-text-primary transition-colors flex items-center gap-1"
    >
      <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7" />
      </svg>
      {{ $t('common.back') }}
    </button>

    <!-- Loading state -->
    <div v-if="loading" class="text-center py-12">
      <div class="animate-spin w-8 h-8 border-2 border-primary border-t-transparent rounded-full mx-auto" />
      <p class="text-text-muted text-sm mt-3">{{ $t('teams.loading') }}</p>
    </div>

    <!-- Error state -->
    <div v-else-if="error" class="text-center py-12">
      <p class="text-text-muted mb-3">{{ $t('common.loadError') }}</p>
      <button class="text-sm text-primary hover:underline" @click="reload">{{ $t('common.retry') }}</button>
    </div>

    <!-- Team data -->
    <template v-else-if="team">
      <!-- Header card -->
      <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
        <h1 class="text-xl font-bold text-text-primary">{{ team.display_name }}</h1>
        <div class="flex flex-wrap gap-1.5 mt-2">
          <span
            v-for="sk in team.sport_keys"
            :key="sk"
            class="text-xs px-2 py-0.5 rounded-full bg-surface-2 text-text-muted"
          >
            {{ sportLabel(sk) }}
          </span>
        </div>

        <!-- Form strip -->
        <div v-if="team.form.length" class="flex items-center gap-2 mt-3">
          <span class="text-xs text-text-muted">{{ $t('common.form') }}:</span>
          <div class="flex gap-1">
            <span
              v-for="(f, i) in team.form"
              :key="i"
              class="w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold text-white"
              :class="formColor(f)"
              :title="formLabel(f)"
            >
              {{ f }}
            </span>
          </div>
          <span v-if="stats" class="ml-auto text-sm font-bold text-text-primary tabular-nums">
            {{ stats.points }} Pkt
          </span>
        </div>
      </div>

      <!-- Season stats -->
      <div v-if="stats" class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
        <h2 class="text-sm font-semibold text-text-primary mb-3">
          {{ t('common.season', { label: stats.season_label }) }}
        </h2>

        <div class="grid grid-cols-4 gap-3 text-center">
          <div>
            <div class="text-lg font-bold text-text-primary tabular-nums">{{ stats.matches_played }}</div>
            <div class="text-[10px] text-text-muted uppercase tracking-wider">{{ $t('teams.played') }}</div>
          </div>
          <div>
            <div class="text-lg font-bold text-emerald-400 tabular-nums">{{ stats.wins }}</div>
            <div class="text-[10px] text-text-muted uppercase tracking-wider">{{ $t('teams.wins') }}</div>
          </div>
          <div>
            <div class="text-lg font-bold text-amber-400 tabular-nums">{{ stats.draws }}</div>
            <div class="text-[10px] text-text-muted uppercase tracking-wider">{{ $t('teams.draws') }}</div>
          </div>
          <div>
            <div class="text-lg font-bold text-red-400 tabular-nums">{{ stats.losses }}</div>
            <div class="text-[10px] text-text-muted uppercase tracking-wider">{{ $t('teams.losses') }}</div>
          </div>
        </div>

        <div class="mt-3 pt-3 border-t border-surface-3/30 grid grid-cols-2 gap-3 text-xs">
          <div>
            <span class="text-text-muted">{{ $t('teams.goals') }}</span>
            <span class="font-mono font-bold text-text-primary ml-1 tabular-nums">
              {{ stats.goals_scored }}:{{ stats.goals_conceded }}
            </span>
            <span class="font-mono text-text-muted ml-1 tabular-nums">({{ gdSign }}{{ stats.goal_difference }})</span>
          </div>
          <div class="text-right">
            <span class="text-text-muted">{{ $t('teams.points') }}</span>
            <span class="font-bold text-text-primary ml-1 tabular-nums">{{ stats.points }}</span>
          </div>
          <div>
            <span class="text-text-muted">{{ $t('teams.homeRecord') }}</span>
            <span class="font-mono text-text-secondary ml-1 tabular-nums">
              {{ stats.home_record.w }}/{{ stats.home_record.d }}/{{ stats.home_record.l }}
            </span>
          </div>
          <div class="text-right">
            <span class="text-text-muted">{{ $t('teams.awayRecord') }}</span>
            <span class="font-mono text-text-secondary ml-1 tabular-nums">
              {{ stats.away_record.w }}/{{ stats.away_record.d }}/{{ stats.away_record.l }}
            </span>
          </div>
        </div>
      </div>

      <!-- Recent results -->
      <div v-if="team.recent_results.length" class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
        <h2 class="text-sm font-semibold text-text-primary mb-3">{{ $t('teams.recentResults') }}</h2>
        <div class="space-y-1.5">
          <div
            v-for="(m, i) in team.recent_results"
            :key="i"
            class="flex items-center gap-2 text-xs py-1"
          >
            <span
              class="w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold text-white shrink-0"
              :class="formColor(matchResult(m))"
            >
              {{ matchResult(m) }}
            </span>
            <span class="text-text-muted w-16 shrink-0 tabular-nums">{{ formatDate(m.match_date) }}</span>
            <span class="text-text-muted/60 w-3 shrink-0 text-center">{{ isHome(m) ? 'H' : 'A' }}</span>
            <RouterLink
              :to="{ name: 'team-detail', params: { teamSlug: opponentSlug(m) } }"
              class="flex-1 truncate text-text-secondary hover:text-primary transition-colors"
            >
              {{ opponent(m) }}
            </RouterLink>
            <span class="font-mono font-bold text-text-primary tabular-nums shrink-0">
              {{ m.result.home_score }}:{{ m.result.away_score }}
            </span>
          </div>
        </div>
      </div>

      <!-- Upcoming matches -->
      <div v-if="team.upcoming_matches.length" class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
        <h2 class="text-sm font-semibold text-text-primary mb-3">{{ $t('match.upcomingMatches') }}</h2>
        <div class="space-y-3">
          <div
            v-for="um in team.upcoming_matches"
            :key="um.id"
            class="border-b border-surface-3/30 last:border-0 pb-2 last:pb-0"
          >
            <div class="flex items-center gap-2 text-xs">
              <span class="text-text-muted w-28 shrink-0">{{ formatDateTime(um.match_date) }}</span>
              <div class="flex-1 min-w-0">
                <span class="text-text-secondary truncate block">
                  {{ um.home_team }} – {{ um.away_team }}
                </span>
              </div>
            </div>
            <!-- Odds + timeline toggle -->
            <div class="flex items-center gap-2 mt-1">
              <div class="flex gap-1.5 text-xs">
                <span
                  v-for="e in getOddsEntries(um)"
                  :key="e.key"
                  class="px-2 py-0.5 rounded bg-surface-2 text-text-secondary font-mono tabular-nums"
                >
                  {{ e.value?.toFixed(2) || '-' }}
                </span>
              </div>
              <button
                @click="toggleTimeline(um.id)"
                class="ml-auto text-[10px] text-text-muted hover:text-text-secondary transition-colors flex items-center gap-0.5"
              >
                <svg
                  class="w-3 h-3 transition-transform duration-200"
                  :class="{ 'rotate-90': expandedTimeline === um.id }"
                  fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"
                >
                  <path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7" />
                </svg>
                Verlauf
              </button>
            </div>
            <!-- Expandable timeline -->
            <Transition name="expand">
              <div v-if="expandedTimeline === um.id" class="overflow-hidden mt-2">
                <div v-if="timelineLoading === um.id" class="text-[10px] text-text-muted text-center py-3">
                  Lade...
                </div>
                <OddsTimelineChart
                  v-else-if="timelineCache.get(um.id)?.items"
                  :snapshots="timelineCache.get(um.id)!.items"
                  :home-team="um.home_team"
                  :away-team="um.away_team"
                  compact
                />
              </div>
            </Transition>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.expand-enter-active,
.expand-leave-active {
  transition: all 0.2s ease;
  max-height: 200px;
}
.expand-enter-from,
.expand-leave-to {
  max-height: 0;
  opacity: 0;
}
</style>
