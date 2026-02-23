<script setup lang="ts">
defineProps<{
  points: number;
}>();

const colorMap: Record<number, string> = {
  3: "bg-emerald-500 text-white",
  2: "bg-blue-500 text-white",
  1: "bg-amber-500 text-white",
  0: "bg-surface-3 text-text-muted",
};
</script>

<template>
  <span
    class="inline-flex items-center justify-center w-6 h-6 rounded-full text-xs font-bold"
    :class="colorMap[points] || 'bg-surface-3 text-text-muted'"
    :title="`${points} Punkt${points !== 1 ? 'e' : ''}`"
  >
    {{ points }}
  </span>
</template>
