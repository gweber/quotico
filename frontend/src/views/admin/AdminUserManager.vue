<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";
import type { TipPersona, TipPersonaSource } from "@/types/persona";

const api = useApi();
const toast = useToast();
const { t } = useI18n();

interface AdminUser {
  id: string;
  email: string;
  alias: string;
  has_custom_alias: boolean;
  points: number;
  is_admin: boolean;
  is_banned: boolean;
  is_2fa_enabled: boolean;
  created_at: string;
  bet_count: number;
  tip_persona: TipPersona;
  tip_override_persona: TipPersona | null;
  tip_persona_effective: TipPersona;
  tip_persona_source: TipPersonaSource;
  tip_persona_updated_at: string | null;
}

const users = ref<AdminUser[]>([]);
const search = ref("");
const loading = ref(true);
const error = ref(false);
const savingPolicy = ref(false);
const simulatingPolicy = ref(false);
const policyVersion = ref(1);
const policyNote = ref("");
const policyRulesJson = ref("[]");
const policySimulation = ref<{ affected_users: number; delta: Record<string, number> } | null>(null);
const personaOptions: TipPersona[] = ["casual", "pro", "silent", "experimental"];

// Points adjustment
const adjustUserId = ref<string | null>(null);
const adjustDelta = ref(0);
const adjustReason = ref("");

async function fetchUsers() {
  loading.value = true;
  error.value = false;
  try {
    const params: Record<string, string> = {};
    if (search.value) params.search = search.value;
    users.value = await api.get<AdminUser[]>("/admin/users", params);
  } catch {
    error.value = true;
  } finally {
    loading.value = false;
  }
}

async function setPersona(user: AdminUser, persona: TipPersona) {
  try {
    await api.patch(`/admin/users/${user.id}/tip-persona`, { tip_persona: persona });
    await fetchUsers();
    toast.success(t("admin.userManager.personaSaved"));
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t("common.loadError"));
  }
}

async function setOverride(user: AdminUser, override: TipPersona | null) {
  try {
    await api.patch(`/admin/users/${user.id}/tip-override`, { tip_override_persona: override });
    await fetchUsers();
    toast.success(t("admin.userManager.overrideSaved"));
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t("common.loadError"));
  }
}

async function fetchPolicy() {
  try {
    const doc = await api.get<{ version: number; note?: string | null; rules: unknown[] }>("/admin/tip-policy");
    policyVersion.value = doc.version || 1;
    policyNote.value = doc.note || "";
    policyRulesJson.value = JSON.stringify(doc.rules || [], null, 2);
  } catch {
    // silent
  }
}

async function simulatePolicy() {
  simulatingPolicy.value = true;
  try {
    const rules = JSON.parse(policyRulesJson.value);
    policySimulation.value = await api.post("/admin/tip-policy/simulate", { rules, note: policyNote.value || null });
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t("common.loadError"));
  } finally {
    simulatingPolicy.value = false;
  }
}

async function savePolicy() {
  savingPolicy.value = true;
  try {
    const rules = JSON.parse(policyRulesJson.value);
    const result = await api.patch<{ new_version: number }>("/admin/tip-policy", { rules, note: policyNote.value || null });
    policyVersion.value = result.new_version;
    toast.success(t("admin.userManager.policySaved"));
    await fetchPolicy();
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t("common.loadError"));
  } finally {
    savingPolicy.value = false;
  }
}

async function toggleBan(user: AdminUser) {
  try {
    if (user.is_banned) {
      await api.post(`/admin/users/${user.id}/unban`);
      toast.success(`${user.alias} entsperrt.`);
    } else {
      await api.post(`/admin/users/${user.id}/ban`);
      toast.success(`${user.alias} gesperrt.`);
    }
    await fetchUsers();
  } catch (e: any) {
    toast.error(e.message);
  }
}

async function submitAdjust() {
  if (!adjustUserId.value || !adjustDelta.value || !adjustReason.value) return;
  try {
    await api.post(`/admin/users/${adjustUserId.value}/points`, {
      delta: adjustDelta.value,
      reason: adjustReason.value,
    });
    toast.success("Punkte angepasst.");
    adjustUserId.value = null;
    adjustDelta.value = 0;
    adjustReason.value = "";
    await fetchUsers();
  } catch (e: any) {
    toast.error(e.message);
  }
}

async function resetAlias(user: AdminUser) {
  try {
    await api.post(`/admin/users/${user.id}/reset-alias`);
    toast.success(`Alias von ${user.alias} zurückgesetzt.`);
    await fetchUsers();
  } catch (e: any) {
    toast.error(e.message);
  }
}

onMounted(async () => {
  await Promise.all([fetchUsers(), fetchPolicy()]);
});

