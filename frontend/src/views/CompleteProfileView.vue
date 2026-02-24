<script setup lang="ts">
import { ref, computed } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useAuthStore } from "@/stores/auth";
import { useToast } from "@/composables/useToast";

const router = useRouter();
const { t } = useI18n();
const auth = useAuthStore();
const toast = useToast();

const birthDate = ref("");
const disclaimerAccepted = ref(false);
const loading = ref(false);
const errorMessage = ref("");

const ageError = computed(() => {
  if (!birthDate.value) return "";
  const birth = new Date(birthDate.value);
  const today = new Date();
  let age = today.getFullYear() - birth.getFullYear();
  const monthDiff = today.getMonth() - birth.getMonth();
  if (monthDiff < 0 || (monthDiff === 0 && today.getDate() < birth.getDate())) {
    age--;
  }
  if (age < 18) return t('auth.ageError');
  return "";
});

async function handleComplete() {
  errorMessage.value = "";

  if (!birthDate.value) {
    errorMessage.value = t('profile.enterBirthDate');
    return;
  }

  if (ageError.value) {
    errorMessage.value = ageError.value;
    return;
  }

  if (!disclaimerAccepted.value) {
    errorMessage.value = t('profile.disclaimerRequired');
    return;
  }

  loading.value = true;
  try {
    await auth.completeProfile(birthDate.value, disclaimerAccepted.value);
    toast.success(t('profile.completedSuccess'));
    // Check for pending invite link from before auth flow
    const pendingInvite = localStorage.getItem("pendingInvite");
    router.push(pendingInvite ? `/join/${pendingInvite}` : "/");
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : t('profile.completedError');
    errorMessage.value = msg;
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="min-h-screen bg-surface-0 flex items-center justify-center px-4">
    <div class="bg-surface-1 rounded-card w-full max-w-md p-8 shadow-xl">
      <div class="text-center mb-8">
        <h1 class="text-2xl font-bold text-text-primary">{{ $t('profile.completeHeading') }}</h1>
        <p class="text-sm text-text-secondary mt-2">
          {{ $t('profile.completeInstruction') }}
        </p>
      </div>

      <form @submit.prevent="handleComplete" novalidate>
        <div
          v-if="errorMessage"
          class="bg-danger-muted/20 border border-danger/30 rounded-lg px-4 py-3 mb-6"
          role="alert"
        >
          <p class="text-sm text-danger">{{ errorMessage }}</p>
        </div>

        <!-- Birth Date -->
        <div class="mb-4">
          <label for="complete-birthdate" class="block text-sm font-medium text-text-secondary mb-1.5">
            {{ $t('auth.birthDate') }}
          </label>
          <input
            id="complete-birthdate"
            v-model="birthDate"
            type="date"
            required
            class="w-full px-4 py-3 bg-surface-2 border border-surface-3 rounded-lg text-text-primary text-sm transition-colors focus:border-primary focus:ring-1 focus:ring-primary"
            :class="{ 'border-danger': ageError }"
          />
          <p v-if="ageError" class="text-xs text-danger mt-1">{{ ageError }}</p>
        </div>

        <!-- Disclaimer -->
        <div class="mb-6">
          <label class="flex items-start gap-3 cursor-pointer">
            <input
              v-model="disclaimerAccepted"
              type="checkbox"
              class="mt-0.5 w-4 h-4 rounded border-surface-3 bg-surface-2 text-primary focus:ring-primary focus:ring-1"
            />
            <span class="text-xs text-text-secondary leading-relaxed">
              {{ $t('profile.ageConfirm') }}
              <RouterLink to="/legal/agb" class="text-secondary underline">{{ $t('legal.agb') }}</RouterLink>
              sowie die
              <RouterLink to="/legal/datenschutz" class="text-secondary underline">{{ $t('legal.datenschutz') }}</RouterLink>.
              {{ $t('profile.noRealMoney') }}
            </span>
          </label>
        </div>

        <button
          type="submit"
          :disabled="loading"
          class="w-full py-3 rounded-lg bg-primary text-surface-0 font-semibold text-sm hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <template v-if="loading">
            <span class="inline-flex items-center gap-2">
              <svg class="animate-spin h-4 w-4" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none" />
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              {{ $t('profile.saving') }}
            </span>
          </template>
          <template v-else>
            {{ $t('profile.confirmContinue') }}
          </template>
        </button>
      </form>

      <p class="text-center text-xs text-text-muted mt-6">
        {{ $t('profile.legalDisclaimer') }}
      </p>
    </div>
  </div>
</template>
