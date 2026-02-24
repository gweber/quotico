<script setup lang="ts">
import { ref } from "vue";
import { useI18n } from "vue-i18n";
import { useBetSlipStore } from "@/stores/betslip";
import { useAuthStore } from "@/stores/auth";
import { useToast } from "@/composables/useToast";
import { useRouter } from "vue-router";
import { cacheUserBet } from "@/composables/useUserBets";

const { t } = useI18n();
const betslip = useBetSlipStore();
const auth = useAuthStore();
const toast = useToast();
const router = useRouter();
const submitting = ref(false);

async function handleSubmit() {
  if (!auth.isLoggedIn) {
    toast.warning(t("auth.loginRequired"));
    router.push("/login");
    return;
  }

  submitting.value = true;
  const { success, errors, bets } = await betslip.submitAll();
  submitting.value = false;

  // If odds change dialog is showing, don't show toast errors
  if (betslip.showOddsChangeDialog) return;

  // Cache returned bets so MatchCard updates immediately
  for (const bet of bets) {
    cacheUserBet(bet);
  }

  if (success.length > 0) {
    toast.success(
      success.length === 1
        ? t("betslip.successSingle")
        : t("betslip.successMultiple", { count: success.length })
    );
  }
  for (const err of errors) {
    toast.error(err);
  }

  if (errors.length === 0) {
    betslip.isOpen = false;
  }
}

async function handleAcceptAndSubmit() {
  betslip.acceptOddsChanges();
  await handleSubmit();
}

function closeMobile() {
  betslip.isOpen = false;
}

function isStale(addedAt: number | undefined): boolean {
  return !!addedAt && (Date.now() - addedAt) > 10 * 60 * 1000;
}
</script>

