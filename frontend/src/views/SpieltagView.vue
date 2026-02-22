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
import SpieltagMatchCard from "@/components/SpieltagMatchCard.vue";
import SpieltagNav from "@/components/SpieltagNav.vue";
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

// Whether parlay is available (classic + bankroll)
const parlayAvailable = computed(() =>
  ["classic", "bankroll"].includes(activeGameMode.value)
);

async function loadSport(sport: string) {
  selectedSport.value = sport;
  spieltag.setSport(sport);
  await spieltag.fetchMatchdays(sport);

  if (route.params.matchday && spieltag.matchdays.length > 0) {
    const mdNum = parseInt(route.params.matchday as string);
    const md = spieltag.matchdays.find((m) => m.matchday_number === mdNum);
    if (md) {
      await selectMatchday(md.id);
      return;
    }
  }

  const current =
    spieltag.matchdays.find((md) => md.status !== "completed") ||
    spieltag.matchdays[spieltag.matchdays.length - 1];
  if (current) {
    await selectMatchday(current.id);
  }
}

async function selectMatchday(id: string) {
  selectedMatchdayId.value = id;
  await spieltag.fetchMatchdayDetail(id);
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
  await loadSport(selectedSport.value);
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
        <h1 class="text-2xl font-bold text-text-primary">Spieltag-Modus</h1>
        <p class="text-sm text-text-secondary mt-1">
          <template v-if="activeGameMode === 'classic'">
            Tippe die exakten Ergebnisse aller Spiele eines Spieltags.
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
      v-if="auth.isLoggedIn && squadsStore.squads.length > 0"
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
        Global (Classic)
      </button>
      <button
        v-for="squad in squadsStore.squads"
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
        <span class="opacity-60 ml-0.5">({{ GAME_MODE_LABELS[squadsStore.getGameModeForSport(squad.id, selectedSport)] || squad.game_mode }})</span>
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

    <!-- Matchday nav -->
    <SpieltagNav
      v-if="spieltag.matchdays.length > 0"
      :matchdays="spieltag.matchdays"
      :current-id="selectedMatchdayId"
      @select="selectMatchday"
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
        <span v-if="activeGameMode === 'classic'" class="text-sm text-text-muted">
          {{ spieltag.tippedCount }}/{{ spieltag.matches.length }} getippt
        </span>
      </div>

      <!-- ============ CLASSIC MODE ============ -->
      <template v-if="activeGameMode === 'classic'">
        <div class="space-y-2">
          <SpieltagMatchCard
            v-for="match in spieltag.matches"
            :key="match.id"
            :match="match"
          />
        </div>

        <template v-if="auth.isLoggedIn">
          <AutoTippSelector />
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

      <!-- Leaderboard (Classic mode) -->
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
