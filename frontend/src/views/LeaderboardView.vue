<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useApi } from "@/composables/useApi";
import { useAuthStore } from "@/stores/auth";
import LeaderboardTable from "@/components/LeaderboardTable.vue";

interface LeaderboardEntry {
  rank: number;
  alias: string;
  points: number;
  is_bot?: boolean;
}

const api = useApi();
const auth = useAuthStore();
const entries = ref<LeaderboardEntry[]>([]);
const loading = ref(true);
const error = ref(false);

async function reload() {
  error.value = false;
  loading.value = true;
  try {
    entries.value = await api.get<LeaderboardEntry[]>("/leaderboard/");
  } catch {
    error.value = true;
    entries.value = [];
  } finally {
    loading.value = false;
  }
}

onMounted(() => reload());
</script>

<template>
  <div class="max-w-3xl mx-auto px-4 py-8">
    <div class="mb-8">
      <h1 class="text-2xl font-bold text-text-primary">{{ $t('leaderboard.heading') }}</h1>
      <p class="text-sm text-text-secondary mt-1">
        {{ $t('leaderboard.description') }}
      </p>
    </div>

    <!-- Error -->
    <div v-if="error" class="text-center py-12">
      <p class="text-text-muted mb-3">{{ $t('common.loadError') }}</p>
      <button class="text-sm text-primary hover:underline" @click="reload">{{ $t('common.retry') }}</button>
    </div>

    <div v-else class="bg-surface-1 rounded-card overflow-hidden">
      <LeaderboardTable
        :entries="entries"
        :loading="loading"
        :current-user-alias="auth.user?.alias"
      />
    </div>
  </div>
</template>
