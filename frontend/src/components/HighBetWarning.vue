<script setup lang="ts">
defineProps<{
  stake: number;
  balance: number;
}>();

const emit = defineEmits<{
  confirm: [];
  cancel: [];
}>();
</script>

<template>
  <div class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
    <div class="bg-surface-0 rounded-xl max-w-sm w-full p-6 shadow-xl space-y-4">
      <h2 class="text-lg font-bold text-text-primary">Hoher Einsatz</h2>

      <p class="text-sm text-text-secondary">
        Du setzt
        <span class="font-bold text-text-primary">{{ stake }}</span>
        von
        <span class="font-bold text-text-primary">{{ Math.round(balance) }}</span>
        Coins ({{ Math.round((stake / balance) * 100) }}%).
      </p>

      <p class="text-sm text-amber-600">
        Bitte spiele verantwortungsbewusst.
      </p>

      <div class="flex gap-3">
        <button
          class="flex-1 py-2.5 rounded-lg bg-surface-2 text-text-secondary text-sm font-medium hover:bg-surface-3 transition-colors"
          @click="emit('cancel')"
        >
          Abbrechen
        </button>
        <button
          class="flex-1 py-2.5 rounded-lg bg-primary text-surface-0 text-sm font-semibold hover:bg-primary-hover transition-colors"
          @click="emit('confirm')"
        >
          Trotzdem setzen
        </button>
      </div>
    </div>
  </div>
</template>
