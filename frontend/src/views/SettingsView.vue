<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useRouter } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";
import TwoFaSetup from "@/components/TwoFaSetup.vue";
import AliasEditor from "@/components/AliasEditor.vue";

const router = useRouter();
const auth = useAuthStore();
const api = useApi();
const toast = useToast();

// Badges
interface Badge {
  key: string;
  name: string;
  description: string;
  icon: string;
  awarded_at: string | null;
}
const badges = ref<Badge[]>([]);
const badgesLoading = ref(true);

async function fetchBadges() {
  try {
    badges.value = await api.get<Badge[]>("/badges/mine");
  } catch {
    // silently fail
  } finally {
    badgesLoading.value = false;
  }
}

onMounted(fetchBadges);

// GDPR Export
const exporting = ref(false);

async function exportData() {
  exporting.value = true;
  try {
    const data = await api.get<Record<string, unknown>>("/gdpr/export");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "quotico-datenexport.json";
    a.click();
    URL.revokeObjectURL(url);
    toast.success("Datenexport heruntergeladen.");
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "Export fehlgeschlagen.";
    toast.error(msg);
  } finally {
    exporting.value = false;
  }
}

// Account Deletion
const showDeleteConfirm = ref(false);
const deleteConfirmText = ref("");
const deleting = ref(false);

async function deleteAccount() {
  if (deleteConfirmText.value !== "LÖSCHEN") return;
  deleting.value = true;
  try {
    await api.del("/gdpr/account");
    toast.success("Dein Konto wurde gelöscht.");
    auth.user = null;
    router.push("/");
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "Löschen fehlgeschlagen.";
    toast.error(msg);
  } finally {
    deleting.value = false;
  }
}
</script>

<template>
  <div class="max-w-2xl mx-auto px-4 py-8 space-y-6">
    <div>
      <h1 class="text-2xl font-bold text-text-primary">Einstellungen</h1>
      <p class="text-sm text-text-secondary mt-1">
        Konto und Sicherheit verwalten.
      </p>
    </div>

    <!-- Account info -->
    <div class="bg-surface-1 rounded-card p-6">
      <h2 class="text-lg font-semibold text-text-primary mb-4">Konto</h2>
      <dl class="space-y-3">
        <div class="flex items-center justify-between">
          <dt class="text-sm text-text-secondary">E-Mail</dt>
          <dd class="text-sm font-medium text-text-primary">{{ auth.user?.email }}</dd>
        </div>
        <div class="flex items-center justify-between">
          <dt class="text-sm text-text-secondary">Punkte</dt>
          <dd class="text-sm font-mono font-bold text-primary">{{ auth.user?.points?.toFixed(1) ?? "0.0" }}</dd>
        </div>
        <div class="flex items-center justify-between">
          <dt class="text-sm text-text-secondary">Mitglied seit</dt>
          <dd class="text-sm text-text-primary">
            {{ auth.user?.created_at
              ? new Date(auth.user.created_at).toLocaleDateString("de-DE", { day: "numeric", month: "long", year: "numeric" })
              : "-"
            }}
          </dd>
        </div>
      </dl>
    </div>

    <!-- Badges -->
    <div class="bg-surface-1 rounded-card p-6">
      <h2 class="text-lg font-semibold text-text-primary mb-4">Badges</h2>
      <div v-if="badgesLoading" class="flex gap-3">
        <div v-for="i in 4" :key="i" class="w-16 h-20 bg-surface-2 rounded-lg animate-pulse" />
      </div>
      <div v-else class="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-3">
        <div
          v-for="badge in badges"
          :key="badge.key"
          class="flex flex-col items-center gap-1 p-3 rounded-lg transition-colors"
          :class="badge.awarded_at ? 'bg-surface-2' : 'bg-surface-2/40 opacity-40'"
          :title="badge.description"
        >
          <span class="text-2xl">{{ badge.icon }}</span>
          <span class="text-xs font-medium text-text-primary text-center leading-tight">{{ badge.name }}</span>
          <span v-if="badge.awarded_at" class="text-[10px] text-text-muted">
            {{ new Date(badge.awarded_at).toLocaleDateString("de-DE", { day: "numeric", month: "short" }) }}
          </span>
        </div>
      </div>
      <p v-if="!badgesLoading && badges.filter(b => b.awarded_at).length === 0" class="text-sm text-text-muted mt-3">
        Noch keine Badges verdient. Gib Tipps ab, um Badges freizuschalten!
      </p>
    </div>

    <!-- Alias / Spielername -->
    <AliasEditor />

    <!-- 2FA -->
    <TwoFaSetup />

    <!-- GDPR Export -->
    <div class="bg-surface-1 rounded-card p-6">
      <h2 class="text-lg font-semibold text-text-primary mb-1">Datenexport (DSGVO)</h2>
      <p class="text-sm text-text-secondary mb-4">
        Lade alle deine gespeicherten Daten als JSON-Datei herunter.
      </p>
      <button
        :disabled="exporting"
        class="px-4 py-2 rounded-lg text-sm bg-secondary text-white hover:bg-secondary-hover transition-colors disabled:opacity-50"
        @click="exportData"
      >
        <template v-if="exporting">
          <span class="inline-flex items-center gap-2">
            <svg class="animate-spin h-4 w-4" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none" />
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            Wird exportiert...
          </span>
        </template>
        <template v-else>
          Daten herunterladen
        </template>
      </button>
    </div>

    <!-- Danger zone: Account deletion -->
    <div class="bg-surface-1 rounded-card p-6 border border-danger/20">
      <h2 class="text-lg font-semibold text-danger mb-1">Konto löschen</h2>
      <p class="text-sm text-text-secondary mb-4">
        Dein Konto wird anonymisiert. Deine Tipps bleiben anonym in der Datenbank,
        aber deine E-Mail und persönlichen Daten werden unwiderruflich entfernt.
      </p>

      <button
        v-if="!showDeleteConfirm"
        class="px-4 py-2 rounded-lg text-sm border border-danger/30 text-danger hover:bg-danger-muted/10 transition-colors"
        @click="showDeleteConfirm = true"
      >
        Konto löschen...
      </button>

      <Transition name="fade">
        <div v-if="showDeleteConfirm" class="mt-4 p-4 bg-danger-muted/10 rounded-lg">
          <p class="text-sm text-text-primary mb-3">
            Gib <strong class="text-danger">LÖSCHEN</strong> ein, um dein Konto unwiderruflich zu löschen.
          </p>
          <div class="flex gap-3">
            <input
              v-model="deleteConfirmText"
              type="text"
              class="flex-1 px-4 py-2 bg-surface-2 border border-surface-3 rounded-lg text-text-primary text-sm transition-colors focus:border-danger focus:ring-1 focus:ring-danger"
              placeholder="LÖSCHEN"
            />
            <button
              :disabled="deleteConfirmText !== 'LÖSCHEN' || deleting"
              class="px-4 py-2 rounded-lg text-sm bg-danger text-white hover:bg-danger-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              @click="deleteAccount"
            >
              Endgültig löschen
            </button>
          </div>
          <button
            class="mt-2 text-sm text-text-muted hover:text-text-secondary transition-colors"
            @click="showDeleteConfirm = false; deleteConfirmText = ''"
          >
            Abbrechen
          </button>
        </div>
      </Transition>
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
