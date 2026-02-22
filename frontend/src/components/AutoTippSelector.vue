<script setup lang="ts">
import { useSpieltagStore } from "@/stores/spieltag";

const spieltag = useSpieltagStore();

const options = [
  {
    value: "none",
    label: "Kein Auto-Tipp",
    desc: "Nur manuell getippte Spiele werden gewertet.",
  },
  {
    value: "draw",
    label: "1:1 (Unentschieden)",
    desc: "Nicht getippte Spiele werden automatisch mit 1:1 getippt.",
  },
  {
    value: "favorite",
    label: "Quoten-Favorit",
    desc: "Nicht getippte Spiele werden auf den Quoten-Favoriten gesetzt. Bei gleichen Quoten wird Unentschieden angenommen.",
  },
];
</script>

<template>
  <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
    <h3 id="auto-tipp-label" class="text-sm font-semibold text-text-primary mb-2">Auto-Tipp</h3>
    <p class="text-xs text-text-muted mb-3">
      Wird für nicht getippte Spiele nach Abschluss des Spieltags angewendet.
    </p>
    <div role="radiogroup" aria-labelledby="auto-tipp-label" class="space-y-2">
      <label
        v-for="opt in options"
        :key="opt.value"
        :for="`auto-tipp-${opt.value}`"
        class="flex items-start gap-3 p-3 rounded-lg cursor-pointer transition-all border border-transparent"
        :class="
          spieltag.draftAutoStrategy === opt.value
            ? 'bg-primary-muted/10 border-primary/20'
            : 'hover:bg-surface-2'
        "
      >
        <input
          :id="`auto-tipp-${opt.value}`"
          type="radio"
          name="auto-tipp"
          :value="opt.value"
          :checked="spieltag.draftAutoStrategy === opt.value"
          class="mt-1 h-4 w-4 accent-primary"
          @change="spieltag.draftAutoStrategy = opt.value"
        />
        <div class="select-none">
          <span class="text-sm font-medium text-text-primary">{{ opt.label }}</span>
          <p class="text-xs text-text-muted">{{ opt.desc }}</p>
        </div>
      </label>
    </div>
  </div>
</template>
