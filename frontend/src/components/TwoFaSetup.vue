<script setup lang="ts">
import { ref } from "vue";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";
import { useAuthStore } from "@/stores/auth";

const api = useApi();
const toast = useToast();
const auth = useAuthStore();

const step = ref<"idle" | "setup" | "verify">("idle");
const qrCodeSrc = ref("");
const totpCode = ref("");
const loading = ref(false);

async function startSetup() {
  loading.value = true;
  try {
    const data = await api.post<{ qr_code: string }>("/2fa/setup");
    qrCodeSrc.value = `data:image/png;base64,${data.qr_code}`;
    step.value = "setup";
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "2FA-Setup fehlgeschlagen.";
    toast.error(msg);
  } finally {
    loading.value = false;
  }
}

async function verifyCode() {
  if (totpCode.value.length !== 6) return;
  loading.value = true;
  try {
    await api.post("/2fa/verify", { code: totpCode.value });
    toast.success("2FA erfolgreich aktiviert!");
    await auth.fetchUser();
    step.value = "idle";
    totpCode.value = "";
    qrCodeSrc.value = "";
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "Code ungültig.";
    toast.error(msg);
  } finally {
    loading.value = false;
  }
}

async function disable2fa() {
  loading.value = true;
  try {
    await api.post("/2fa/disable");
    toast.success("2FA deaktiviert.");
    await auth.fetchUser();
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "Fehler beim Deaktivieren.";
    toast.error(msg);
  } finally {
    loading.value = false;
  }
}

function cancel() {
  step.value = "idle";
  totpCode.value = "";
  qrCodeSrc.value = "";
}
</script>

<template>
  <div class="bg-surface-1 rounded-card p-6">
    <h2 class="text-lg font-semibold text-text-primary mb-1">
      Zwei-Faktor-Authentifizierung (2FA)
    </h2>
    <p class="text-sm text-text-secondary mb-4">
      Schütze dein Konto mit einem zusätzlichen Sicherheitsschritt.
    </p>

    <!-- Already enabled -->
    <div v-if="auth.user?.is_2fa_enabled && step === 'idle'">
      <div class="flex items-center gap-2 mb-4">
        <span class="w-2 h-2 rounded-full bg-primary" />
        <span class="text-sm font-medium text-primary">Aktiviert</span>
      </div>
      <button
        :disabled="loading"
        class="px-4 py-2 rounded-lg text-sm border border-danger/30 text-danger hover:bg-danger-muted/10 transition-colors disabled:opacity-50"
        @click="disable2fa"
      >
        2FA deaktivieren
      </button>
    </div>

    <!-- Not enabled — show setup button -->
    <div v-else-if="step === 'idle'">
      <div class="flex items-center gap-2 mb-4">
        <span class="w-2 h-2 rounded-full bg-text-muted" />
        <span class="text-sm text-text-muted">Nicht aktiviert</span>
      </div>
      <button
        :disabled="loading"
        class="px-4 py-2 rounded-lg text-sm bg-primary text-surface-0 hover:bg-primary-hover transition-colors disabled:opacity-50"
        @click="startSetup"
      >
        2FA einrichten
      </button>
    </div>

    <!-- Setup: show QR code -->
    <div v-else-if="step === 'setup'">
      <p class="text-sm text-text-secondary mb-4">
        Scanne diesen QR-Code mit deiner Authenticator-App (z.B. Google Authenticator, Authy).
      </p>
      <div class="flex justify-center mb-6">
        <img
          :src="qrCodeSrc"
          alt="QR-Code für 2FA-Setup"
          class="w-48 h-48 rounded-lg bg-white p-2"
        />
      </div>
      <p class="text-sm text-text-secondary mb-3">
        Gib anschließend den 6-stelligen Code ein:
      </p>
      <form @submit.prevent="verifyCode" class="flex gap-3">
        <input
          v-model="totpCode"
          type="text"
          inputmode="numeric"
          pattern="[0-9]{6}"
          maxlength="6"
          autocomplete="one-time-code"
          class="flex-1 px-4 py-3 bg-surface-2 border border-surface-3 rounded-lg text-text-primary font-mono text-center tracking-[0.5em] text-sm transition-colors focus:border-primary focus:ring-1 focus:ring-primary"
          placeholder="000000"
        />
        <button
          type="submit"
          :disabled="loading || totpCode.length !== 6"
          class="px-4 py-3 rounded-lg text-sm bg-primary text-surface-0 hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Bestätigen
        </button>
      </form>
      <button
        class="mt-3 text-sm text-text-muted hover:text-text-secondary transition-colors"
        @click="cancel"
      >
        Abbrechen
      </button>
    </div>
  </div>
</template>
