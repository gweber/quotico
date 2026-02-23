<script setup lang="ts">
import { ref, watch, onMounted, computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useSpieltagStore } from "@/stores/spieltag";
import { useAuthStore } from "@/stores/auth";
import { useSquadsStore, type Squad } from "@/stores/squads";
import { useWalletStore } from "@/stores/wallet";
import { useSurvivorStore } from "@/stores/survivor";
import { useFantasyStore } from "@/stores/fantasy";
import { useToast } from "@/composables/useToast";
import { refreshQuoticoTips } from "@/composables/useQuoticoTip";
import SpieltagMatchCard from "@/components/SpieltagMatchCard.vue";
import SeasonTrack from "@/components/SeasonTrack.vue";
import AutoTippSelector from "@/components/AutoTippSelector.vue";
import SpieltagLeaderboard from "@/components/SpieltagLeaderboard.vue";
import WalletBar from "@/components/WalletBar.vue";
import WalletDisclaimer from "@/components/WalletDisclaimer.vue";
import BankrollBetCard from "@/components/BankrollBetCard.vue";
import BankrollLeaderboard from "@/components/BankrollLeaderboard.vue";
import SurvivorPickCard from "@/components/SurvivorPickCard.vue";
import SurvivorStandings from "@/components/SurvivorStandings.vue";
import OverUnderCard from "@/components/OverUnderCard.vue";
import OverUnderLeaderboard from "@/components/OverUnderLeaderboard.vue";
import FantasyPickCard from "@/components/FantasyPickCard.vue";
import FantasyStandings from "@/components/FantasyStandings.vue";
import ParlayBuilder from "@/components/ParlayBuilder.vue";
import QuotentippCard from "@/components/QuotentippCard.vue";
import AdminPredictionPanel from "@/components/AdminPredictionPanel.vue";

const route = useRoute();
const router = useRouter();
const spieltag = useSpieltagStore();
const auth = useAuthStore();
const squadsStore = useSquadsStore();
const walletStore = useWalletStore();
const survivorStore = useSurvivorStore();
const fantasyStore = useFantasyStore();
const toast = useToast();

import { SPORT_LABELS } from "@/types/sports";
import { GAME_MODE_LABELS } from "@/types/league";

const sportLabels = SPORT_LABELS;

// Sport selector
const selectedSport = ref(
  (route.params.sport as string) || "soccer_germany_bundesliga"
);

// Squad selector
const selectedSquad = ref<Squad | null>(null);
const showDisclaimer = ref(false);

const activeGameMode = computed(() => {
  if (!selectedSquad.value) return "classic";
  return squadsStore.getGameModeForSport(
    selectedSquad.value.id,
    selectedSport.value
  );
});

const autoTippBlocked = computed(() => {
  if (!selectedSquad.value) return false;
  return !!selectedSquad.value.auto_tipp_blocked;
});

// Only show squads that have at least one active league config
const spieltagSquads = computed(() =>
  squadsStore.squads.filter((s) =>
    s.league_configs?.some((lc) => !lc.deactivated_at)
  )
);

// Filter sports to only those configured in the selected squad
const availableSports = computed(() => {
  if (!selectedSquad.value?.league_configs?.length) return spieltag.sports;
  const configured = new Set(
    selectedSquad.value.league_configs
      .filter((lc) => !lc.deactivated_at)
      .map((lc) => lc.sport_key)
  );
  if (configured.size === 0) return spieltag.sports;
  return spieltag.sports.filter((s) => configured.has(s.sport_key));
});

// Determine which matchday is selected
const selectedMatchdayId = ref<string | null>(null);

const currentMatchdayLabel = computed(
  () => spieltag.currentMatchday?.label || "Spieltag"
);

const refreshingTips = ref(false);

async function handleRefreshTips() {
  refreshingTips.value = true;
  try {
    await refreshQuoticoTips(selectedSport.value);
  } finally {
    refreshingTips.value = false;
  }
}

const hasUnsavedChanges = computed(() => {
  if (activeGameMode.value !== "classic") return false;
  if (!spieltag.predictions) return spieltag.draftPredictions.size > 0;
  const saved = new Map(
    spieltag.predictions.predictions.map((p) => [
      p.match_id,
      { home: p.home_score, away: p.away_score },
    ])
  );
  for (const [matchId, draft] of spieltag.draftPredictions) {
    const s = saved.get(matchId);
    if (!s || s.home !== draft.home || s.away !== draft.away) return true;
  }
  return (
    spieltag.draftAutoStrategy !==
    (spieltag.predictions.auto_tipp_strategy || "none")
  );
});

