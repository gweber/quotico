<script setup lang="ts">
import { onMounted, computed, ref, watch } from "vue";
import { useBattlesStore, type Battle, type SquadSearchResult } from "@/stores/battles";
import { useSquadsStore } from "@/stores/squads";
import { useToast } from "@/composables/useToast";
import BattleCard from "@/components/BattleCard.vue";

const battles = useBattlesStore();
const squadsStore = useSquadsStore();
const toast = useToast();

// My battles
const activeBattles = computed(() =>
  battles.battles.filter((b) => b.status === "active")
);
const upcomingBattles = computed(() =>
  battles.battles.filter((b) => b.status === "upcoming")
);
const outgoingChallenges = computed(() =>
  battles.battles.filter((b) => b.status === "open" || b.status === "pending")
);

// Lobby
const openChallenges = computed(() =>
  battles.lobby.filter((b) => b.status === "open")
);
const incomingChallenges = computed(() =>
  battles.lobby.filter((b) => b.status === "pending")
);

// Challenge creation
const adminSquads = computed(() =>
  squadsStore.squads.filter((s) => s.is_admin)
);
const showCreateForm = ref(false);
const createSquadId = ref("");
const createStartDate = ref("");
const createEndDate = ref("");
const createMode = ref<"open" | "direct">("open");
const creating = ref(false);

// Target squad search (direct challenge)
const targetQuery = ref("");
const targetResults = ref<SquadSearchResult[]>([]);
const selectedTarget = ref<SquadSearchResult | null>(null);
const targetIdManual = ref("");
const targetInputMode = ref<"search" | "id">("search");
let searchTimeout: ReturnType<typeof setTimeout> | null = null;

watch(targetQuery, (q) => {
  if (searchTimeout) clearTimeout(searchTimeout);
  if (!q || q.length < 2) {
    targetResults.value = [];
    return;
  }
  searchTimeout = setTimeout(async () => {
    targetResults.value = await battles.searchSquads(q);
  }, 300);
});

function selectTarget(squad: SquadSearchResult) {
  selectedTarget.value = squad;
  targetQuery.value = squad.name;
  targetResults.value = [];
}

function clearTarget() {
  selectedTarget.value = null;
  targetQuery.value = "";
  targetIdManual.value = "";
  targetResults.value = [];
}

// Squads the user can accept challenges with
const acceptableSquads = computed(() =>
  squadsStore.squads.filter((s) => s.is_admin)
);

const resolvedTargetId = computed(() => {
  if (createMode.value !== "direct") return undefined;
  if (targetInputMode.value === "search") return selectedTarget.value?.id;
  return targetIdManual.value || undefined;
});

onMounted(async () => {
  await Promise.all([
    battles.fetchMyBattles(),
    battles.fetchLobby(),
    squadsStore.squads.length === 0 ? squadsStore.fetchMySquads() : Promise.resolve(),
  ]);
  // Pre-select first admin squad
  if (adminSquads.value.length > 0 && !createSquadId.value) {
    createSquadId.value = adminSquads.value[0].id;
  }
});

async function handleCommit(battle: Battle, squadId: string) {
  try {
    await battles.commitToBattle(battle.id, squadId);
  } catch (e: any) {
    toast.error(e.message || "Commitment fehlgeschlagen.");
  }
}

async function handleCreate() {
  if (!createSquadId.value || !createStartDate.value || !createEndDate.value) {
    toast.error("Bitte alle Felder ausfüllen.");
    return;
  }
  if (createMode.value === "direct" && !resolvedTargetId.value) {
    toast.error("Bitte einen Ziel-Squad auswählen.");
    return;
  }
  creating.value = true;
  const ok = await battles.createChallenge(
    createSquadId.value,
    new Date(createStartDate.value).toISOString(),
    new Date(createEndDate.value).toISOString(),
    resolvedTargetId.value,
  );
  creating.value = false;
  if (ok) {
    showCreateForm.value = false;
    createStartDate.value = "";
    createEndDate.value = "";
    clearTarget();
    await battles.fetchLobby();
  }
}

async function handleAccept(battle: Battle, squadId: string) {
  const ok = await battles.acceptChallenge(battle.id, squadId);
  if (!ok) toast.error("Annehmen fehlgeschlagen.");
}

async function handleDecline(battle: Battle) {
  const ok = await battles.declineChallenge(battle.id);
  if (!ok) toast.error("Ablehnen fehlgeschlagen.");
}
</script>

