<script setup lang="ts">
import { ref } from "vue";
import { useWalletStore } from "@/stores/wallet";

const emit = defineEmits<{
  accepted: [];
}>();

const walletStore = useWalletStore();
const accepting = ref(false);

async function accept() {
  accepting.value = true;
  try {
    await walletStore.acceptDisclaimer();
    emit("accepted");
  } finally {
    accepting.value = false;
  }
}
</script>

<template>
  <div class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
    <div class="bg-surface-0 rounded-xl max-w-md w-full p-6 shadow-xl space-y-4">
      <h2 class="text-lg font-bold text-text-primary">{{ $t('wallet.virtualCurrency') }}</h2>

      <p class="text-sm text-text-secondary leading-relaxed">
        {{ $t('wallet.disclaimer') }}
        {{ $t('wallet.entertainmentOnly') }}
        {{ $t('wallet.playResponsibly') }}
      </p>

      <div class="bg-surface-1 rounded-lg p-3 border border-surface-3/50">
        <p class="text-xs text-text-muted">
          {{ $t('wallet.acceptDisclaimer') }}
        </p>
      </div>

      <button
        class="w-full py-3 rounded-lg bg-primary text-surface-0 font-semibold text-sm hover:bg-primary-hover transition-colors disabled:opacity-50"
        :disabled="accepting"
        @click="accept"
      >
        {{ accepting ? $t('profile.saving') : $t('wallet.acceptButton') }}
      </button>
    </div>
  </div>
</template>