<template>
  <!-- Desktop: Sidebar -->
  <aside
    class="hidden lg:block w-72 shrink-0"
    :aria-label="$t('betslip.title')"
  >
    <div class="sticky top-[3.75rem] p-3">
      <div class="bg-surface-2 rounded-card p-4">
        <h2 class="text-sm font-semibold text-text-primary mb-3 flex items-center justify-between">
          <span>{{ $t('betslip.title') }}</span>
          <span
            v-if="betslip.itemCount > 0"
            class="bg-primary text-surface-0 text-xs font-bold px-2 py-0.5 rounded-full"
          >
            {{ betslip.itemCount }}
          </span>
        </h2>

        <!-- Empty state -->
        <div v-if="betslip.itemCount === 0" class="text-center py-6">
          <p class="text-text-muted text-sm">{{ $t('betslip.empty') }}</p>
          <p class="text-text-muted text-xs mt-1">{{ $t('betslip.addInstruction') }}</p>
        </div>

        <!-- Items -->
        <div v-else class="space-y-2">
          <!-- Odds change confirmation dialog -->
          <div
            v-if="betslip.showOddsChangeDialog"
            class="bg-warning/10 border border-warning/30 rounded-lg p-3 space-y-2"
          >
            <p class="text-xs font-semibold text-warning">{{ $t('betslip.oddsChanged') }}</p>
            <div
              v-for="change in betslip.pendingOddsChanges"
              :key="change.matchId"
              class="flex items-center justify-between text-xs"
            >
              <span class="text-text-secondary truncate">{{ change.prediction }}</span>
              <span class="font-mono">
                <span class="text-text-muted line-through">{{ change.oldOdds.toFixed(2) }}</span>
                <span class="mx-1 text-text-muted">&rarr;</span>
                <span class="font-bold" :class="change.newOdds > change.oldOdds ? 'text-success' : 'text-danger'">
                  {{ change.newOdds.toFixed(2) }}
                </span>
              </span>
            </div>
            <div class="flex gap-2 mt-2">
              <button
                class="flex-1 py-2 rounded-lg bg-primary text-surface-0 text-xs font-semibold hover:bg-primary-hover transition-colors"
                @click="handleAcceptAndSubmit"
              >
                {{ $t('betslip.accept') }}
              </button>
              <button
                class="flex-1 py-2 rounded-lg border border-surface-3 text-xs text-text-muted hover:text-text-primary transition-colors"
                @click="betslip.dismissOddsChanges()"
              >
                {{ $t('common.cancel') }}
              </button>
            </div>
          </div>

          <!-- Expired items -->
          <div
            v-for="item in betslip.expiredItems"
            :key="item.matchId"
            class="bg-surface-1 rounded-lg p-3 relative group opacity-50 border border-danger/30"
          >
            <button
              class="absolute top-2 right-2 w-6 h-6 flex items-center justify-center rounded text-text-muted hover:text-danger hover:bg-danger-muted/20 transition-colors opacity-0 group-hover:opacity-100"
              :aria-label="t('betslip.removeBet', { teams: item.homeTeam + ' vs ' + item.awayTeam })"
              @click="betslip.removeItem(item.matchId)"
            >
              <span aria-hidden="true">&times;</span>
            </button>
            <p class="text-xs text-text-secondary truncate pr-6 line-through">
              {{ item.homeTeam }} vs {{ item.awayTeam }}
            </p>
            <div class="flex items-center justify-between mt-1.5">
              <span class="text-xs text-danger font-medium">{{ $t('betslip.expired') }}</span>
              <span class="text-sm font-mono text-text-muted line-through">{{ item.odds.toFixed(2) }}</span>
            </div>
          </div>

          <!-- Remove expired button -->
          <button
            v-if="betslip.expiredItems.length > 0"
            class="w-full py-1.5 rounded-lg text-xs text-danger hover:bg-danger-muted/10 transition-colors"
            @click="betslip.removeExpired()"
          >
            {{ t('betslip.removeExpired', { count: betslip.expiredItems.length }) }}
          </button>

          <!-- Valid items -->
          <div
            v-for="item in betslip.validItems"
            :key="item.matchId"
            class="bg-surface-1 rounded-lg p-3 relative group"
            :class="isStale(item.addedAt) ? 'border border-warning/20' : ''"
          >
            <button
              class="absolute top-2 right-2 w-6 h-6 flex items-center justify-center rounded text-text-muted hover:text-danger hover:bg-danger-muted/20 transition-colors opacity-0 group-hover:opacity-100"
              :aria-label="t('betslip.removeBet', { teams: item.homeTeam + ' vs ' + item.awayTeam })"
              @click="betslip.removeItem(item.matchId)"
            >
              <span aria-hidden="true">&times;</span>
            </button>
            <p class="text-xs text-text-secondary truncate pr-6">
              {{ item.homeTeam }} vs {{ item.awayTeam }}
            </p>
            <div class="flex items-center justify-between mt-1.5">
              <span class="text-sm text-text-primary font-medium">{{ item.predictionLabel }}</span>
              <span class="text-sm font-mono text-primary font-bold">{{ item.odds.toFixed(2) }}</span>
            </div>
            <p v-if="isStale(item.addedAt)" class="text-[10px] text-warning mt-1">{{ $t('betslip.oddsStale') }}</p>
          </div>

          <!-- Total -->
          <div class="border-t border-surface-3 pt-3 mt-3 flex items-center justify-between">
            <span class="text-sm text-text-secondary">{{ $t('betslip.totalOdds') }}</span>
            <span class="text-lg font-mono text-primary font-bold">
              {{ betslip.totalOdds.toFixed(2) }}
            </span>
          </div>

          <!-- Submit -->
          <button
            class="w-full py-3 rounded-lg bg-primary text-surface-0 font-semibold text-sm hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed mt-2"
            :disabled="submitting || betslip.validItems.length === 0"
            @click="handleSubmit"
          >
            <template v-if="submitting">
              <span class="inline-flex items-center gap-2">
                <svg class="animate-spin h-4 w-4" viewBox="0 0 24 24">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none" />
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                {{ $t('betslip.submitting') }}
              </span>
            </template>
            <template v-else>
              {{ $t('betslip.submit') }}
            </template>
          </button>

          <!-- Clear -->
          <button
            class="w-full py-2 rounded-lg text-sm text-text-muted hover:text-danger hover:bg-danger-muted/10 transition-colors"
            @click="betslip.clear()"
          >
            {{ $t('betslip.clearAll') }}
          </button>
        </div>
      </div>
    </div>
  </aside>

  <!-- Mobile: Bottom sheet -->
  <Teleport to="body">
    <Transition name="sheet">
      <div
        v-if="betslip.isOpen && betslip.itemCount > 0"
        class="lg:hidden fixed inset-0 z-50"
      >
        <!-- Backdrop -->
        <div
          class="absolute inset-0 bg-black/60"
          @click="closeMobile"
        />

        <!-- Sheet -->
        <div
          class="absolute bottom-0 left-0 right-0 bg-surface-1 rounded-t-2xl max-h-[80vh] overflow-y-auto"
          role="dialog"
          :aria-label="$t('betslip.title')"
        >
          <!-- Handle -->
          <div class="flex justify-center pt-3 pb-1">
            <div class="w-10 h-1 bg-surface-3 rounded-full" />
          </div>

          <div class="px-4 pb-6">
            <div class="flex items-center justify-between mb-4">
              <h2 class="text-lg font-semibold text-text-primary">
                {{ $t('betslip.title') }}
                <span
                  class="ml-2 bg-primary text-surface-0 text-xs font-bold px-2 py-0.5 rounded-full"
                >
                  {{ betslip.itemCount }}
                </span>
              </h2>
              <button
                class="w-touch h-touch flex items-center justify-center rounded-lg text-text-muted hover:text-text-primary transition-colors"
                :aria-label="$t('betslip.close')"
                @click="closeMobile"
              >
                <span aria-hidden="true" class="text-xl">&times;</span>
              </button>
            </div>

            <!-- Items -->
            <div class="space-y-2">
              <!-- Odds change confirmation dialog (mobile) -->
              <div
                v-if="betslip.showOddsChangeDialog"
                class="bg-warning/10 border border-warning/30 rounded-lg p-3 space-y-2"
              >
                <p class="text-xs font-semibold text-warning">{{ $t('betslip.oddsChanged') }}</p>
                <div
                  v-for="change in betslip.pendingOddsChanges"
                  :key="change.matchId"
                  class="flex items-center justify-between text-xs"
                >
                  <span class="text-text-secondary truncate">{{ change.prediction }}</span>
                  <span class="font-mono">
                    <span class="text-text-muted line-through">{{ change.oldOdds.toFixed(2) }}</span>
                    <span class="mx-1 text-text-muted">&rarr;</span>
                    <span class="font-bold" :class="change.newOdds > change.oldOdds ? 'text-success' : 'text-danger'">
                      {{ change.newOdds.toFixed(2) }}
                    </span>
                  </span>
                </div>
                <div class="flex gap-2 mt-2">
                  <button
                    class="flex-1 py-2.5 rounded-lg bg-primary text-surface-0 text-xs font-semibold hover:bg-primary-hover transition-colors"
                    @click="handleAcceptAndSubmit"
                  >
                    {{ $t('betslip.accept') }}
                  </button>
                  <button
                    class="flex-1 py-2.5 rounded-lg border border-surface-3 text-xs text-text-muted hover:text-text-primary transition-colors"
                    @click="betslip.dismissOddsChanges()"
                  >
                    {{ $t('common.cancel') }}
                  </button>
                </div>
              </div>

              <!-- Expired items -->
              <div
                v-for="item in betslip.expiredItems"
                :key="item.matchId"
                class="bg-surface-2 rounded-lg p-3 flex items-center justify-between opacity-50 border border-danger/30"
              >
                <div class="min-w-0 flex-1">
                  <p class="text-xs text-text-secondary truncate line-through">
                    {{ item.homeTeam }} vs {{ item.awayTeam }}
                  </p>
                  <p class="text-xs text-danger font-medium mt-0.5">
                    {{ $t('betslip.expired') }}
                  </p>
                </div>
                <div class="flex items-center gap-3 shrink-0 ml-3">
                  <span class="text-sm font-mono text-text-muted line-through">{{ item.odds.toFixed(2) }}</span>
                  <button
                    class="w-touch h-touch flex items-center justify-center rounded-lg text-text-muted hover:text-danger transition-colors"
                    :aria-label="t('betslip.removeBet', { teams: item.homeTeam + ' vs ' + item.awayTeam })"
                    @click="betslip.removeItem(item.matchId)"
                  >
                    <span aria-hidden="true">&times;</span>
                  </button>
                </div>
              </div>

              <!-- Remove expired button (mobile) -->
              <button
                v-if="betslip.expiredItems.length > 0"
                class="w-full py-2 rounded-lg text-xs text-danger hover:bg-danger-muted/10 transition-colors"
                @click="betslip.removeExpired()"
              >
                {{ t('betslip.removeExpired', { count: betslip.expiredItems.length }) }}
              </button>

              <!-- Valid items -->
              <div
                v-for="item in betslip.validItems"
                :key="item.matchId"
                class="bg-surface-2 rounded-lg p-3 flex items-center justify-between"
                :class="isStale(item.addedAt) ? 'border border-warning/20' : ''"
              >
                <div class="min-w-0 flex-1">
                  <p class="text-xs text-text-secondary truncate">
                    {{ item.homeTeam }} vs {{ item.awayTeam }}
                  </p>
                  <p class="text-sm text-text-primary font-medium mt-0.5">
                    {{ item.predictionLabel }}
                  </p>
                  <p v-if="isStale(item.addedAt)" class="text-[10px] text-warning mt-0.5">{{ $t('betslip.oddsStale') }}</p>
                </div>
                <div class="flex items-center gap-3 shrink-0 ml-3">
                  <span class="text-sm font-mono text-primary font-bold">{{ item.odds.toFixed(2) }}</span>
                  <button
                    class="w-touch h-touch flex items-center justify-center rounded-lg text-text-muted hover:text-danger transition-colors"
                    :aria-label="t('betslip.removeBet', { teams: item.homeTeam + ' vs ' + item.awayTeam })"
                    @click="betslip.removeItem(item.matchId)"
                  >
                    <span aria-hidden="true">&times;</span>
                  </button>
                </div>
              </div>
            </div>

            <!-- Total + Submit -->
            <div class="border-t border-surface-3 pt-4 mt-4">
              <div class="flex items-center justify-between mb-4">
                <span class="text-sm text-text-secondary">{{ $t('betslip.totalOdds') }}</span>
                <span class="text-xl font-mono text-primary font-bold">
                  {{ betslip.totalOdds.toFixed(2) }}
                </span>
              </div>
              <button
                class="w-full py-3.5 rounded-lg bg-primary text-surface-0 font-semibold hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                :disabled="submitting || betslip.validItems.length === 0"
                @click="handleSubmit"
              >
                <template v-if="submitting">
                  <span class="inline-flex items-center gap-2">
                    <svg class="animate-spin h-4 w-4" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none" />
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                    </svg>
                    {{ $t('betslip.submitting') }}
                  </span>
                </template>
                <template v-else>
                  {{ $t('betslip.submit') }}
                </template>
              </button>
              <button
                class="w-full py-2 mt-2 rounded-lg text-sm text-text-muted hover:text-danger transition-colors"
                @click="betslip.clear()"
              >
                {{ $t('betslip.clearAll') }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.sheet-enter-active {
  transition: all 0.3s ease-out;
}
.sheet-leave-active {
  transition: all 0.2s ease-in;
}
.sheet-enter-from > :first-child,
.sheet-leave-to > :first-child {
  opacity: 0;
}
.sheet-enter-from > :last-child,
.sheet-leave-to > :last-child {
  transform: translateY(100%);
}
</style>
