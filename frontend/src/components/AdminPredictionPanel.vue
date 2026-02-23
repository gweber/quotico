<script setup lang="ts">
import { ref, watch, computed } from "vue";
import { useSpieltagStore } from "@/stores/spieltag";
import { useToast } from "@/composables/useToast";

const props = defineProps<{
  squadId: string;
  matchdayId: string;
}>();

const spieltag = useSpieltagStore();
const toast = useToast();

const expanded = ref(false);
const selectedUserId = ref<string | null>(null);
const savingMatch = ref<string | null>(null);
const unlockingMatch = ref<string | null>(null);

// Draft scores for admin entry (keyed by match_id)
const adminDrafts = ref<Map<string, { home: number; away: number }>>(new Map());

// Load squad members when panel is expanded
watch(expanded, async (isOpen) => {
  if (isOpen && spieltag.squadMembers.length === 0) {
    await spieltag.fetchSquadMembers(props.squadId);
  }
});

// Load target user's predictions when selected
watch(selectedUserId, async (userId) => {
  if (userId && props.matchdayId) {
    await spieltag.fetchAdminPredictions(
      props.matchdayId,
      props.squadId,
      userId
    );
    // Populate drafts from existing predictions
    adminDrafts.value = new Map();
    if (spieltag.adminTargetPredictions?.predictions) {
      for (const p of spieltag.adminTargetPredictions.predictions) {
        adminDrafts.value.set(p.match_id, {
          home: p.home_score,
          away: p.away_score,
        });
      }
    }
  } else {
    spieltag.adminTargetPredictions = null;
    adminDrafts.value = new Map();
  }
});

const unlockedSet = computed(
  () =>
    new Set(
      spieltag.adminTargetPredictions?.admin_unlocked_matches ?? []
    )
);

function getPrediction(matchId: string) {
  return spieltag.adminTargetPredictions?.predictions.find(
    (p) => p.match_id === matchId
  );
}

function getDraft(matchId: string) {
  return adminDrafts.value.get(matchId);
}

function setDraft(matchId: string, home: number, away: number) {
  const newMap = new Map(adminDrafts.value);
  newMap.set(matchId, { home, away });
  adminDrafts.value = newMap;
}

async function handleUnlock(matchId: string) {
  if (!selectedUserId.value) return;
  unlockingMatch.value = matchId;
  const ok = await spieltag.adminUnlockMatch(
    props.squadId,
    props.matchdayId,
    selectedUserId.value,
    matchId
  );
  if (ok) {
    toast.success("Spiel entsperrt.");
  } else {
    toast.error("Fehler beim Entsperren.");
  }
  unlockingMatch.value = null;
}

async function handleSavePrediction(matchId: string) {
  if (!selectedUserId.value) return;
  const draft = getDraft(matchId);
  if (!draft) return;
  savingMatch.value = matchId;
  const result = await spieltag.adminSavePrediction(
    props.squadId,
    props.matchdayId,
    selectedUserId.value,
    matchId,
    draft.home,
    draft.away
  );
  if (result) {
    const ptsMsg =
      result.points_earned !== null
        ? ` (${result.points_earned} Pkt.)`
        : "";
    toast.success(`Tipp eingetragen${ptsMsg}.`);
  } else {
    toast.error("Fehler beim Speichern.");
  }
  savingMatch.value = null;
}
</script>

