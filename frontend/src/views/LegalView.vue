<script setup lang="ts">
import { ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useApi } from "@/composables/useApi";

interface LegalDoc {
  key: string;
  title: string;
  slug: string;
  content_html: string;
  version: string | null;
  updated_at: string;
}

const route = useRoute();
const router = useRouter();
const api = useApi();

const tabs = [
  { key: "imprint", slug: "impressum", label: "Impressum" },
  { key: "privacy", slug: "datenschutz", label: "Datenschutz" },
  { key: "terms", slug: "agb", label: "AGB" },
  { key: "youth-protection", slug: "jugendschutz", label: "Jugendschutz" },
];

const slugToKey: Record<string, string> = {
  impressum: "imprint",
  datenschutz: "privacy",
  agb: "terms",
  jugendschutz: "youth-protection",
};

const activeTab = ref("imprint");
const doc = ref<LegalDoc | null>(null);
const loading = ref(true);

async function loadDoc(key: string) {
  loading.value = true;
  try {
    doc.value = await api.get<LegalDoc>(`/legal/${key}`);
  } catch {
    doc.value = null;
  } finally {
    loading.value = false;
  }
}

function selectTab(slug: string) {
  router.replace({ params: { section: slug } });
}

watch(
  () => route.params.section as string,
  (slug) => {
    const key = slugToKey[slug] || "imprint";
    activeTab.value = key;
    loadDoc(key);
  },
  { immediate: true },
);
</script>

<template>
  <main id="main-content" class="max-w-3xl mx-auto px-4 py-8">
    <h1 class="text-2xl font-bold text-text-primary mb-6">Rechtliches</h1>

    <!-- Tab navigation -->
    <nav
      class="flex gap-1 mb-8 overflow-x-auto border-b border-surface-3"
      aria-label="Rechtliche Dokumente"
    >
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors border-b-2 -mb-px"
        :class="
          activeTab === tab.key
            ? 'text-primary border-primary'
            : 'text-text-secondary border-transparent hover:text-text-primary hover:border-surface-3'
        "
        @click="selectTab(tab.slug)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <!-- Loading skeleton -->
    <div v-if="loading" class="space-y-4">
      <div class="h-6 w-48 bg-surface-2 rounded animate-pulse" />
      <div class="h-4 w-full bg-surface-2 rounded animate-pulse" />
      <div class="h-4 w-3/4 bg-surface-2 rounded animate-pulse" />
      <div class="h-4 w-5/6 bg-surface-2 rounded animate-pulse" />
    </div>

    <!-- Content -->
    <article v-else-if="doc" class="legal-prose">
      <div v-if="doc.version" class="text-xs text-text-muted mb-4">
        Version {{ doc.version }} — Stand:
        {{ new Date(doc.updated_at).toLocaleDateString("de-DE") }}
      </div>
      <div v-html="doc.content_html" />
    </article>

    <div v-else class="text-text-muted text-sm">Dokument nicht gefunden.</div>
  </main>
</template>

<style scoped>
.legal-prose :deep(h2) {
  @apply text-xl font-bold text-text-primary mt-8 mb-3;
}
.legal-prose :deep(h3) {
  @apply text-lg font-semibold text-text-primary mt-6 mb-2;
}
.legal-prose :deep(p) {
  @apply text-sm text-text-secondary leading-relaxed mb-3;
}
.legal-prose :deep(ul) {
  @apply list-disc list-inside text-sm text-text-secondary mb-3 space-y-1;
}
.legal-prose :deep(ol) {
  @apply list-decimal list-inside text-sm text-text-secondary mb-3 space-y-1;
}
.legal-prose :deep(a) {
  @apply text-secondary underline;
}
.legal-prose :deep(strong) {
  @apply text-text-primary font-semibold;
}
</style>
