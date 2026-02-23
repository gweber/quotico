<script setup lang="ts">
import { ref, computed } from "vue";
import { useRouter } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { useToast } from "@/composables/useToast";

const router = useRouter();
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
  if (age < 18) return "Du musst mindestens 18 Jahre alt sein (Jugendschutz).";
  return "";
});

async function handleComplete() {
  errorMessage.value = "";

  if (!birthDate.value) {
    errorMessage.value = "Bitte Geburtsdatum angeben.";
    return;
  }

  if (ageError.value) {
    errorMessage.value = ageError.value;
    return;
  }

  if (!disclaimerAccepted.value) {
    errorMessage.value = "Bitte bestätige den Haftungsausschluss.";
    return;
  }

  loading.value = true;
  try {
    await auth.completeProfile(birthDate.value, disclaimerAccepted.value);
    toast.success("Profil vervollständigt! Willkommen bei Quotico.");
    // Check for pending invite link from before auth flow
    const pendingInvite = localStorage.getItem("pendingInvite");
    router.push(pendingInvite ? `/join/${pendingInvite}` : "/");
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "Fehler beim Vervollständigen.";
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
        <h1 class="text-2xl font-bold text-text-primary">Profil vervollständigen</h1>
        <p class="text-sm text-text-secondary mt-2">
          Bitte bestätige dein Alter, um Quotico.de nutzen zu können.
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
            Geburtsdatum
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
              Ich bestätige, dass ich mindestens 18 Jahre alt bin und akzeptiere die
              <RouterLink to="/legal/agb" class="text-secondary underline">AGB</RouterLink>
              sowie die
              <RouterLink to="/legal/datenschutz" class="text-secondary underline">Datenschutzerklärung</RouterLink>.
              Quotico.de ist kein Echtgeld-Glücksspiel.
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
              Wird gespeichert...
            </span>
          </template>
          <template v-else>
            Bestätigen und weiter
          </template>
        </button>
      </form>

      <p class="text-center text-xs text-text-muted mt-6">
        Ab 18 Jahren. Kein echtes Geld. Quotico dient nur der Unterhaltung.
      </p>
    </div>
  </div>
</template>
