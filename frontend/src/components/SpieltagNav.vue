<script setup lang="ts">
import { computed } from "vue";
import type { Matchday } from "@/stores/spieltag";

const props = defineProps<{
  matchdays: Matchday[];
  currentId: string | null;
}>();

const emit = defineEmits<{
  select: [id: string];
}>();

const statusIcon = (md: Matchday) => {
  if (md.all_resolved) return "\u2705";
  if (md.status === "in_progress") return "\u26BD";
  return "";
};

// Show a window of matchdays around the current one
const visibleMatchdays = computed(() => {
  if (props.matchdays.length <= 10) return props.matchdays;

  const currentIdx = props.matchdays.findIndex(
    (md) => md.id === props.currentId
  );
  const center = currentIdx >= 0 ? currentIdx : 0;
  const start = Math.max(0, center - 4);
  const end = Math.min(props.matchdays.length, start + 10);
  return props.matchdays.slice(start, end);
});
</script>

<template>
  <div class="flex items-center gap-1 overflow-x-auto pb-1 scrollbar-hide">
    <button
      v-for="md in visibleMatchdays"
      :key="md.id"
      class="shrink-0 flex items-center gap-1 px-3 py-1.5 rounded-full text-sm font-medium transition-colors"
      :class="
        md.id === currentId
          ? 'bg-primary text-surface-0'
          : 'bg-surface-2 text-text-secondary hover:bg-surface-3 hover:text-text-primary'
      "
      @click="emit('select', md.id)"
    >
      <span>{{ md.matchday_number }}</span>
      <span v-if="statusIcon(md)" class="text-xs">{{ statusIcon(md) }}</span>
    </button>
  </div>
</template>

<style scoped>
.scrollbar-hide::-webkit-scrollbar {
  display: none;
}
.scrollbar-hide {
  -ms-overflow-style: none;
  scrollbar-width: none;
}
</style>
