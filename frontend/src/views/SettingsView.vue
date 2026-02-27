<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useAuthStore } from "@/stores/auth";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";
import TwoFaSetup from "@/components/TwoFaSetup.vue";
import AliasEditor from "@/components/AliasEditor.vue";
import type { TipPersona } from "@/types/persona";

const { t } = useI18n();
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

// Security log
interface SecurityEvent {
  timestamp: string;
  action: string;
  ip_truncated: string;
}

const securityLog = ref<SecurityEvent[]>([]);
const securityLogLoading = ref(true);


async function fetchSecurityLog() {
  try {
    securityLog.value = await api.get<SecurityEvent[]>("/gdpr/security-log");
  } catch {
    // silently fail
  } finally {
    securityLogLoading.value = false;
  }
}

onMounted(fetchSecurityLog);

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
    toast.success(t('settings.exportSuccess'));
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : t('settings.exportFailed');
    toast.error(msg);
  } finally {
    exporting.value = false;
  }
}

// Account Deletion
const showDeleteConfirm = ref(false);
const deleteConfirmText = ref("");
const deleting = ref(false);
const updatingPersona = ref(false);

const personaOptions: TipPersona[] = ["casual", "pro", "silent", "experimental"];

async function updatePersona(persona: TipPersona) {
  updatingPersona.value = true;
  try {
    await auth.updateTipPersona(persona);
    toast.success(t("settings.persona.saved"));
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : t("settings.persona.saveError");
    toast.error(msg);
  } finally {
    updatingPersona.value = false;
  }
}

async function deleteAccount() {
  if (deleteConfirmText.value !== "LÖSCHEN") return;
  deleting.value = true;
  try {
    await api.del("/gdpr/account");
    toast.success(t('deleteAccount.success'));
    auth.user = null;
    router.push("/");
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : t('deleteAccount.error');
    toast.error(msg);
  } finally {
    deleting.value = false;
  }
}
</script>