let searchTimer: ReturnType<typeof setTimeout> | null = null;
function onSearch() {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(fetchUsers, 300);
}
</script>

<template>
  <div class="max-w-4xl mx-auto p-4">
    <div class="flex items-center justify-between mb-6">
      <div>
        <RouterLink to="/admin" class="text-xs text-text-muted hover:text-text-primary">&larr; Dashboard</RouterLink>
        <h1 class="text-xl font-bold text-text-primary">User Management</h1>
      </div>
    </div>

    <!-- Search -->
    <div class="mb-4">
      <input
        v-model="search"
        @input="onSearch"
        type="text"
        placeholder="E-Mail oder Alias suchen..."
        class="w-full max-w-sm px-3 py-2 rounded-lg bg-surface-2 border border-surface-3 text-text-primary text-sm focus:ring-2 focus:ring-primary focus:outline-none"
      />
    </div>

    <div class="mb-6 rounded-card border border-surface-3/50 bg-surface-1 p-4">
      <h2 class="text-base font-semibold text-text-primary">{{ t("admin.userManager.policyTitle") }}</h2>
      <p class="mt-1 text-xs text-text-muted">{{ t("admin.userManager.policyVersion", { version: policyVersion }) }}</p>
      <label class="mt-3 block text-xs text-text-secondary">{{ t("admin.userManager.policyNote") }}</label>
      <input
        v-model="policyNote"
        type="text"
        class="mt-1 w-full rounded-lg border border-surface-3 bg-surface-2 px-3 py-2 text-sm text-text-primary"
      />
      <label class="mt-3 block text-xs text-text-secondary">{{ t("admin.userManager.policyRules") }}</label>
      <textarea
        v-model="policyRulesJson"
        rows="8"
        class="mt-1 w-full rounded-lg border border-surface-3 bg-surface-2 px-3 py-2 font-mono text-xs text-text-primary"
      />
      <div class="mt-3 flex flex-wrap gap-2">
        <button
          class="rounded bg-surface-2 px-3 py-1.5 text-xs text-text-secondary hover:bg-surface-3"
          :disabled="simulatingPolicy"
          @click="simulatePolicy"
        >
          {{ t("admin.userManager.simulate") }}
        </button>
        <button
          class="rounded bg-primary px-3 py-1.5 text-xs text-surface-0 hover:bg-primary-hover disabled:opacity-60"
          :disabled="savingPolicy"
          @click="savePolicy"
        >
          {{ t("admin.userManager.savePolicy") }}
        </button>
      </div>
      <div v-if="policySimulation" class="mt-3 rounded border border-surface-3 bg-surface-2 px-3 py-2 text-xs text-text-secondary">
        {{ t("admin.userManager.simAffected", { count: policySimulation.affected_users }) }}
        <span v-if="Object.keys(policySimulation.delta).length > 0" class="ml-2">
          {{ Object.entries(policySimulation.delta).map(([k, v]) => `${k}:${v}`).join(", ") }}
        </span>
      </div>
    </div>

    <!-- Error -->
    <div v-if="error" class="text-center py-12">
      <p class="text-text-muted mb-3">Error loading.</p>
      <button class="text-sm text-primary hover:underline" @click="fetchUsers">Try again</button>
    </div>

    <!-- Table -->
    <div v-else class="bg-surface-1 rounded-card border border-surface-3/50 overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="text-xs text-text-muted border-b border-surface-3">
            <th class="text-left px-4 py-3 font-medium">User</th>
            <th class="text-right px-4 py-3 font-medium">Punkte</th>
            <th class="text-right px-4 py-3 font-medium">Tipps</th>
            <th class="text-left px-4 py-3 font-medium">{{ t("admin.userManager.persona") }}</th>
            <th class="text-center px-4 py-3 font-medium">Status</th>
            <th class="text-right px-4 py-3 font-medium">Aktionen</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="6" class="px-4 py-8 text-center text-text-muted">Laden...</td>
          </tr>
          <tr
            v-for="u in users"
            :key="u.id"
            class="border-b border-surface-3/20 last:border-0"
          >
            <td class="px-4 py-3">
              <div class="flex flex-col">
                <div>
                  <span class="text-text-primary font-medium">{{ u.alias }}</span>
                  <span v-if="u.is_admin" class="ml-1 text-xs text-primary font-medium">(Admin)</span>
                </div>
                <span class="text-xs text-text-muted">{{ u.email }}</span>
              </div>
            </td>
            <td class="px-4 py-3 text-right tabular-nums text-text-primary">{{ u.points.toFixed(1) }}</td>
            <td class="px-4 py-3 text-right tabular-nums text-text-muted">{{ u.bet_count }}</td>
            <td class="px-4 py-3">
              <div class="space-y-1">
                <div class="flex items-center gap-2">
                  <select
                    class="rounded border border-surface-3 bg-surface-2 px-2 py-1 text-xs text-text-primary"
                    :value="u.tip_persona"
                    @change="setPersona(u, ($event.target as HTMLSelectElement).value as TipPersona)"
                  >
                    <option v-for="p in personaOptions" :key="`p-${u.id}-${p}`" :value="p">
                      {{ $t(`settings.persona.options.${p}`) }}
                    </option>
                  </select>
                  <select
                    class="rounded border border-surface-3 bg-surface-2 px-2 py-1 text-xs text-text-primary"
                    :value="u.tip_override_persona ?? ''"
                    @change="setOverride(u, (($event.target as HTMLSelectElement).value || null) as TipPersona | null)"
                  >
                    <option value="">{{ t("admin.userManager.noOverride") }}</option>
                    <option v-for="p in personaOptions" :key="`o-${u.id}-${p}`" :value="p">
                      {{ $t(`settings.persona.options.${p}`) }}
                    </option>
                  </select>
                </div>
                <div class="text-[11px] text-text-muted">
                  {{ t("admin.userManager.effective", { persona: $t(`settings.persona.options.${u.tip_persona_effective}`), source: $t(`settings.persona.source.${u.tip_persona_source}`) }) }}
                </div>
              </div>
            </td>
            <td class="px-4 py-3 text-center">
              <span
                v-if="u.is_banned"
                class="text-xs px-2 py-0.5 rounded-full bg-danger-muted/20 text-danger font-medium"
              >Gesperrt</span>
              <span v-else class="text-xs text-text-muted">Aktiv</span>
            </td>
            <td class="px-4 py-3 text-right">
              <div class="flex gap-1 justify-end flex-wrap">
                <button
                  class="text-xs px-2 py-1 rounded bg-surface-2 hover:bg-surface-3 text-text-secondary transition-colors"
                  @click="adjustUserId = u.id"
                >Punkte</button>
                <button
                  v-if="u.has_custom_alias"
                  class="text-xs px-2 py-1 rounded bg-warning/10 text-warning hover:bg-warning/20 transition-colors"
                  @click="resetAlias(u)"
                >Alias Reset</button>
                <button
                  v-if="!u.is_admin"
                  class="text-xs px-2 py-1 rounded transition-colors"
                  :class="u.is_banned ? 'bg-primary/10 text-primary hover:bg-primary/20' : 'bg-danger/10 text-danger hover:bg-danger/20'"
                  @click="toggleBan(u)"
                >{{ u.is_banned ? "Entsperren" : "Sperren" }}</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Points Adjustment Modal -->
    <Teleport to="body">
      <Transition name="fade">
        <div
          v-if="adjustUserId"
          class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60"
          @click.self="adjustUserId = null"
        >
          <div class="bg-surface-1 rounded-card p-6 w-full max-w-sm border border-surface-3">
            <h2 class="text-lg font-semibold text-text-primary mb-4">Punkte anpassen</h2>
            <form @submit.prevent="submitAdjust" class="space-y-4">
              <div>
                <label class="block text-sm text-text-secondary mb-1">Betrag (+/-)</label>
                <input
                  v-model.number="adjustDelta"
                  type="number"
                  step="0.1"
                  class="w-full px-3 py-2 rounded-lg bg-surface-2 border border-surface-3 text-text-primary text-sm focus:ring-2 focus:ring-primary focus:outline-none"
                  placeholder="z.B. -5.0 oder +10"
                  required
                />
              </div>
              <div>
                <label class="block text-sm text-text-secondary mb-1">Grund</label>
                <input
                  v-model="adjustReason"
                  type="text"
                  class="w-full px-3 py-2 rounded-lg bg-surface-2 border border-surface-3 text-text-primary text-sm focus:ring-2 focus:ring-primary focus:outline-none"
                  placeholder="z.B. Fehlerhafte Auswertung korrigiert"
                  required
                />
              </div>
              <div class="flex gap-2 justify-end">
                <button
                  type="button"
                  class="px-4 py-2 text-sm rounded-lg text-text-secondary hover:bg-surface-2"
                  @click="adjustUserId = null"
                >Abbrechen</button>
                <button
                  type="submit"
                  :disabled="!adjustDelta || !adjustReason"
                  class="px-4 py-2 text-sm rounded-lg bg-primary text-surface-0 hover:bg-primary-hover disabled:opacity-50"
                >Anpassen</button>
              </div>
            </form>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.fade-enter-active { transition: opacity 0.15s ease-out; }
.fade-leave-active { transition: opacity 0.1s ease-in; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