<template>
  <div class="bg-surface-1 rounded-card border border-amber-500/20">
    <!-- Toggle header -->
    <button
      class="w-full flex items-center justify-between px-4 py-3 text-left"
      @click="expanded = !expanded"
    >
      <div class="flex items-center gap-2">
        <svg
          class="w-4 h-4 text-amber-500"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          stroke-width="2"
        >
          <path
            stroke-linecap="round"
            stroke-linejoin="round"
            d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"
          />
        </svg>
        <span class="text-sm font-medium text-text-primary"
          >Admin: Tipps verwalten</span
        >
      </div>
      <svg
        class="w-4 h-4 text-text-muted transition-transform"
        :class="{ 'rotate-180': expanded }"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        stroke-width="2"
      >
        <path
          stroke-linecap="round"
          stroke-linejoin="round"
          d="M19 9l-7 7-7-7"
        />
      </svg>
    </button>

    <!-- Expanded content -->
    <div v-if="expanded" class="px-4 pb-4 space-y-3">
      <!-- Member selector -->
      <div>
        <label class="text-xs text-text-muted block mb-1">Mitglied</label>
        <select
          v-model="selectedUserId"
          class="w-full bg-surface-2 border border-surface-3 rounded-lg px-3 py-2 text-sm text-text-primary focus:outline-none focus:ring-1 focus:ring-primary"
        >
          <option :value="null">-- Mitglied wählen --</option>
          <option
            v-for="member in spieltag.squadMembers"
            :key="member.user_id"
            :value="member.user_id"
          >
            {{ member.alias }}
          </option>
        </select>
      </div>

      <!-- Match list for selected user -->
      <template v-if="selectedUserId">
        <div class="space-y-2">
          <div
            v-for="match in spieltag.matches"
            :key="match.id"
            class="bg-surface-2 rounded-lg p-3 border border-surface-3/50"
          >
            <div class="flex items-center justify-between mb-2">
              <div class="flex-1 min-w-0">
                <p class="text-xs font-medium text-text-primary truncate">
                  {{ match.teams.home }} — {{ match.teams.away }}
                </p>
                <div class="flex items-center gap-2 mt-0.5">
                  <span
                    v-if="match.status === 'completed'"
                    class="text-[10px] text-text-muted"
                    >Ergebnis: {{ match.home_score }}:{{
                      match.away_score
                    }}</span
                  >
                  <span
                    v-if="match.is_locked && !unlockedSet.has(match.id)"
                    class="text-[10px] px-1.5 py-0.5 rounded bg-red-500/10 text-red-500"
                    >Gesperrt</span
                  >
                  <span
                    v-if="unlockedSet.has(match.id)"
                    class="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-600"
                    >Entsperrt</span
                  >
                </div>
              </div>

              <!-- Existing prediction display -->
              <div v-if="getPrediction(match.id)" class="text-right">
                <span class="text-xs text-text-muted">Tipp: </span>
                <span class="text-xs font-medium text-text-primary">
                  {{ getPrediction(match.id)!.home_score }}:{{
                    getPrediction(match.id)!.away_score
                  }}
                </span>
                <span
                  v-if="getPrediction(match.id)!.points_earned !== null"
                  class="ml-1 text-xs font-medium"
                  :class="{
                    'text-green-500':
                      getPrediction(match.id)!.points_earned! >= 2,
                    'text-amber-500':
                      getPrediction(match.id)!.points_earned === 1,
                    'text-red-500':
                      getPrediction(match.id)!.points_earned === 0,
                  }"
                >
                  ({{ getPrediction(match.id)!.points_earned }} Pkt.)
                </span>
                <span
                  v-if="getPrediction(match.id)!.is_admin_entry"
                  class="ml-1 text-[10px] text-amber-500"
                  >(Admin)</span
                >
              </div>
            </div>

            <!-- Admin actions -->
            <div class="flex items-center gap-2">
              <!-- Score inputs -->
              <input
                type="number"
                min="0"
                max="99"
                :value="getDraft(match.id)?.home ?? ''"
                class="w-12 bg-surface-1 border border-surface-3 rounded px-2 py-1 text-sm text-center text-text-primary focus:outline-none focus:ring-1 focus:ring-primary"
                placeholder="H"
                @input="
                  setDraft(
                    match.id,
                    parseInt(($event.target as HTMLInputElement).value) || 0,
                    getDraft(match.id)?.away ?? 0
                  )
                "
              />
              <span class="text-text-muted text-xs">:</span>
              <input
                type="number"
                min="0"
                max="99"
                :value="getDraft(match.id)?.away ?? ''"
                class="w-12 bg-surface-1 border border-surface-3 rounded px-2 py-1 text-sm text-center text-text-primary focus:outline-none focus:ring-1 focus:ring-primary"
                placeholder="A"
                @input="
                  setDraft(
                    match.id,
                    getDraft(match.id)?.home ?? 0,
                    parseInt(($event.target as HTMLInputElement).value) || 0
                  )
                "
              />

              <!-- Save prediction button -->
              <button
                v-if="getDraft(match.id)"
                class="px-2 py-1 text-xs font-medium rounded bg-primary text-surface-0 hover:bg-primary/80 transition-colors disabled:opacity-50"
                :disabled="savingMatch === match.id"
                @click="handleSavePrediction(match.id)"
              >
                {{ savingMatch === match.id ? "..." : "Eintragen" }}
              </button>

              <!-- Unlock button (only for locked matches without an unlock) -->
              <button
                v-if="
                  match.is_locked &&
                  !unlockedSet.has(match.id) &&
                  !getPrediction(match.id)
                "
                class="px-2 py-1 text-xs font-medium rounded bg-amber-500/10 text-amber-600 hover:bg-amber-500/20 transition-colors disabled:opacity-50"
                :disabled="unlockingMatch === match.id"
                @click="handleUnlock(match.id)"
              >
                {{
                  unlockingMatch === match.id ? "..." : "Entsperren"
                }}
              </button>
            </div>
          </div>
        </div>
      </template>

      <p
        v-else
        class="text-xs text-text-muted text-center py-2"
      >
        Wähle ein Mitglied, um dessen Tipps zu verwalten.
      </p>
    </div>
  </div>
</template>
