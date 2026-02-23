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
  // Only allow local paths (prevent open redirect)
  return r && r.startsWith("/") ? r : "/";
});

const googleAuthUrl = computed(() => {
  const base = "/api/auth/google";
  const r = route.query.redirect as string | undefined;
  return r && r.startsWith("/") ? `${base}?redirect=${encodeURIComponent(r)}` : base;
});

const email = ref("");
const password = ref("");
const totpCode = ref("");
const showTotpInput = ref(false);
const loading = ref(false);
const errorMessage = ref("");

async function handleLogin() {
  errorMessage.value = "";

  if (!email.value || !password.value) {
    errorMessage.value = "Bitte E-Mail und Passwort eingeben.";
    return;
  }

  loading.value = true;
  try {
    if (showTotpInput.value) {
      // 2FA verification step
      await auth.login2fa(email.value, password.value, totpCode.value);
      toast.success("Erfolgreich angemeldet!");
      router.push(redirectTarget.value);
    } else {
      const result = await auth.login(email.value, password.value);
      if (result.requires2fa) {
        showTotpInput.value = true;
        return;
      }
      toast.success("Erfolgreich angemeldet!");
      router.push(redirectTarget.value);
    }
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "Anmeldung fehlgeschlagen.";
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
        <h1 class="text-2xl font-bold text-text-primary">Anmelden</h1>
        <p class="text-sm text-text-secondary mt-2">
          Melde dich an, um deine Tipps abzugeben.
        </p>
      </div>

      <form @submit.prevent="handleLogin" novalidate>
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
          <label for="login-email" class="block text-sm font-medium text-text-secondary mb-1.5">
            E-Mail-Adresse
          </label>
          <input
            id="login-email"
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
          <label for="login-password" class="block text-sm font-medium text-text-secondary mb-1.5">
            Passwort
          </label>
          <input
            id="login-password"
            v-model="password"
            type="password"
            autocomplete="current-password"
            required
            class="w-full px-4 py-3 bg-surface-2 border border-surface-3 rounded-lg text-text-primary placeholder-text-muted text-sm transition-colors focus:border-primary focus:ring-1 focus:ring-primary"
            placeholder="Passwort"
          />
        </div>

        <!-- 2FA Code (conditional) -->
        <Transition name="fade">
          <div v-if="showTotpInput" class="mb-4">
            <label for="login-totp" class="block text-sm font-medium text-text-secondary mb-1.5">
              2FA-Code
            </label>
            <p class="text-xs text-text-muted mb-2">
              Gib den 6-stelligen Code aus deiner Authenticator-App ein.
            </p>
            <input
              id="login-totp"
              v-model="totpCode"
              type="text"
              inputmode="numeric"
              pattern="[0-9]{6}"
              maxlength="6"
              autocomplete="one-time-code"
              required
              class="w-full px-4 py-3 bg-surface-2 border border-surface-3 rounded-lg text-text-primary placeholder-text-muted text-sm font-mono text-center tracking-[0.5em] transition-colors focus:border-primary focus:ring-1 focus:ring-primary"
              placeholder="000000"
            />
          </div>
        </Transition>

        <!-- Submit -->
        <button
          type="submit"
          :disabled="loading"
          class="w-full py-3 rounded-lg bg-primary text-surface-0 font-semibold text-sm hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed mt-2"
        >
          <template v-if="loading">
            <span class="inline-flex items-center gap-2">
              <svg class="animate-spin h-4 w-4" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none" />
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              <span v-if="showTotpInput">Wird verifiziert...</span>
              <span v-else>Wird angemeldet...</span>
            </span>
          </template>
          <template v-else>
            {{ showTotpInput ? "Verifizieren" : "Anmelden" }}
          </template>
        </button>
      </form>

      <!-- Divider -->
      <div class="flex items-center my-6">
        <div class="flex-1 border-t border-surface-3"></div>
        <span class="px-3 text-xs text-text-muted">oder</span>
        <div class="flex-1 border-t border-surface-3"></div>
      </div>

      <!-- Google login -->
      <a
        :href="googleAuthUrl"
        class="w-full flex items-center justify-center gap-3 py-3 rounded-lg border border-surface-3 bg-surface-2 text-text-primary text-sm font-medium hover:bg-surface-3 transition-colors"
      >
        <svg class="w-5 h-5" viewBox="0 0 24 24">
          <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" />
          <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
          <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" />
          <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" />
        </svg>
        Mit Google anmelden
      </a>

      <!-- Register link -->
      <p class="text-center text-sm text-text-secondary mt-6">
        Noch kein Konto?
        <RouterLink :to="{ path: '/register', query: route.query.redirect ? { redirect: route.query.redirect } : {} }" class="text-primary hover:text-primary-hover transition-colors font-medium">
          Jetzt registrieren
        </RouterLink>
      </p>
    </div>
  </div>
</template>

<style scoped>
.fade-enter-active {
  transition: all 0.3s ease-out;
}
.fade-leave-active {
  transition: all 0.2s ease-in;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
  transform: translateY(-0.5rem);
}
</style>
