<script setup lang="ts">
import { computed } from "vue";
import { useWalletStore } from "@/stores/wallet";

const walletStore = useWalletStore();

const profit = computed(() => {
  if (!walletStore.wallet) return 0;
  return walletStore.wallet.balance - walletStore.wallet.initial_balance;
});

const profitClass = computed(() => {
  if (profit.value > 0) return "text-emerald-500";
  if (profit.value < 0) return "text-red-500";
  return "text-text-muted";
});

const isBankrupt = computed(() => walletStore.wallet?.status === "bankrupt");
</script>

<template>
  <div
    v-if="walletStore.wallet"
    class="flex items-center gap-3 bg-surface-1 rounded-lg px-4 py-2 border border-surface-3/50"
  >
    <div class="flex items-center gap-1.5">
      <span class="text-amber-400 text-lg" aria-hidden="true">C</span>
      <span class="text-lg font-bold text-text-primary">
        {{ Math.round(walletStore.wallet.balance) }}
      </span>
    </div>
    <div class="text-xs" :class="profitClass">
      {{ profit >= 0 ? "+" : "" }}{{ Math.round(profit) }}
    </div>
    <div
      v-if="isBankrupt"
      class="text-xs text-red-400 font-medium bg-red-500/10 px-2 py-0.5 rounded-full"
    >
      Bankrott
    </div>
  </div>
</template>
