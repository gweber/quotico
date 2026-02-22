<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useSquadsStore } from "@/stores/squads";
import { useToast } from "@/composables/useToast";

const route = useRoute();
const router = useRouter();
const squads = useSquadsStore();
const toast = useToast();

const squadId = computed(() => route.params.id as string);
const squad = computed(() => squads.squads.find((s) => s.id === squadId.value) ?? null);
const copied = ref(false);
const showLeaveConfirm = ref(false);

onMounted(async () => {
  if (squads.squads.length === 0) {
    await squads.fetchMySquads();
  }
  if (squadId.value) {
    await squads.fetchLeaderboard(squadId.value);
  }
});

async function copyInviteCode() {
  if (!squad.value) return;
  await navigator.clipboard.writeText(squad.value.invite_code);
  copied.value = true;
  toast.success("Einladungscode kopiert!");
  setTimeout(() => (copied.value = false), 2000);
}

async function handleLeave() {
  await squads.leaveSquad(squadId.value);
  router.push("/squads");
}
</script>

<template>
  <div class="max-w-2xl mx-auto p-4">
    <!-- Back -->
    <RouterLink to="/squads" class="inline-flex items-center gap-1 text-sm text-text-muted hover:text-text-primary mb-4">
      <span aria-hidden="true">&larr;</span> Alle Squads
    </RouterLink>

    <template v-if="squad">
      <!-- Header -->
      <div class="bg-surface-1 rounded-card p-5 border border-surface-3/50 mb-4">
        <div class="flex items-start justify-between">
          <div>
            <h1 class="text-xl font-bold text-text-primary">{{ squad.name }}</h1>
            <p v-if="squad.description" class="text-sm text-text-muted mt-1">{{ squad.description }}</p>
            <p class="text-xs text-text-muted mt-2">{{ squad.member_count }} Mitglieder</p>
          </div>
          <span
            v-if="squad.is_admin"
            class="px-2 py-0.5 text-xs font-medium bg-primary-muted/20 text-primary rounded-full"
          >Admin</span>
        </div>

        <!-- Invite code -->
        <div class="mt-4 flex items-center gap-3">
          <div class="flex-1 bg-surface-2 rounded-lg px-4 py-2.5 font-mono text-sm text-text-primary tracking-wider text-center border border-surface-3">
            {{ squad.invite_code }}
          </div>
          <button
            class="shrink-0 px-4 py-2.5 text-sm rounded-lg bg-surface-2 text-text-primary hover:bg-surface-3 transition-colors border border-surface-3"
            @click="copyInviteCode"
          >
            {{ copied ? "Kopiert!" : "Kopieren" }}
          </button>
        </div>

        <!-- Leave button (non-admin) -->
        <div v-if="!squad.is_admin" class="mt-4 pt-4 border-t border-surface-3/50">
          <button
            v-if="!showLeaveConfirm"
            class="text-xs text-text-muted hover:text-danger transition-colors"
            @click="showLeaveConfirm = true"
          >
            Squad verlassen
          </button>
          <div v-else class="flex items-center gap-2">
            <span class="text-xs text-text-muted">Sicher?</span>
            <button
              class="text-xs px-3 py-1 rounded bg-danger text-white hover:bg-danger/80 transition-colors"
              @click="handleLeave"
            >Ja, verlassen</button>
            <button
              class="text-xs text-text-muted hover:text-text-primary"
              @click="showLeaveConfirm = false"
            >Abbrechen</button>
          </div>
        </div>
      </div>

      <!-- Squad Leaderboard -->
      <div class="bg-surface-1 rounded-card border border-surface-3/50">
        <h2 class="text-sm font-semibold text-text-primary px-5 py-3 border-b border-surface-3/50">
          Squad-Rangliste
        </h2>
        <div v-if="squads.leaderboard.length === 0" class="px-5 py-8 text-center">
          <p class="text-sm text-text-muted">Noch keine Tipps abgegeben.</p>
        </div>
        <table v-else class="w-full text-sm">
          <thead>
            <tr class="text-xs text-text-muted border-b border-surface-3/30">
              <th class="text-left px-5 py-2 font-medium w-12">#</th>
              <th class="text-left py-2 font-medium">Spieler</th>
              <th class="text-right py-2 font-medium pr-5">Punkte</th>
              <th class="text-right py-2 font-medium pr-5 hidden sm:table-cell">Tipps</th>
              <th class="text-right py-2 font-medium pr-5 hidden sm:table-cell">&#x2300; Quote</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="entry in squads.leaderboard"
              :key="entry.user_id"
              class="border-b border-surface-3/20 last:border-0"
            >
              <td class="px-5 py-3 font-mono">
                <span v-if="entry.rank === 1" class="text-warning">&#x1F947;</span>
                <span v-else-if="entry.rank === 2">&#x1F948;</span>
                <span v-else-if="entry.rank === 3">&#x1F949;</span>
                <span v-else class="text-text-muted">{{ entry.rank }}</span>
              </td>
              <td class="py-3 text-text-primary truncate max-w-[200px]">{{ entry.alias }}</td>
              <td class="py-3 pr-5 text-right font-medium text-text-primary tabular-nums">
                {{ entry.points.toFixed(1) }}
              </td>
              <td class="py-3 pr-5 text-right text-text-muted tabular-nums hidden sm:table-cell">
                {{ entry.tip_count }}
              </td>
              <td class="py-3 pr-5 text-right text-text-muted tabular-nums hidden sm:table-cell">
                {{ entry.avg_odds.toFixed(2) }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- Not found -->
    <div v-else class="text-center py-16">
      <p class="text-sm text-text-muted">Squad nicht gefunden.</p>
    </div>
  </div>
</template>
