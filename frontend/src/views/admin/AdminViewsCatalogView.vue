<!--
frontend/src/views/admin/AdminViewsCatalogView.vue

Purpose:
    Admin-only overview for all frontend views with server-backed catalog,
    filtering, and direct navigation for concrete routes.
-->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";

type GroupFilter = "all" | "public" | "admin" | "auth" | "admin_required";
type ViewGroup = "public" | "admin";

interface ViewCatalogItem {
  id: string;
  name_key: string;
  route_name: string;
  path: string;
  group: ViewGroup;
  requires_auth: boolean;
  requires_admin: boolean;
  enabled: boolean;
}

interface ViewCatalogSummary {
  total: number;
  public: number;
  admin: number;
  auth_required: number;
  admin_required: number;
}

interface ViewCatalogResponse {
  generated_at_utc: string;
  summary: ViewCatalogSummary;
  items: ViewCatalogItem[];
}

const api = useApi();
const router = useRouter();
const { t } = useI18n();

const loading = ref(true);
const error = ref(false);
const query = ref("");
const groupFilter = ref<GroupFilter>("all");
const payload = ref<ViewCatalogResponse | null>(null);

const filterOptions = computed(() => ([
  { key: "all" as GroupFilter, label: t("admin.viewsCatalog.filters.all") },
  { key: "public" as GroupFilter, label: t("admin.viewsCatalog.filters.public") },
  { key: "admin" as GroupFilter, label: t("admin.viewsCatalog.filters.admin") },
  { key: "auth" as GroupFilter, label: t("admin.viewsCatalog.filters.authRequired") },
  { key: "admin_required" as GroupFilter, label: t("admin.viewsCatalog.filters.adminRequired") },
]));

const filteredItems = computed(() => {
  const items = payload.value?.items || [];
  const term = query.value.trim().toLowerCase();
  return items.filter((item) => {
    if (groupFilter.value === "public" && item.group !== "public") return false;
    if (groupFilter.value === "admin" && item.group !== "admin") return false;
    if (groupFilter.value === "auth" && !item.requires_auth) return false;
    if (groupFilter.value === "admin_required" && !item.requires_admin) return false;
    if (!term) return true;
    const localizedName = t(item.name_key).toLowerCase();
    return item.path.toLowerCase().includes(term) || localizedName.includes(term);
  });
});

function isDynamicPath(path: string): boolean {
  return path.includes(":");
}

async function loadCatalog() {
  loading.value = true;
  error.value = false;
  try {
    payload.value = await api.get<ViewCatalogResponse>("/admin/views/catalog");
  } catch {
    error.value = true;
    payload.value = null;
  } finally {
    loading.value = false;
  }
}

function openView(item: ViewCatalogItem): void {
  if (isDynamicPath(item.path) || !item.enabled) {
    return;
  }
  void router.push(item.path);
}

onMounted(() => {
  void loadCatalog();
});
</script>

