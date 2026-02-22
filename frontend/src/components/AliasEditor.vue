<script setup lang="ts">
import { ref, watch } from "vue";
import { useApi } from "@/composables/useApi";
import { useAuthStore } from "@/stores/auth";
import { useToast } from "@/composables/useToast";

const api = useApi();
const auth = useAuthStore();
const toast = useToast();

const alias = ref("");
const checking = ref(false);
const saving = ref(false);
const checkResult = ref<{ available: boolean; reason?: string } | null>(null);
const showConfirm = ref(false);

let debounceTimer: ReturnType<typeof setTimeout> | null = null;

watch(alias, (val) => {
  checkResult.value = null;
  showConfirm.value = false;

  if (debounceTimer) clearTimeout(debounceTimer);

  if (val.length < 3) return;

  debounceTimer = setTimeout(() => checkAlias(val), 350);
});

async function checkAlias(name: string) {
  if (name !== alias.value) return;
  checking.value = true;
  try {
    checkResult.value = await api.get<{ available: boolean; reason?: string }>(
      "/auth/check-alias",
      { name },
    );
  } catch {
    checkResult.value = null;
  } finally {
    checking.value = false;
  }
}

async function saveAlias() {
  if (!checkResult.value?.available) return;
  saving.value = true;
  try {
    await api.patch("/user/alias", { alias: alias.value });
    toast.success("Alias gespeichert!");
    await auth.fetchUser();
    alias.value = "";
    checkResult.value = null;
    showConfirm.value = false;
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "Alias konnte nicht gespeichert werden.";
    toast.error(msg);
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="bg-surface-1 rounded-card p-6">
    <h2 class="text-lg font-semibold text-text-primary mb-1">Spielername</h2>
    <p class="text-sm text-text-secondary mb-4">
      Wähle einen einzigartigen Namen, der in Ranglisten und Squads angezeigt wird.
    </p>

    <!-- Current alias -->
    <div class="flex items-center gap-3 mb-4 p-3 bg-surface-2 rounded-lg">
      <span class="text-sm text-text-secondary">Aktuell:</span>
      <span class="text-sm font-medium text-text-primary">{{ auth.user?.alias }}</span>
      <span
        v-if="!auth.user?.has_custom_alias"
        class="text-xs px-2 py-0.5 rounded-full bg-warning/10 text-warning"
      >
        Standard
      </span>
    </div>

    <!-- Already custom — locked -->
    <div v-if="auth.user?.has_custom_alias" class="flex items-center gap-2">
      <span class="w-2 h-2 rounded-full bg-primary" />
      <span class="text-sm text-primary font-medium">Dein Spielername ist gesetzt.</span>
    </div>

    <!-- Editor -->
    <template v-else>
      <div class="space-y-3">
        <div class="relative">
          <input
            v-model="alias"
            type="text"
            maxlength="20"
            autocomplete="off"
            spellcheck="false"
            class="w-full px-4 py-3 bg-surface-2 border rounded-lg text-text-primary text-sm transition-colors focus:outline-none focus:ring-1"
            :class="{
              'border-surface-3 focus:border-primary focus:ring-primary': !checkResult,
              'border-primary focus:border-primary focus:ring-primary': checkResult?.available,
              'border-danger focus:border-danger focus:ring-danger': checkResult && !checkResult.available,
            }"
            placeholder="Dein Spielername (3-20 Zeichen)"
            aria-describedby="alias-feedback"
          />
          <!-- Spinner -->
          <div
            v-if="checking"
            class="absolute right-3 top-1/2 -translate-y-1/2"
            aria-label="Prüfe Verfügbarkeit"
          >
            <svg class="animate-spin h-4 w-4 text-text-muted" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none" />
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
          </div>
        </div>

        <!-- Feedback -->
        <p
          v-if="checkResult"
          id="alias-feedback"
          class="text-xs"
          :class="checkResult.available ? 'text-primary' : 'text-danger'"
          role="status"
          aria-live="polite"
        >
          {{ checkResult.available ? "Verfügbar!" : checkResult.reason }}
        </p>

        <!-- Leaderboard preview -->
        <div
          v-if="checkResult?.available && alias"
          class="p-3 bg-surface-2 rounded-lg border border-surface-3/50"
        >
          <p class="text-xs text-text-muted mb-2">Vorschau in der Rangliste:</p>
          <div class="flex items-center gap-3">
            <span class="inline-flex items-center justify-center w-7 h-7 rounded-full text-sm font-bold bg-yellow-500/20 text-yellow-400">1</span>
            <span class="text-sm font-medium text-text-primary">{{ alias }}</span>
          </div>
        </div>

        <!-- Confirm -->
        <div v-if="checkResult?.available && !showConfirm">
          <button
            class="px-4 py-2 rounded-lg text-sm bg-primary text-surface-0 hover:bg-primary-hover transition-colors"
            @click="showConfirm = true"
          >
            Spielername übernehmen
          </button>
        </div>

        <div v-if="showConfirm" class="p-4 bg-warning/5 border border-warning/20 rounded-lg">
          <p class="text-sm text-text-primary mb-3">
            Bist du sicher? Der Spielername <strong class="text-primary">{{ alias }}</strong> kann danach nicht mehr geändert werden.
          </p>
          <div class="flex gap-2">
            <button
              :disabled="saving"
              class="px-4 py-2 rounded-lg text-sm bg-primary text-surface-0 hover:bg-primary-hover transition-colors disabled:opacity-50"
              @click="saveAlias"
            >
              <template v-if="saving">Speichern...</template>
              <template v-else>Ja, bestätigen</template>
            </button>
            <button
              class="px-4 py-2 rounded-lg text-sm text-text-secondary hover:bg-surface-2 transition-colors"
              @click="showConfirm = false"
            >
              Abbrechen
            </button>
          </div>
        </div>
      </div>

      <p class="text-xs text-text-muted mt-3">
        Erlaubt: Buchstaben (A-Z), Ziffern (0-9) und Unterstriche (_). Mindestens 3, maximal 20 Zeichen.
      </p>
    </template>
  </div>
</template>
