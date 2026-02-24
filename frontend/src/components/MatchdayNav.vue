<script setup lang="ts">
import { ref, onMounted, nextTick, watch } from "vue";
import type { Matchday } from "@/stores/matchday";

const props = defineProps<{
  matchdays: Matchday[];
  currentId: string | null;
}>();

const emit = defineEmits<{
  select: [id: string];
}>();

const scrollContainer = ref<HTMLElement | null>(null);

function statusClass(md: Matchday): string {
  if (md.all_resolved) return "border-emerald-500/40";
  if (md.status === "in_progress") return "border-primary/60";
  return "border-transparent";
}

function scrollToActive() {
  if (!scrollContainer.value || !props.currentId) return;
  const el = scrollContainer.value.querySelector(`[data-md-id="${props.currentId}"]`);
  if (el) {
    el.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
  }
}

onMounted(() => nextTick(scrollToActive));
watch(() => props.currentId, () => nextTick(scrollToActive));
</script>

<template>
  <div
    ref="scrollContainer"
    class="flex items-center gap-0.5 overflow-x-auto pb-1 scrollbar-hide"
  >
    <button
      v-for="md in matchdays"
      :key="md.id"
      :data-md-id="md.id"
      class="shrink-0 w-8 h-8 rounded-md text-xs font-semibold transition-colors border-b-2"
      :class="[
        md.id === currentId
          ? 'bg-primary text-surface-0 border-primary'
          : `bg-surface-2 text-text-secondary hover:bg-surface-3 hover:text-text-primary ${statusClass(md)}`,
      ]"
      @click="emit('select', md.id)"
    >
      {{ md.matchday_number }}
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