<template>
  <div class="max-w-6xl mx-auto p-4">
    <div class="flex flex-wrap items-end justify-between gap-3 mb-4">
      <div>
        <h1 class="text-xl font-bold text-text-primary">{{ t("admin.viewsCatalog.title") }}</h1>
        <p class="text-sm text-text-muted">{{ t("admin.viewsCatalog.subtitle") }}</p>
      </div>
      <button class="px-3 py-2 rounded-card bg-surface-1 border border-surface-3/50 text-sm text-text-primary hover:bg-surface-2" @click="loadCatalog">
        {{ t("admin.viewsCatalog.actions.refresh") }}
      </button>
    </div>

    <div v-if="loading" class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
      <div v-for="n in 5" :key="n" class="h-16 bg-surface-1 rounded-card animate-pulse" />
    </div>

    <div v-else-if="error" class="text-center py-12 bg-surface-1 rounded-card border border-surface-3/50">
      <p class="text-text-muted mb-3">{{ t("admin.viewsCatalog.states.error") }}</p>
      <button class="text-sm text-primary hover:underline" @click="loadCatalog">{{ t("admin.viewsCatalog.actions.retry") }}</button>
    </div>

    <template v-else-if="payload">
      <div class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
        <div class="bg-surface-1 rounded-card p-3 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("admin.viewsCatalog.summary.total") }}</p>
          <p class="text-2xl font-semibold text-text-primary tabular-nums">{{ payload.summary.total }}</p>
        </div>
        <div class="bg-surface-1 rounded-card p-3 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("admin.viewsCatalog.summary.public") }}</p>
          <p class="text-2xl font-semibold text-text-primary tabular-nums">{{ payload.summary.public }}</p>
        </div>
        <div class="bg-surface-1 rounded-card p-3 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("admin.viewsCatalog.summary.admin") }}</p>
          <p class="text-2xl font-semibold text-text-primary tabular-nums">{{ payload.summary.admin }}</p>
        </div>
        <div class="bg-surface-1 rounded-card p-3 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("admin.viewsCatalog.summary.authRequired") }}</p>
          <p class="text-2xl font-semibold text-text-primary tabular-nums">{{ payload.summary.auth_required }}</p>
        </div>
        <div class="bg-surface-1 rounded-card p-3 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("admin.viewsCatalog.summary.adminRequired") }}</p>
          <p class="text-2xl font-semibold text-text-primary tabular-nums">{{ payload.summary.admin_required }}</p>
        </div>
      </div>

      <div class="bg-surface-1 rounded-card p-3 border border-surface-3/50 mb-4">
        <div class="flex flex-wrap gap-2 mb-3">
          <button
            v-for="option in filterOptions"
            :key="option.key"
            class="px-3 py-1.5 rounded-full text-xs border"
            :class="option.key === groupFilter ? 'bg-primary/15 border-primary text-primary' : 'bg-surface-2 border-surface-3 text-text-muted'"
            @click="groupFilter = option.key"
          >
            {{ option.label }}
          </button>
        </div>
        <input
          v-model="query"
          type="text"
          class="w-full rounded-card bg-surface-2 border border-surface-3/60 px-3 py-2 text-sm text-text-primary"
          :placeholder="t('admin.viewsCatalog.searchPlaceholder')"
        >
      </div>

      <div v-if="filteredItems.length === 0" class="text-center py-10 bg-surface-1 rounded-card border border-surface-3/50 text-text-muted">
        {{ t("admin.viewsCatalog.states.empty") }}
      </div>

      <div v-else class="bg-surface-1 rounded-card border border-surface-3/50 overflow-x-auto">
        <table class="min-w-full text-sm">
          <thead class="bg-surface-2 text-text-muted">
            <tr>
              <th class="text-left font-medium px-3 py-2">{{ t("admin.viewsCatalog.table.name") }}</th>
              <th class="text-left font-medium px-3 py-2">{{ t("admin.viewsCatalog.table.path") }}</th>
              <th class="text-left font-medium px-3 py-2">{{ t("admin.viewsCatalog.table.group") }}</th>
              <th class="text-left font-medium px-3 py-2">{{ t("admin.viewsCatalog.table.auth") }}</th>
              <th class="text-left font-medium px-3 py-2">{{ t("admin.viewsCatalog.table.admin") }}</th>
              <th class="text-left font-medium px-3 py-2">{{ t("admin.viewsCatalog.table.action") }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in filteredItems" :key="item.id" class="border-t border-surface-3/40">
              <td class="px-3 py-2 text-text-primary">{{ t(item.name_key) }}</td>
              <td class="px-3 py-2 font-mono text-xs text-text-muted">{{ item.path }}</td>
              <td class="px-3 py-2 text-text-muted">{{ t(`admin.viewsCatalog.groups.${item.group}`) }}</td>
              <td class="px-3 py-2 text-text-muted">{{ item.requires_auth ? t("admin.viewsCatalog.flags.yes") : t("admin.viewsCatalog.flags.no") }}</td>
              <td class="px-3 py-2 text-text-muted">{{ item.requires_admin ? t("admin.viewsCatalog.flags.yes") : t("admin.viewsCatalog.flags.no") }}</td>
              <td class="px-3 py-2">
                <button
                  class="px-2.5 py-1.5 rounded text-xs border"
                  :class="isDynamicPath(item.path) || !item.enabled ? 'border-surface-3 text-text-muted cursor-not-allowed' : 'border-primary text-primary hover:bg-primary/10'"
                  :disabled="isDynamicPath(item.path) || !item.enabled"
                  @click="openView(item)"
                >
                  {{ isDynamicPath(item.path) ? t("admin.viewsCatalog.actions.openDynamic") : t("admin.viewsCatalog.actions.open") }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>
