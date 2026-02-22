<script setup lang="ts">
import { ref } from "vue";

const emit = defineEmits<{ confirmed: [] }>();

const ageConfirmed = ref(false);
const termsAccepted = ref(false);
const visible = ref(true);

function confirm() {
  if (!ageConfirmed.value || !termsAccepted.value) return;
  try {
    localStorage.setItem("ageGateAcceptedAt", new Date().toISOString());
  } catch {
    // localStorage unavailable (private browsing) — proceed anyway
  }
  visible.value = false;
}

function onLeave() {
  emit("confirmed");
}
</script>

<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-300 ease-out"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition duration-200 ease-in"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
      @after-leave="onLeave"
    >
      <div
        v-if="visible"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4"
      >
        <div
          class="bg-surface-1 rounded-card w-full max-w-md p-8 shadow-xl"
          role="dialog"
          aria-modal="true"
          aria-labelledby="age-gate-title"
        >
          <div class="text-center mb-6">
            <h2
              id="age-gate-title"
              class="text-xl font-bold text-text-primary"
            >
              Willkommen bei Quotico.de
            </h2>
            <p class="text-sm text-text-secondary mt-2">
              Bitte bestätige die folgenden Hinweise, um fortzufahren.
            </p>
          </div>

          <div class="space-y-4 mb-6">
            <label class="flex items-start gap-3 cursor-pointer">
              <input
                v-model="ageConfirmed"
                type="checkbox"
                autofocus
                class="mt-0.5 w-4 h-4 rounded border-surface-3 bg-surface-2 text-primary focus:ring-primary focus:ring-1"
              />
              <span class="text-sm text-text-secondary leading-relaxed">
                Ich bestätige, dass ich mindestens
                <strong class="text-text-primary">18 Jahre</strong> alt bin.
              </span>
            </label>

            <label class="flex items-start gap-3 cursor-pointer">
              <input
                v-model="termsAccepted"
                type="checkbox"
                class="mt-0.5 w-4 h-4 rounded border-surface-3 bg-surface-2 text-primary focus:ring-primary focus:ring-1"
              />
              <span class="text-sm text-text-secondary leading-relaxed">
                Ich akzeptiere die
                <a
                  href="/legal/agb"
                  target="_blank"
                  rel="noopener"
                  class="text-secondary underline"
                >AGB</a>
                und habe die
                <a
                  href="/legal/datenschutz"
                  target="_blank"
                  rel="noopener"
                  class="text-secondary underline"
                >Datenschutzerklärung</a>
                zur Kenntnis genommen.
              </span>
            </label>
          </div>

          <button
            :disabled="!ageConfirmed || !termsAccepted"
            class="w-full py-3 rounded-lg bg-primary text-surface-0 font-semibold text-sm hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            @click="confirm"
          >
            Weiter zu Quotico.de
          </button>

          <p class="text-center text-xs text-text-muted mt-4">
            Kein echtes Geld. Quotico dient nur der Unterhaltung.
          </p>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
