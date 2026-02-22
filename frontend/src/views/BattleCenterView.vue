<script setup lang="ts">
import { onMounted, computed } from "vue";
import { useBattlesStore, type Battle } from "@/stores/battles";
import { useToast } from "@/composables/useToast";
import BattleCard from "@/components/BattleCard.vue";

const battles = useBattlesStore();
const toast = useToast();

const activeBattles = computed(() =>
  battles.battles.filter((b) => b.status === "active")
);
const upcomingBattles = computed(() =>
  battles.battles.filter((b) => b.status === "upcoming")
);

onMounted(() => {
  battles.fetchMyBattles();
});

async function handleCommit(battle: Battle, squadId: string) {
  try {
    await battles.commitToBattle(battle.id, squadId);
  } catch (e: any) {
    toast.error(e.message || "Commitment fehlgeschlagen.");
  }
}
</script>

<template>
  <div class="max-w-2xl mx-auto p-4">
    <h1 class="text-xl font-bold text-text-primary mb-6">Battle Center</h1>

    <!-- Loading -->
    <div v-if="battles.loading" class="space-y-4">
      <div v-for="n in 2" :key="n" class="bg-surface-1 rounded-card h-40 animate-pulse" />
    </div>

    <!-- Empty -->
    <div v-else-if="battles.battles.length === 0" class="text-center py-16">
      <p class="text-4xl mb-4" aria-hidden="true">&#x2694;&#xFE0F;</p>
      <h2 class="text-lg font-semibold text-text-primary mb-2">Keine Battles</h2>
      <p class="text-sm text-text-secondary max-w-xs mx-auto">
        Tritt einem Squad bei und starte ein Battle gegen ein anderes Team.
      </p>
    </div>

    <template v-else>
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
      <section v-if="upcomingBattles.length > 0">
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
    </template>
  </div>
</template>
