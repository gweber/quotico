<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useApi } from "@/composables/useApi";
import { useAuthStore } from "@/stores/auth";
import LeaderboardTable from "@/components/LeaderboardTable.vue";

interface LeaderboardEntry {
  rank: number;
  alias: string;
  points: number;
}

const api = useApi();
const auth = useAuthStore();
const entries = ref<LeaderboardEntry[]>([]);
const loading = ref(true);

onMounted(async () => {
  try {
    entries.value = await api.get<LeaderboardEntry[]>("/leaderboard/");
  } catch {
    entries.value = [];
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <div class="max-w-3xl mx-auto px-4 py-8">
    <div class="mb-8">
      <h1 class="text-2xl font-bold text-text-primary">Rangliste</h1>
      <p class="text-sm text-text-secondary mt-1">
        Die besten Tipper auf einen Blick.
      </p>
    </div>

    <div class="bg-surface-1 rounded-card overflow-hidden">
      <LeaderboardTable
        :entries="entries"
        :loading="loading"
        :current-user-alias="auth.user?.alias"
      />
    </div>
  </div>
</template>