<template>
  <div class="max-w-2xl mx-auto px-4 py-8 space-y-6">
    <div>
      <h1 class="text-2xl font-bold text-text-primary">{{ $t('settings.heading') }}</h1>
      <p class="text-sm text-text-secondary mt-1">
        {{ $t('settings.description') }}
      </p>
    </div>

    <!-- Account info -->
    <div class="bg-surface-1 rounded-card p-6">
      <h2 class="text-lg font-semibold text-text-primary mb-4">{{ $t('settings.account') }}</h2>
      <dl class="space-y-3">
        <div class="flex items-center justify-between">
          <dt class="text-sm text-text-secondary">{{ $t('settings.email') }}</dt>
          <dd class="text-sm font-medium text-text-primary">{{ auth.user?.email }}</dd>
        </div>
        <div class="flex items-center justify-between">
          <dt class="text-sm text-text-secondary">{{ $t('settings.points') }}</dt>
          <dd class="text-sm font-mono font-bold text-primary">{{ auth.user?.points?.toFixed(1) ?? "0.0" }}</dd>
        </div>
        <div class="flex items-center justify-between">
          <dt class="text-sm text-text-secondary">{{ $t('settings.memberSince') }}</dt>
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
        {{ $t('settings.noBadges') }}
      </p>
    </div>

    <!-- Alias / Spielername -->
    <AliasEditor />

    <!-- Persona -->
    <div class="bg-surface-1 rounded-card p-6">
      <h2 class="text-lg font-semibold text-text-primary mb-1">{{ $t("settings.persona.title") }}</h2>
      <p class="text-sm text-text-secondary mb-4">{{ $t("settings.persona.description") }}</p>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label class="block">
          <span class="block text-sm text-text-secondary mb-1">{{ $t("settings.persona.selected") }}</span>
          <select
            class="w-full rounded-lg border border-surface-3 bg-surface-2 px-3 py-2 text-sm text-text-primary"
            :value="auth.user?.tip_persona || 'casual'"
            :disabled="updatingPersona || !auth.user"
            @change="updatePersona(($event.target as HTMLSelectElement).value as TipPersona)"
          >
            <option v-for="p in personaOptions" :key="p" :value="p">
              {{ $t(`settings.persona.options.${p}`) }}
            </option>
          </select>
        </label>
        <div class="rounded-lg border border-surface-3 bg-surface-2 px-3 py-2">
          <div class="text-sm text-text-secondary">{{ $t("settings.persona.effective") }}</div>
          <div class="mt-1 text-sm font-medium text-text-primary">
            {{ $t(`settings.persona.options.${auth.user?.tip_persona_effective || "casual"}`) }}
          </div>
          <div class="mt-1 text-xs text-text-muted">
            {{ $t(`settings.persona.source.${auth.user?.tip_persona_source || "default"}`) }}
          </div>
        </div>
      </div>
      <p
        v-if="auth.user?.tip_persona && auth.user?.tip_persona_effective && auth.user.tip_persona !== auth.user.tip_persona_effective"
        class="mt-3 text-xs text-warning"
      >
        {{ $t("settings.persona.limitedHint") }}
      </p>
    </div>

    <!-- 2FA -->
    <TwoFaSetup />

    <!-- Security Log -->
    <div class="bg-surface-1 rounded-card p-6">
      <h2 class="text-lg font-semibold text-text-primary mb-1">{{ $t('settings.securityLog') }}</h2>
      <p class="text-sm text-text-secondary mb-4">
        {{ $t('settings.securityLogDescription') }}
      </p>

      <div v-if="securityLogLoading" class="space-y-2">
        <div v-for="i in 3" :key="i" class="h-10 bg-surface-2 rounded animate-pulse" />
      </div>

      <div v-else-if="securityLog.length === 0" class="text-sm text-text-muted">
        {{ $t('settings.noEntries') }}
      </div>

      <div v-else class="overflow-x-auto -mx-6 px-6">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-text-muted border-b border-surface-3">
              <th class="pb-2 pr-4 font-medium">{{ $t('settings.date') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('settings.action') }}</th>
              <th class="pb-2 font-medium">{{ $t('settings.ip') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(entry, idx) in securityLog"
              :key="idx"
              class="border-b border-surface-3/50 last:border-0"
            >
              <td class="py-2 pr-4 text-text-primary whitespace-nowrap">
                {{ new Date(entry.timestamp).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }) }}
              </td>
              <td class="py-2 pr-4 text-text-primary">
                {{ $t(`settings.securityActions.${entry.action}`) }}
              </td>
              <td class="py-2 text-text-muted font-mono text-xs">
                {{ entry.ip_truncated || "-" }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- GDPR Export -->
    <div class="bg-surface-1 rounded-card p-6">
      <h2 class="text-lg font-semibold text-text-primary mb-1">{{ $t('settings.gdprHeading') }}</h2>
      <p class="text-sm text-text-secondary mb-4">
        {{ $t('settings.gdprDescription') }}
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
            {{ $t('settings.exporting') }}
          </span>
        </template>
        <template v-else>
          {{ $t('settings.download') }}
        </template>
      </button>
    </div>

    <!-- Danger zone: Account deletion -->
    <div class="bg-surface-1 rounded-card p-6 border border-danger/20">
      <h2 class="text-lg font-semibold text-danger mb-1">{{ $t('deleteAccount.heading') }}</h2>
      <p class="text-sm text-text-secondary mb-4">
        {{ $t('deleteAccount.description') }}
      </p>

      <button
        v-if="!showDeleteConfirm"
        class="px-4 py-2 rounded-lg text-sm border border-danger/30 text-danger hover:bg-danger-muted/10 transition-colors"
        @click="showDeleteConfirm = true"
      >
        {{ $t('deleteAccount.deleting') }}
      </button>

      <Transition name="fade">
        <div v-if="showDeleteConfirm" class="mt-4 p-4 bg-danger-muted/10 rounded-lg">
          <p class="text-sm text-text-primary mb-3">
            {{ $t('deleteAccount.instruction') }}
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
              {{ $t('deleteAccount.finalDelete') }}
            </button>
          </div>
          <button
            class="mt-2 text-sm text-text-muted hover:text-text-secondary transition-colors"
            @click="showDeleteConfirm = false; deleteConfirmText = ''"
          >
            {{ $t('common.cancel') }}
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
