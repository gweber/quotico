<script setup lang="ts">
defineProps<{
  entries: Array<{
    rank: number;
    alias: string;
    points: number;
    is_bot?: boolean;
  }>;
  loading?: boolean;
  currentUserAlias?: string;
}>();
</script>

<template>
  <!-- Loading skeleton -->
  <div v-if="loading" class="space-y-2">
    <div
      v-for="n in 10"
      :key="n"
      class="bg-surface-2 rounded-lg h-12 animate-pulse"
    />
  </div>

  <!-- Empty -->
  <div
    v-else-if="entries.length === 0"
    class="text-center py-12"
  >
    <p class="text-text-muted">Noch keine Einträge in der Rangliste.</p>
  </div>

  <!-- Table -->
  <div v-else class="overflow-x-auto">
    <table class="w-full text-sm" aria-label="Rangliste">
      <thead>
        <tr class="border-b border-surface-3">
          <th class="text-left py-3 px-4 text-xs font-semibold text-text-muted uppercase tracking-wider w-16">
            #
          </th>
          <th class="text-left py-3 px-4 text-xs font-semibold text-text-muted uppercase tracking-wider">
            Spieler
          </th>
          <th class="text-right py-3 px-4 text-xs font-semibold text-text-muted uppercase tracking-wider w-28">
            Punkte
          </th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="entry in entries"
          :key="entry.rank"
          class="border-b border-surface-3/50 transition-colors"
          :class="entry.alias === currentUserAlias ? 'bg-primary-muted/10' : 'hover:bg-surface-2/50'"
        >
          <td class="py-3 px-4">
            <span
              v-if="entry.rank <= 3"
              class="inline-flex items-center justify-center w-7 h-7 rounded-full text-sm font-bold"
              :class="{
                'bg-yellow-500/20 text-yellow-400': entry.rank === 1,
                'bg-gray-400/20 text-gray-300': entry.rank === 2,
                'bg-amber-700/20 text-amber-500': entry.rank === 3,
              }"
            >
              {{ entry.rank }}
            </span>
            <span v-else class="text-text-muted pl-1.5">
              {{ entry.rank }}
            </span>
          </td>
          <td class="py-3 px-4">
            <span
              class="font-medium"
              :class="entry.alias === currentUserAlias ? 'text-primary' : 'text-text-primary'"
            >
              {{ entry.alias }}
            </span>
            <span
              v-if="entry.is_bot"
              class="ml-1.5 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-primary/10 text-primary"
              title="Automatischer Q-Bot Tipper"
            >
              BOT
            </span>
            <span
              v-if="entry.alias === currentUserAlias"
              class="ml-2 text-xs text-primary"
            >
              (Du)
            </span>
          </td>
          <td class="py-3 px-4 text-right">
            <span class="font-mono font-bold text-text-primary">
              {{ entry.points.toFixed(1) }}
            </span>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
