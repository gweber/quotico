<script setup lang="ts">
import { ref } from "vue";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

const props = defineProps<{
  squadId: string;
  currentMode: string;
  isAdmin: boolean;
}>();

const emit = defineEmits<{
  updated: [mode: string];
}>();

const api = useApi();
const toast = useToast();
const saving = ref(false);

const modes = [
  { value: "classic", label: "Classic", description: "Spieltag-Tipps mit 0-3 Punkten" },
  { value: "bankroll", label: "Bankroll", description: "1000 virtuelle Coins, Wetten mit Quoten" },
  { value: "survivor", label: "Survivor", description: "Wähle ein Team pro Spieltag, eliminiert bei Verlust" },
  { value: "over_under", label: "Über/Unter", description: "Tippe ob mehr oder weniger als 2.5 Tore fallen" },
  { value: "fantasy", label: "Fantasy", description: "Wähle ein Team, Punkte basierend auf Performance" },
];

async function setMode(mode: string) {
  if (!props.isAdmin || mode === props.currentMode) return;
  saving.value = true;
  try {
    await api.post(`/squads/${props.squadId}/game-mode`, { game_mode: mode });
    toast.success(`Modus auf "${modes.find(m => m.value === mode)?.label}" geändert!`);
    emit("updated", mode);
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : "Fehler.");
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="space-y-2">
    <h3 class="text-sm font-semibold text-text-primary">Spielmodus</h3>
    <div class="grid gap-2">
      <button
        v-for="mode in modes"
        :key="mode.value"
        class="text-left p-3 rounded-lg border transition-colors"
        :class="
          currentMode === mode.value
            ? 'border-primary bg-primary/5'
            : isAdmin
              ? 'border-surface-3 hover:border-primary/50 bg-surface-1'
              : 'border-surface-3 bg-surface-1 opacity-60 cursor-default'
        "
        :disabled="!isAdmin || saving"
        @click="setMode(mode.value)"
      >
        <div class="text-sm font-medium text-text-primary">
          {{ mode.label }}
          <span v-if="currentMode === mode.value" class="text-primary ml-1 text-xs">(aktiv)</span>
        </div>
        <div class="text-xs text-text-muted mt-0.5">{{ mode.description }}</div>
      </button>
    </div>
  </div>
</template>