// Whether parlay is available (tippspiel + bankroll)
const parlayAvailable = computed(() =>
  ["classic", "bankroll"].includes(activeGameMode.value)
);

const totalMatchdays = computed(() => {
  const sport = spieltag.sports.find((s) => s.sport_key === selectedSport.value);
  return sport?.matchdays_per_season ?? spieltag.matchdays.length;
});

async function loadSport(sport: string, fromRoute = false) {
  selectedSport.value = sport;
  spieltag.setSport(sport);
  await spieltag.fetchMatchdays(sport);

  if (fromRoute && route.params.matchday && spieltag.matchdays.length > 0) {
    const mdNum = parseInt(route.params.matchday as string);
    const md = spieltag.matchdays.find((m) => m.matchday_number === mdNum);
    if (md) {
      await selectMatchday(md.id);
      return;
    }
  }

  // Prefer the next fully tippable matchday (upcoming), then fall back
  // to in_progress (partially tippable), then the last completed one.
  const current =
    spieltag.matchdays.find((md) => md.status === "upcoming") ||
    spieltag.matchdays.find((md) => md.status === "in_progress") ||
    spieltag.matchdays[spieltag.matchdays.length - 1];
  if (current) {
    await selectMatchday(current.id);
  }
}

function handleDragPreview(matchdayId: string | null) {
  if (matchdayId) spieltag.previewCached(matchdayId);
}

async function selectMatchday(id: string) {
  selectedMatchdayId.value = id;
  await spieltag.fetchMatchdayDetail(id, selectedSquad.value?.id);

  if (auth.isLoggedIn) {
    await spieltag.fetchPredictions(id);
    await loadGameModeData(id);
  }
  const md = spieltag.currentMatchday;
  if (md) {
    router.replace({
      params: {
        sport: selectedSport.value,
        matchday: String(md.matchday_number),
      },
    });
  }
}

async function loadGameModeData(matchdayId: string) {
  if (!selectedSquad.value) return;
  const squad = selectedSquad.value;
  const mode = activeGameMode.value;

  if (mode === "bankroll" || mode === "over_under") {
    await walletStore.fetchWallet(squad.id, selectedSport.value);
    if (mode === "bankroll") {
      await walletStore.fetchBets(squad.id, matchdayId);
    } else {
      await walletStore.fetchOverUnderBets(squad.id, matchdayId);
    }
    await walletStore.fetchParlay(squad.id, matchdayId);
  } else if (mode === "survivor") {
    await survivorStore.fetchStatus(squad.id, selectedSport.value);
  } else if (mode === "fantasy") {
    const md = spieltag.currentMatchday;
    if (md) {
      await fantasyStore.fetchPick(
        squad.id,
        selectedSport.value,
        md.season ?? new Date().getFullYear(),
        md.matchday_number
      );
    }
  } else if (mode === "moneyline") {
    const matchIds = spieltag.matches.map((m) => m.id);
    await spieltag.fetchMoneylineTips(matchIds);
  }

  // Classic + bankroll can also have parlays
  if (parlayAvailable.value) {
    await walletStore.fetchParlay(squad.id, matchdayId);
  }
}

async function selectSquad(squad: Squad | null) {
  selectedSquad.value = squad;
  spieltag.setSquadContext(squad?.id ?? null);
  walletStore.reset();
  survivorStore.reset();
  fantasyStore.reset();

  // Reset auto-tipp strategy if blocked in new squad
  if (squad?.auto_tipp_blocked) {
    spieltag.draftAutoStrategy = "none";
  }

  if (selectedMatchdayId.value) {
    // Re-fetch matchday detail with new squad's lock_minutes
    await spieltag.fetchMatchdayDetail(selectedMatchdayId.value, squad?.id);
  }

  if (squad && selectedMatchdayId.value && auth.isLoggedIn) {
    // Re-fetch predictions for new squad context
    await spieltag.fetchPredictions(selectedMatchdayId.value);

    // Check disclaimer for wallet-based modes
    const mode = activeGameMode.value;
    if (["bankroll", "over_under"].includes(mode)) {
      const user = auth.user;
      if (user && !(user as unknown as Record<string, unknown>).wallet_disclaimer_accepted_at) {
        showDisclaimer.value = true;
        return;
      }
    }
    await loadGameModeData(selectedMatchdayId.value);
  }
}

