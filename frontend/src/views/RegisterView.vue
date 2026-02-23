<script setup lang="ts">
import { ref, computed } from "vue";
import { useRouter, useRoute } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { useToast } from "@/composables/useToast";

const router = useRouter();
const route = useRoute();
const auth = useAuthStore();
const toast = useToast();

const redirectTarget = computed(() => {
  const r = route.query.redirect as string | undefined;
  return r && r.startsWith("/") ? r : "/";
});

const email = ref("");
const password = ref("");
const passwordConfirm = ref("");
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

async function handleRegister() {
  errorMessage.value = "";

  if (!email.value || !password.value || !birthDate.value) {
    errorMessage.value = "Bitte alle Felder ausfüllen.";
    return;
  }

  if (password.value.length < 10) {
    errorMessage.value = "Das Passwort muss mindestens 10 Zeichen lang sein.";
    return;
  }

  if (password.value !== passwordConfirm.value) {
    errorMessage.value = "Die Passwörter stimmen nicht überein.";
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
    await auth.register(email.value, password.value, birthDate.value, disclaimerAccepted.value);
    toast.success("Registrierung erfolgreich! Willkommen bei Quotico.");
    router.push(redirectTarget.value);
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "Registrierung fehlgeschlagen.";
    errorMessage.value = msg;
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="min-h-screen bg-surface-0 flex items-center justify-center px-4">
    <div class="bg-surface-1 rounded-card w-full max-w-md p-8 shadow-xl">
      <!-- Header -->
      <div class="text-center mb-8">
        <h1 class="text-2xl font-bold text-text-primary">Registrieren</h1>
        <p class="text-sm text-text-secondary mt-2">
          Erstelle dein Konto und starte mit Tipps.
        </p>
      </div>

      <form @submit.prevent="handleRegister" novalidate>
        <!-- Error banner -->
        <div
          v-if="errorMessage"
          class="bg-danger-muted/20 border border-danger/30 rounded-lg px-4 py-3 mb-6"
          role="alert"
        >
          <p class="text-sm text-danger">{{ errorMessage }}</p>
        </div>

        <!-- Email -->
        <div class="mb-4">
          <label for="register-email" class="block text-sm font-medium text-text-secondary mb-1.5">
            E-Mail-Adresse
          </label>
          <input
            id="register-email"
            v-model="email"
            type="email"
            autocomplete="email"
            required
            class="w-full px-4 py-3 bg-surface-2 border border-surface-3 rounded-lg text-text-primary placeholder-text-muted text-sm transition-colors focus:border-primary focus:ring-1 focus:ring-primary"
            placeholder="deine@email.de"
          />
        </div>

        <!-- Password -->
        <div class="mb-4">
          <label for="register-password" class="block text-sm font-medium text-text-secondary mb-1.5">
            Passwort
          </label>
          <input
            id="register-password"
            v-model="password"
            type="password"
            autocomplete="new-password"
            required
            minlength="10"
            class="w-full px-4 py-3 bg-surface-2 border border-surface-3 rounded-lg text-text-primary placeholder-text-muted text-sm transition-colors focus:border-primary focus:ring-1 focus:ring-primary"
            placeholder="Mind. 10 Zeichen, Großbuchstabe + Ziffer"
          />
        </div>

        <!-- Confirm Password -->
        <div class="mb-4">
          <label for="register-password-confirm" class="block text-sm font-medium text-text-secondary mb-1.5">
            Passwort bestätigen
          </label>
          <input
            id="register-password-confirm"
            v-model="passwordConfirm"
            type="password"
            autocomplete="new-password"
            required
            class="w-full px-4 py-3 bg-surface-2 border border-surface-3 rounded-lg text-text-primary placeholder-text-muted text-sm transition-colors focus:border-primary focus:ring-1 focus:ring-primary"
            placeholder="Passwort wiederholen"
          />
        </div>

        <!-- Birth Date (Age Gate) -->
        <div class="mb-4">
          <label for="register-birthdate" class="block text-sm font-medium text-text-secondary mb-1.5">
            Geburtsdatum
          </label>
          <input
            id="register-birthdate"
            v-model="birthDate"
            type="date"
            required
            class="w-full px-4 py-3 bg-surface-2 border border-surface-3 rounded-lg text-text-primary text-sm transition-colors focus:border-primary focus:ring-1 focus:ring-primary"
            :class="{ 'border-danger': ageError }"
          />
          <p v-if="ageError" class="text-xs text-danger mt-1">{{ ageError }}</p>
        </div>

        <!-- Disclaimer Checkbox -->
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

        <!-- Submit -->
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
              Wird registriert...
            </span>
          </template>
          <template v-else>
            Konto erstellen
          </template>
        </button>
      </form>

      <!-- Divider -->
      <div class="flex items-center my-6">
        <div class="flex-1 border-t border-surface-3"></div>
        <span class="px-3 text-xs text-text-muted">oder</span>
        <div class="flex-1 border-t border-surface-3"></div>
      </div>

      <!-- Google register -->
      <a
        href="/api/auth/google"
        class="w-full flex items-center justify-center gap-3 py-3 rounded-lg border border-surface-3 bg-surface-2 text-text-primary text-sm font-medium hover:bg-surface-3 transition-colors"
      >
        <svg class="w-5 h-5" viewBox="0 0 24 24">
          <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" />
          <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
          <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" />
          <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" />
        </svg>
        Mit Google registrieren
      </a>

      <!-- Login link -->
      <p class="text-center text-sm text-text-secondary mt-6">
        Bereits registriert?
        <RouterLink :to="{ path: '/login', query: route.query.redirect ? { redirect: route.query.redirect } : {} }" class="text-primary hover:text-primary-hover transition-colors font-medium">
          Jetzt anmelden
        </RouterLink>
      </p>

      <!-- Legal notice -->
      <p class="text-center text-xs text-text-muted mt-4">
        Ab 18 Jahren. Tippspiel &mdash; kein Echtgeld. Keine Wette, keine Gewinnauszahlung.
      </p>
    </div>
  </div>
</template>