<template>
  <div class="max-w-2xl mx-auto p-4">
    <div class="flex items-center justify-between mb-6">
      <h1 class="text-xl font-bold text-text-primary">Battle Center</h1>
      <button
        v-if="adminSquads.length > 0 && !showCreateForm"
        class="text-sm px-3 py-1.5 rounded-lg bg-primary/10 text-primary border border-primary/30 hover:bg-primary/20 transition-colors font-medium"
        @click="showCreateForm = true"
      >
        + Herausforderung
      </button>
    </div>

    <!-- Create Challenge Form -->
    <div
      v-if="showCreateForm"
      class="bg-surface-1 rounded-card border border-surface-3/50 p-4 mb-6"
    >
      <h2 class="text-sm font-semibold text-text-primary mb-3">Neue Herausforderung</h2>

      <div class="space-y-3">
        <!-- Squad selector -->
        <div>
          <label class="text-xs text-text-muted block mb-1">Dein Squad</label>
          <select
            v-model="createSquadId"
            class="w-full bg-surface-2 border border-surface-3 rounded-lg px-3 py-2 text-sm text-text-primary"
          >
            <option v-for="s in adminSquads" :key="s.id" :value="s.id">{{ s.name }}</option>
          </select>
        </div>

        <!-- Type toggle -->
        <div class="flex gap-2">
          <button
            class="flex-1 py-2 text-sm rounded-lg border transition-colors font-medium"
            :class="createMode === 'open'
              ? 'bg-primary/15 text-primary border-primary/40'
              : 'bg-surface-2 text-text-muted border-surface-3 hover:border-surface-3/80'"
            @click="createMode = 'open'"
          >
            Offene Herausforderung
          </button>
          <button
            class="flex-1 py-2 text-sm rounded-lg border transition-colors font-medium"
            :class="createMode === 'direct'
              ? 'bg-primary/15 text-primary border-primary/40'
              : 'bg-surface-2 text-text-muted border-surface-3 hover:border-surface-3/80'"
            @click="createMode = 'direct'"
          >
            Direkte Herausforderung
          </button>
        </div>

        <!-- Target squad (direct only) -->
        <div v-if="createMode === 'direct'">
          <div class="flex items-center justify-between mb-1">
            <label class="text-xs text-text-muted">Ziel-Squad</label>
            <button
              class="text-xs text-text-muted hover:text-text-secondary transition-colors"
              @click="targetInputMode = targetInputMode === 'search' ? 'id' : 'search'; clearTarget()"
            >
              {{ targetInputMode === "search" ? "Per ID eingeben" : "Per Name suchen" }}
            </button>
          </div>

          <!-- Search by name -->
          <div v-if="targetInputMode === 'search'" class="relative">
            <div v-if="selectedTarget" class="flex items-center gap-2 bg-surface-2 border border-primary/30 rounded-lg px-3 py-2">
              <span class="text-sm text-text-primary flex-1">{{ selectedTarget.name }}</span>
              <span class="text-xs text-text-muted">{{ selectedTarget.member_count }} Mitglieder</span>
              <button
                class="text-text-muted hover:text-text-primary text-sm"
                @click="clearTarget"
              >
                &times;
              </button>
            </div>
            <template v-else>
              <input
                v-model="targetQuery"
                type="text"
                placeholder="Squad-Name suchen..."
                class="w-full bg-surface-2 border border-surface-3 rounded-lg px-3 py-2 text-sm text-text-primary placeholder:text-text-muted/50"
              />
              <div
                v-if="targetResults.length > 0"
                class="absolute z-10 left-0 right-0 mt-1 bg-surface-1 border border-surface-3 rounded-lg shadow-lg overflow-hidden"
              >
                <button
                  v-for="s in targetResults"
                  :key="s.id"
                  class="w-full px-3 py-2 text-left hover:bg-surface-2 transition-colors flex items-center justify-between"
                  @click="selectTarget(s)"
                >
                  <span class="text-sm text-text-primary">{{ s.name }}</span>
                  <span class="text-xs text-text-muted">{{ s.member_count }} Mitgl.</span>
                </button>
              </div>
            </template>
          </div>

          <!-- Manual ID input -->
          <input
            v-else
            v-model="targetIdManual"
            type="text"
            placeholder="Squad-ID eingeben"
            class="w-full bg-surface-2 border border-surface-3 rounded-lg px-3 py-2 text-sm text-text-primary placeholder:text-text-muted/50"
          />
        </div>

        <!-- Date range -->
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="text-xs text-text-muted block mb-1">Start</label>
            <input
              v-model="createStartDate"
              type="datetime-local"
              class="w-full bg-surface-2 border border-surface-3 rounded-lg px-3 py-2 text-sm text-text-primary"
            />
          </div>
          <div>
            <label class="text-xs text-text-muted block mb-1">Ende</label>
            <input
              v-model="createEndDate"
              type="datetime-local"
              class="w-full bg-surface-2 border border-surface-3 rounded-lg px-3 py-2 text-sm text-text-primary"
            />
          </div>
        </div>

        <!-- Actions -->
        <div class="flex gap-2 pt-1">
          <button
            class="flex-1 py-2 text-sm rounded-lg bg-primary text-white font-medium hover:bg-primary/90 transition-colors disabled:opacity-50"
            :disabled="creating"
            @click="handleCreate"
          >
            {{ creating ? "Erstelle..." : "Erstellen" }}
          </button>
          <button
            class="py-2 px-4 text-sm rounded-lg bg-surface-2 text-text-muted border border-surface-3 hover:bg-surface-3/50 transition-colors"
            @click="showCreateForm = false"
          >
            Abbrechen
          </button>
        </div>
      </div>
    </div>

    <!-- Loading -->
    <div v-if="battles.loading && battles.battles.length === 0" class="space-y-4">
      <div v-for="n in 2" :key="n" class="bg-surface-1 rounded-card h-40 animate-pulse" />
    </div>

    <!-- Incoming Challenges (direct) -->
    <section v-if="incomingChallenges.length > 0" class="mb-8">
      <h2 class="text-sm font-semibold text-text-muted uppercase tracking-wider mb-3">
        Eingehende Herausforderungen
      </h2>
      <div class="space-y-4">
        <BattleCard
          v-for="battle in incomingChallenges"
          :key="battle.id"
          :battle="battle"
          :acceptable-squads="acceptableSquads"
          @accept="(squadId) => handleAccept(battle, squadId)"
          @decline="handleDecline(battle)"
        />
      </div>
    </section>

    <!-- Open Challenges (lobby) -->
    <section v-if="openChallenges.length > 0" class="mb-8">
      <h2 class="text-sm font-semibold text-text-muted uppercase tracking-wider mb-3">
        Offene Herausforderungen
      </h2>
      <div class="space-y-4">
        <BattleCard
          v-for="battle in openChallenges"
          :key="battle.id"
          :battle="battle"
          :acceptable-squads="acceptableSquads"
          @accept="(squadId) => handleAccept(battle, squadId)"
        />
      </div>
    </section>

    <!-- Outgoing challenges (my open/pending) -->
    <section v-if="outgoingChallenges.length > 0" class="mb-8">
      <h2 class="text-sm font-semibold text-text-muted uppercase tracking-wider mb-3">
        Meine Herausforderungen
      </h2>
      <div class="space-y-4">
        <BattleCard
          v-for="battle in outgoingChallenges"
          :key="battle.id"
          :battle="battle"
        />
      </div>
    </section>

    <!-- Active Battles -->
    <section v-if="activeBattles.length > 0" class="mb-8">
      <h2 class="text-sm font-semibold text-text-muted uppercase tracking-wider mb-3">
        Laufende Battles
      </h2>
      <div class="space-y-4">
        <BattleCard
          v-for="battle in activeBattles"
          :key="battle.id"
          :battle="battle"
          @commit="(squadId) => handleCommit(battle, squadId)"
        />
      </div>
    </section>

    <!-- Upcoming Battles -->
    <section v-if="upcomingBattles.length > 0" class="mb-8">
      <h2 class="text-sm font-semibold text-text-muted uppercase tracking-wider mb-3">
        Geplante Battles
      </h2>
      <div class="space-y-4">
        <BattleCard
          v-for="battle in upcomingBattles"
          :key="battle.id"
          :battle="battle"
          @commit="(squadId) => handleCommit(battle, squadId)"
        />
      </div>
    </section>

    <!-- Empty state -->
    <div
      v-if="!battles.loading && battles.battles.length === 0 && battles.lobby.length === 0 && !showCreateForm"
      class="text-center py-16"
    >
      <p class="text-4xl mb-4" aria-hidden="true">&#x2694;&#xFE0F;</p>
      <h2 class="text-lg font-semibold text-text-primary mb-2">Keine Battles</h2>
      <p class="text-sm text-text-secondary max-w-xs mx-auto">
        Tritt einem Squad bei und starte ein Battle gegen ein anderes Team.
      </p>
    </div>
  </div>
</template>