async function handleSave() {
  if (!selectedMatchdayId.value) return;
  const ok = await spieltag.savePredictions(selectedMatchdayId.value);
  if (ok) {
    toast.success("Tipps gespeichert!");
  } else {
    toast.error("Fehler beim Speichern.");
  }
}

function onDisclaimerAccepted() {
  showDisclaimer.value = false;
  if (selectedMatchdayId.value) {
    loadGameModeData(selectedMatchdayId.value);
  }
}

// Init
onMounted(async () => {
  const sportsPromise = spieltag.fetchSports();
  if (auth.isLoggedIn) {
    await squadsStore.fetchMySquads();
  }
  await sportsPromise;
  await loadSport(selectedSport.value, true);
});

// React to sport changes
watch(selectedSport, (sport) => {
  loadSport(sport);
});
</script>

<template>
  <div class="max-w-3xl mx-auto px-4 py-6 space-y-4">
    <!-- Header -->
    <div class="flex items-center justify-between">
      <div>
        <p class="text-sm text-text-secondary">
          <template v-if="activeGameMode === 'classic'">
            Tippe die exakten Ergebnisse aller Spiele eines Spieltags.
          </template>
          <template v-else-if="activeGameMode === 'moneyline'">
            Tippe 1X2 auf die Spielausgänge. Quoten werden beim Abgeben gesperrt.
          </template>
          <template v-else-if="activeGameMode === 'bankroll'">
            Setze deine Coins auf Spielausgänge.
          </template>
          <template v-else-if="activeGameMode === 'survivor'">
            Wähle ein Team pro Spieltag. Eliminiert bei Verlust.
          </template>
          <template v-else-if="activeGameMode === 'over_under'">
            Tippe ob mehr oder weniger Tore als die Linie fallen.
          </template>
          <template v-else-if="activeGameMode === 'fantasy'">
            Wähle ein Team und sammle Punkte basierend auf Performance.
          </template>
        </p>
      </div>
      <!-- Wallet bar for coin-based modes -->
      <WalletBar v-if="['bankroll', 'over_under'].includes(activeGameMode)" />
    </div>

    <!-- Squad switcher (pill bar) -->
    <div
      v-if="auth.isLoggedIn && spieltagSquads.length > 0"
      class="flex gap-2 overflow-x-auto pb-1"
    >
      <button
        class="shrink-0 px-3 py-1.5 rounded-full text-xs font-medium transition-colors"
        :class="
          !selectedSquad
            ? 'bg-primary text-surface-0'
            : 'bg-surface-1 text-text-secondary hover:bg-surface-2 border border-surface-3'
        "
        @click="selectSquad(null)"
      >
        Global
      </button>
      <button
        v-for="squad in spieltagSquads"
        :key="squad.id"
        class="shrink-0 px-3 py-1.5 rounded-full text-xs font-medium transition-colors"
        :class="
          selectedSquad?.id === squad.id
            ? 'bg-primary text-surface-0'
            : 'bg-surface-1 text-text-secondary hover:bg-surface-2 border border-surface-3'
        "
        @click="selectSquad(squad)"
      >
        {{ squad.name }}
        <span class="opacity-60 ml-0.5"
        >({{ GAME_MODE_LABELS[squadsStore.getGameModeForSport(squad.id, selectedSport)] }})</span>
      </button>
    </div>

    <!-- Sport selector (filtered by squad league_configs) -->
    <div class="flex gap-2 overflow-x-auto pb-1">
      <button
        v-for="sport in availableSports"
        :key="sport.sport_key"
        class="shrink-0 px-4 py-2 rounded-lg text-sm font-medium transition-colors"
        :class="
          selectedSport === sport.sport_key
            ? 'bg-primary text-surface-0'
            : 'bg-surface-1 text-text-secondary hover:bg-surface-2 border border-surface-3'
        "
        @click="selectedSport = sport.sport_key"
      >
        {{ sportLabels[sport.sport_key] || sport.sport_key }}
        <span
          v-if="selectedSquad?.league_configs?.length"
          class="text-[10px] opacity-60 ml-0.5"
        >({{ GAME_MODE_LABELS[squadsStore.getGameModeForSport(selectedSquad.id, sport.sport_key)] }})</span>
      </button>
    </div>

    <!-- Survivor eliminated banner -->
    <div
      v-if="activeGameMode === 'survivor' && survivorStore.entry?.status === 'eliminated'"
      class="bg-red-500/10 border border-red-500/30 rounded-card p-4 text-center"
    >
      <p class="text-sm font-semibold text-red-500">
        Du bist ausgeschieden! Streak: {{ survivorStore.entry.streak }}
      </p>
    </div>

    <!-- Season track -->
    <SeasonTrack
      v-if="spieltag.matchdays.length > 0"
      :matchdays="spieltag.matchdays"
      :current-id="selectedMatchdayId"
      :total-matchdays="totalMatchdays"
      @select="selectMatchday"
      @preview="handleDragPreview"
    />

    <!-- Loading -->
    <div v-if="spieltag.loading" class="space-y-3">
      <div
        v-for="n in 6"
        :key="n"
        class="bg-surface-1 rounded-card h-24 animate-pulse"
      />
    </div>

    <!-- Empty state -->
    <div
      v-else-if="spieltag.matches.length === 0 && !spieltag.loading"
      class="flex flex-col items-center justify-center py-16"
    >
      <span class="text-4xl mb-4" aria-hidden="true">📋</span>
      <h2 class="text-lg font-semibold text-text-primary mb-2">
        Keine Spieltage verfügbar
      </h2>
      <p class="text-sm text-text-secondary text-center max-w-xs">
        Für diese Liga sind noch keine Spieltage geladen. Schau bald wieder vorbei.
      </p>
    </div>

    <!-- Match cards — mode-specific rendering -->
    <template v-else>
      <div class="flex items-center justify-between">
        <h2 class="text-lg font-semibold text-text-primary">
          {{ currentMatchdayLabel }}
        </h2>
        <div class="flex items-center gap-3">
          <!-- Q-Tip refresh -->
          <button
            class="text-text-muted hover:text-primary transition-colors p-1"
            title="Q-Tips aktualisieren"
            :disabled="refreshingTips"
            @click="handleRefreshTips"
          >
            <svg
              class="w-4 h-4"
              :class="{ 'animate-spin': refreshingTips }"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              stroke-width="2"
            >
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h4.586M20 20v-5h-4.586M4.929 9A8 8 0 0119.07 9M19.071 15A8 8 0 014.93 15" />
            </svg>
          </button>
          <span v-if="activeGameMode === 'classic'" class="text-sm text-text-muted">
            {{ spieltag.tippedCount }}/{{ spieltag.matches.length }} getippt
          </span>
        </div>
      </div>

      <!-- Admin prediction panel (squad admin only) -->
      <AdminPredictionPanel
        v-if="selectedSquad?.is_admin && selectedMatchdayId"
        :squad-id="selectedSquad.id"
        :matchday-id="selectedMatchdayId"
      />

      <!-- ============ TIPPSPIEL MODE ============ -->
      <template v-if="activeGameMode === 'classic'">
        <div class="space-y-2">
          <SpieltagMatchCard
            v-for="match in spieltag.matches"
            :key="match.id"
            :match="match"
            :sport-key="selectedSport"
          />
        </div>

        <template v-if="auth.isLoggedIn">
          <AutoTippSelector v-if="!autoTippBlocked" />
          <div
            v-else
            class="bg-surface-1 rounded-card p-4 border border-amber-500/20"
          >
            <p class="text-sm text-text-secondary">
              Auto-Tipp wurde vom Squad-Admin deaktiviert.
            </p>
          </div>
          <div class="flex items-center justify-between pt-2">
            <span
              v-if="spieltag.predictions?.total_points !== null && spieltag.predictions?.total_points !== undefined"
              class="text-sm font-semibold text-text-primary"
            >
              Gesamt: {{ spieltag.predictions.total_points }} Punkte
            </span>
            <span v-else />
            <button
              :disabled="spieltag.saving || spieltag.editableMatches.length === 0"
              class="px-6 py-2.5 rounded-lg text-sm font-semibold transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              :class="
                hasUnsavedChanges
                  ? 'bg-primary text-surface-0 hover:bg-primary-hover'
                  : 'bg-surface-2 text-text-secondary'
              "
              @click="handleSave"
            >
              <template v-if="spieltag.saving">
                <span class="inline-flex items-center gap-2">
                  <svg class="animate-spin h-4 w-4" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none" />
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                  Speichert...
                </span>
              </template>
              <template v-else>Tipps speichern</template>
            </button>
          </div>
        </template>
      </template>

      <!-- ============ QUOTENTIPP (MONEYLINE) MODE ============ -->
      <template v-else-if="activeGameMode === 'moneyline'">
        <div class="space-y-2">
          <QuotentippCard
            v-for="match in spieltag.matches"
            :key="match.id"
            :match="match"
            :sport-key="selectedSport"
          />
        </div>
      </template>

      <!-- ============ BANKROLL MODE ============ -->
      <template v-else-if="activeGameMode === 'bankroll'">
        <div class="space-y-2">
          <BankrollBetCard
            v-for="match in spieltag.matches"
            :key="match.id"
            :match="match"
          />
        </div>
        <BankrollLeaderboard
          v-if="selectedSquad"
          :squad-id="selectedSquad.id"
          :sport-key="selectedSport"
        />
      </template>

      <!-- ============ SURVIVOR MODE ============ -->
      <template v-else-if="activeGameMode === 'survivor'">
        <div class="space-y-2">
          <SurvivorPickCard
            v-for="match in spieltag.matches"
            :key="match.id"
            :match="match"
            :squad-id="selectedSquad!.id"
          />
        </div>
        <SurvivorStandings
          v-if="selectedSquad"
          :squad-id="selectedSquad.id"
          :sport-key="selectedSport"
        />
      </template>

      <!-- ============ OVER/UNDER MODE ============ -->
      <template v-else-if="activeGameMode === 'over_under'">
        <div class="space-y-2">
          <OverUnderCard
            v-for="match in spieltag.matches"
            :key="match.id"
            :match="match"
            :sport-key="selectedSport"
          />
        </div>
        <OverUnderLeaderboard
          v-if="selectedSquad"
          :squad-id="selectedSquad.id"
          :sport-key="selectedSport"
        />
      </template>

      <!-- ============ FANTASY MODE ============ -->
      <template v-else-if="activeGameMode === 'fantasy'">
        <div class="space-y-2">
          <FantasyPickCard
            v-for="match in spieltag.matches"
            :key="match.id"
            :match="match"
            :squad-id="selectedSquad!.id"
          />
        </div>
        <FantasyStandings
          v-if="selectedSquad"
          :squad-id="selectedSquad.id"
          :sport-key="selectedSport"
        />
      </template>

      <!-- Parlay builder (Classic + Bankroll) -->
      <ParlayBuilder
        v-if="parlayAvailable && selectedMatchdayId && auth.isLoggedIn"
        :matches="spieltag.matches"
        :squad-id="selectedSquad?.id || ''"
        :matchday-id="selectedMatchdayId"
        :game-mode="activeGameMode"
      />

      <!-- Not logged in hint -->
      <div
        v-if="!auth.isLoggedIn"
        class="bg-surface-1 rounded-card p-4 border border-primary/20 text-center"
      >
        <p class="text-sm text-text-secondary">
          <RouterLink to="/login" class="text-primary font-medium hover:underline">
            Anmelden
          </RouterLink>
          um deine Tipps zu speichern.
        </p>
      </div>

      <!-- Leaderboard (Tippspiel mode) -->
      <template v-if="activeGameMode === 'classic'">
        <SpieltagLeaderboard
          v-if="spieltag.currentMatchday?.all_resolved && selectedMatchdayId"
          :sport-key="selectedSport"
          :matchday-id="selectedMatchdayId"
          mode="matchday"
        />
        <SpieltagLeaderboard
          :sport-key="selectedSport"
          mode="season"
        />
      </template>
    </template>
  </div>

  <!-- Disclaimer modal -->
  <WalletDisclaimer
    v-if="showDisclaimer"
    @accepted="onDisclaimerAccepted"
  />
</template>
