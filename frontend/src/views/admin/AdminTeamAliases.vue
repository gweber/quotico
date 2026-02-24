<script setup lang="ts">
import { ref, onMounted, watch } from "vue";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

const api = useApi();
const toast = useToast();

interface AliasEntry {
  id: string;
  sport_key: string;
  team_name: string;
  team_key: string;
  canonical_name: string | null;
}

interface CanonicalEntry {
  id: string;
  provider_name: string;
  canonical_name: string;
  team_key: string;
  source: "seed" | "manual";
}

// State
const aliases = ref<AliasEntry[]>([]);
const canonicalEntries = ref<CanonicalEntry[]>([]);
const aliasTotal = ref(0);
const canonicalTotal = ref(0);
const loading = ref(true);
const error = ref(false);
const search = ref("");
const sportKeyFilter = ref("");
const sourceFilter = ref("");
const aliasPage = ref(0);
const canonicalPage = ref(0);
const tab = ref<"db" | "canonical">("canonical");
const PAGE_SIZE = 50;

// Edit state (shared for both tabs)
const editingId = ref<string | null>(null);
const editValue = ref("");

// Create state
const showCreate = ref(false);
const createMode = ref<"alias" | "canonical">("canonical");
const newAlias = ref({ sport_key: "", team_name: "", team_key: "" });
const newCanonical = ref({ provider_name: "", canonical_name: "" });
const reseeding = ref(false);

const sportKeys = [
  { value: "", label: "Alle Ligen" },
  { value: "soccer_germany_bundesliga", label: "Bundesliga" },
  { value: "soccer_germany_bundesliga2", label: "2. Bundesliga" },
  { value: "soccer_epl", label: "Premier League" },
  { value: "soccer_spain_la_liga", label: "La Liga" },
  { value: "soccer_italy_serie_a", label: "Serie A" },
  { value: "soccer_france_ligue_one", label: "Ligue 1" },
  { value: "soccer_netherlands_eredivisie", label: "Eredivisie" },
  { value: "soccer_portugal_primeira_liga", label: "Primeira Liga" },
];

async function fetchAliases() {
  loading.value = true;
  error.value = false;
  try {
    const params = new URLSearchParams();
    if (search.value) params.set("search", search.value);
    if (sportKeyFilter.value) params.set("sport_key", sportKeyFilter.value);
    params.set("limit", String(PAGE_SIZE));
    params.set("offset", String(aliasPage.value * PAGE_SIZE));
    const result = await api.get<{ total: number; items: AliasEntry[] }>(
      `/admin/team-aliases?${params}`
    );
    aliases.value = result.items;
    aliasTotal.value = result.total;
  } catch {
    error.value = true;
  } finally {
    loading.value = false;
  }
}

async function fetchCanonical() {
  loading.value = true;
  error.value = false;
  try {
    const params = new URLSearchParams();
    if (search.value) params.set("search", search.value);
    if (sourceFilter.value) params.set("source", sourceFilter.value);
    params.set("limit", String(PAGE_SIZE));
    params.set("offset", String(canonicalPage.value * PAGE_SIZE));
    const result = await api.get<{ total: number; items: CanonicalEntry[] }>(
      `/admin/team-aliases/canonical-map?${params}`
    );
    canonicalEntries.value = result.items;
    canonicalTotal.value = result.total;
  } catch {
    error.value = true;
  } finally {
    loading.value = false;
  }
}

function startEdit(id: string, value: string) {
  editingId.value = id;
  editValue.value = value;
}

function cancelEdit() {
  editingId.value = null;
  editValue.value = "";
}

// --- Alias CRUD ---
async function saveAliasEdit(alias: AliasEntry) {
  try {
    await api.put(`/admin/team-aliases/${alias.id}`, {
      team_key: editValue.value,
    });
    toast.success(`Alias aktualisiert: ${alias.team_name}`);
    cancelEdit();
    await fetchAliases();
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : "Fehler beim Speichern");
  }
}

async function deleteAlias(alias: AliasEntry) {
  if (!confirm(`Alias "${alias.team_name}" wirklich löschen?`)) return;
  try {
    await api.del(`/admin/team-aliases/${alias.id}`);
    toast.success(`Alias gelöscht: ${alias.team_name}`);
    await fetchAliases();
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : "Fehler beim Löschen");
  }
}

async function createAlias() {
  try {
    await api.post("/admin/team-aliases", newAlias.value);
    toast.success(`Alias erstellt: ${newAlias.value.team_name}`);
    showCreate.value = false;
    newAlias.value = { sport_key: "", team_name: "", team_key: "" };
    await fetchAliases();
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : "Fehler beim Erstellen");
  }
}

// --- Canonical Map CRUD ---
async function saveCanonicalEdit(entry: CanonicalEntry) {
  try {
    await api.put(`/admin/team-aliases/canonical-map/${entry.id}`, {
      canonical_name: editValue.value,
    });
    toast.success(`Aktualisiert: ${entry.provider_name}`);
    cancelEdit();
    await fetchCanonical();
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : "Fehler beim Speichern");
  }
}

async function deleteCanonical(entry: CanonicalEntry) {
  if (!confirm(`"${entry.provider_name}" wirklich löschen?`)) return;
  try {
    await api.del(`/admin/team-aliases/canonical-map/${entry.id}`);
    toast.success(`Gelöscht: ${entry.provider_name}`);
    await fetchCanonical();
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : "Fehler beim Löschen");
  }
}

async function createCanonical() {
  try {
    await api.post(
      "/admin/team-aliases/canonical-map",
      newCanonical.value
    );
    toast.success(`Erstellt: ${newCanonical.value.provider_name}`);
    showCreate.value = false;
    newCanonical.value = { provider_name: "", canonical_name: "" };
    await fetchCanonical();
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : "Fehler beim Erstellen");
  }
}

async function reseedCanonical() {
  reseeding.value = true;
  try {
    const result = await api.post<{ message: string }>(
      "/admin/team-aliases/canonical-map/reseed"
    );
    toast.success(result.message);
    await fetchCanonical();
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : "Fehler beim Reseed");
  } finally {
    reseeding.value = false;
  }
}

function sportLabel(key: string): string {
  return sportKeys.find((s) => s.value === key)?.label ?? key;
}

function fetchCurrent() {
  if (tab.value === "db") fetchAliases();
  else fetchCanonical();
}

watch([search, sportKeyFilter, sourceFilter], () => {
  aliasPage.value = 0;
  canonicalPage.value = 0;
  fetchCurrent();
});

watch(tab, () => {
  cancelEdit();
  fetchCurrent();
});
watch(aliasPage, fetchAliases);
watch(canonicalPage, fetchCanonical);

onMounted(() => {
  fetchCanonical();
});
</script>

<template>
  <div class="max-w-5xl mx-auto p-4">
    <!-- Header -->
    <div class="flex items-center justify-between mb-4">
      <h1 class="text-xl font-bold text-text-primary">Team Aliases</h1>
      <div class="flex gap-2">
        <button
          v-if="tab === 'canonical'"
          class="px-3 py-1.5 text-sm rounded-lg border border-surface-3 text-text-secondary hover:bg-surface-2 transition-colors disabled:opacity-50"
          :disabled="reseeding"
          @click="reseedCanonical"
        >
          {{ reseeding ? "Seeding..." : "Reseed" }}
        </button>
        <button
          class="px-3 py-1.5 text-sm rounded-lg bg-primary text-white hover:bg-primary/90 transition-colors"
          @click="
            showCreate = !showCreate;
            createMode = tab === 'db' ? 'alias' : 'canonical';
          "
        >
          + Neu
        </button>
      </div>
    </div>

    <!-- Help box -->
    <div class="bg-surface-1 rounded-card border border-surface-3/50 p-4 mb-6 text-xs text-text-muted leading-relaxed space-y-2">
      <p class="text-sm font-medium text-text-secondary">Wie funktioniert Team-Matching?</p>
      <p>
        Verschiedene Daten-Provider liefern unterschiedliche Teamnamen
        (z.B. OddsAPI: <span class="text-text-primary font-mono">"Manchester City"</span>,
        football-data.org: <span class="text-text-primary font-mono">"Manchester City FC"</span>,
        OpenLigaDB: <span class="text-text-primary font-mono">"Manchester City"</span>).
        Damit H2H-Statistiken und historische Daten korrekt zugeordnet werden, müssen alle Varianten
        auf denselben internen <span class="font-mono text-text-secondary">DB-Key</span> abgebildet werden.
      </p>
      <p>
        <span class="text-text-secondary font-medium">Canonical Map</span> &mdash;
        Globale Zuordnung: Provider-Name &rarr; Kurzname (z.B. <span class="font-mono">"FC Bayern München"</span> &rarr; <span class="font-mono">"Bayern Munich"</span>).
        Der Kurzname wird automatisch in den DB-Key umgewandelt. Hat höchste Priorität beim Matching.
        <span class="text-text-secondary">Nicht sichtbar für Nutzer</span> &mdash; auf der Seite wird weiterhin der Original-Provider-Name angezeigt.
      </p>
      <p>
        <span class="text-text-secondary font-medium">DB Aliases</span> &mdash;
        Liga-spezifische Zuordnung: Provider-Name + Liga &rarr; DB-Key.
        Wird automatisch vom Fuzzy-Matcher erstellt, wenn ein neuer Teamname erkannt wird.
        Kann manuell korrigiert werden, falls der Matcher falsch geraten hat.
      </p>
    </div>

    <!-- Create form: Canonical -->
    <div
      v-if="showCreate && createMode === 'canonical'"
      class="bg-surface-1 rounded-card border border-surface-3/50 p-4 mb-6"
    >
      <h2 class="text-sm font-semibold text-text-primary mb-3">
        Neues Canonical Mapping
      </h2>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
        <input
          v-model="newCanonical.provider_name"
          placeholder="Provider-Name (z.B. FC Bayern München)"
          class="bg-surface-2 text-text-primary text-sm rounded-lg px-3 py-2 border border-surface-3"
        />
        <input
          v-model="newCanonical.canonical_name"
          placeholder="Canonical Name (z.B. Bayern Munich)"
          class="bg-surface-2 text-text-primary text-sm rounded-lg px-3 py-2 border border-surface-3"
        />
        <div class="flex gap-2">
          <button
            class="flex-1 px-3 py-2 text-sm rounded-lg bg-primary text-white hover:bg-primary/90"
            :disabled="
              !newCanonical.provider_name || !newCanonical.canonical_name
            "
            @click="createCanonical"
          >
            Erstellen
          </button>
          <button
            class="px-3 py-2 text-sm rounded-lg border border-surface-3 text-text-secondary hover:bg-surface-2"
            @click="showCreate = false"
          >
            Abbrechen
          </button>
        </div>
      </div>
    </div>

    <!-- Create form: Alias -->
    <div
      v-if="showCreate && createMode === 'alias'"
      class="bg-surface-1 rounded-card border border-surface-3/50 p-4 mb-6"
    >
      <h2 class="text-sm font-semibold text-text-primary mb-3">
        Neuen DB Alias erstellen
      </h2>
      <div class="grid grid-cols-1 md:grid-cols-4 gap-3">
        <select
          v-model="newAlias.sport_key"
          class="bg-surface-2 text-text-primary text-sm rounded-lg px-3 py-2 border border-surface-3"
        >
          <option value="" disabled>Liga</option>
          <option
            v-for="sk in sportKeys.filter((s) => s.value)"
            :key="sk.value"
            :value="sk.value"
          >
            {{ sk.label }}
          </option>
        </select>
        <input
          v-model="newAlias.team_name"
          placeholder="Provider-Name"
          class="bg-surface-2 text-text-primary text-sm rounded-lg px-3 py-2 border border-surface-3"
        />
        <input
          v-model="newAlias.team_key"
          placeholder="DB-Key"
          class="bg-surface-2 text-text-primary text-sm rounded-lg px-3 py-2 border border-surface-3"
        />
        <div class="flex gap-2">
          <button
            class="flex-1 px-3 py-2 text-sm rounded-lg bg-primary text-white hover:bg-primary/90"
            :disabled="
              !newAlias.sport_key || !newAlias.team_name || !newAlias.team_key
            "
            @click="createAlias"
          >
            Erstellen
          </button>
          <button
            class="px-3 py-2 text-sm rounded-lg border border-surface-3 text-text-secondary hover:bg-surface-2"
            @click="showCreate = false"
          >
            Abbrechen
          </button>
        </div>
      </div>
    </div>

    <!-- Tabs -->
    <div class="flex gap-1 mb-4 bg-surface-1 rounded-lg p-1 w-fit">
      <button
        class="px-4 py-1.5 text-sm rounded-md transition-colors"
        :class="
          tab === 'canonical'
            ? 'bg-primary text-white'
            : 'text-text-secondary hover:text-text-primary'
        "
        @click="tab = 'canonical'"
      >
        Canonical Map ({{ canonicalTotal }})
      </button>
      <button
        class="px-4 py-1.5 text-sm rounded-md transition-colors"
        :class="
          tab === 'db'
            ? 'bg-primary text-white'
            : 'text-text-secondary hover:text-text-primary'
        "
        @click="tab = 'db'"
      >
        DB Aliases ({{ aliasTotal }})
      </button>
    </div>

    <!-- Error -->
    <div v-if="error" class="text-center py-12">
      <p class="text-text-muted mb-3">Error loading.</p>
      <button class="text-sm text-primary hover:underline" @click="fetchCurrent">Try again</button>
    </div>

    <!-- Filters -->
    <div v-if="!error" class="flex gap-3 mb-4">
      <input
        v-model="search"
        placeholder="Suche (Name oder Key)..."
        class="flex-1 bg-surface-1 text-text-primary text-sm rounded-lg px-3 py-2 border border-surface-3 focus:border-primary focus:outline-none"
      />
      <select
        v-if="tab === 'canonical'"
        v-model="sourceFilter"
        class="bg-surface-1 text-text-primary text-sm rounded-lg px-3 py-2 border border-surface-3"
      >
        <option value="">Alle</option>
        <option value="seed">Seed</option>
        <option value="manual">Manual</option>
      </select>
      <select
        v-if="tab === 'db'"
        v-model="sportKeyFilter"
        class="bg-surface-1 text-text-primary text-sm rounded-lg px-3 py-2 border border-surface-3"
      >
        <option v-for="sk in sportKeys" :key="sk.value" :value="sk.value">
          {{ sk.label }}
        </option>
      </select>
    </div>

    <!-- Canonical Map Table -->
    <div
      v-if="tab === 'canonical' && !error"
      class="bg-surface-1 rounded-card border border-surface-3/50 overflow-hidden"
    >
      <div class="px-4 py-3 border-b border-surface-3/30">
        <p class="text-xs text-text-muted">
          Höchste Priorität. Provider-Name &rarr; Kurzname &rarr; DB-Key. Beim Bearbeiten den Kurzname ändern (z.B. <span class="font-mono">"Man City"</span>), der DB-Key wird automatisch berechnet.
        </p>
      </div>
      <div v-if="loading" class="p-8 text-center text-text-muted">
        Laden...
      </div>
      <div
        v-else-if="canonicalEntries.length === 0"
        class="p-8 text-center text-text-muted"
      >
        Keine Einträge gefunden.
      </div>
      <table v-else class="w-full text-sm">
        <thead>
          <tr
            class="text-left text-xs text-text-muted border-b border-surface-3/30"
          >
            <th class="px-4 py-2 font-medium">Provider-Name</th>
            <th class="px-4 py-2 font-medium">Canonical Name</th>
            <th class="px-4 py-2 font-medium">DB-Key</th>
            <th class="px-4 py-2 font-medium text-right">Aktionen</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="e in canonicalEntries"
            :key="e.id"
            class="border-b border-surface-3/20 last:border-0"
          >
            <td class="px-4 py-2 text-text-primary">
              {{ e.provider_name }}
              <span
                v-if="e.source === 'manual'"
                class="ml-1.5 text-[10px] px-1.5 py-0.5 rounded bg-warning/20 text-warning font-medium"
              >manual</span>
            </td>
            <td class="px-4 py-2">
              <template v-if="editingId === e.id">
                <input
                  v-model="editValue"
                  class="bg-surface-2 text-text-primary text-sm rounded px-2 py-1 border border-primary w-full"
                  @keyup.enter="saveCanonicalEdit(e)"
                  @keyup.escape="cancelEdit"
                />
              </template>
              <span v-else class="text-text-secondary">{{
                e.canonical_name
              }}</span>
            </td>
            <td class="px-4 py-2 font-mono text-text-muted">
              {{ e.team_key }}
            </td>
            <td class="px-4 py-2 text-right">
              <template v-if="editingId === e.id">
                <button
                  class="text-xs text-primary hover:underline mr-2"
                  @click="saveCanonicalEdit(e)"
                >
                  Speichern
                </button>
                <button
                  class="text-xs text-text-muted hover:underline"
                  @click="cancelEdit"
                >
                  Abbrechen
                </button>
              </template>
              <template v-else>
                <button
                  class="text-xs text-primary hover:underline mr-2"
                  @click="startEdit(e.id, e.canonical_name)"
                >
                  Bearbeiten
                </button>
                <button
                  class="text-xs text-danger hover:underline"
                  @click="deleteCanonical(e)"
                >
                  X
                </button>
              </template>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- Pagination -->
      <div
        v-if="canonicalTotal > PAGE_SIZE"
        class="flex items-center justify-between px-4 py-3 border-t border-surface-3/30"
      >
        <span class="text-xs text-text-muted">
          {{ canonicalPage * PAGE_SIZE + 1 }}-{{
            Math.min((canonicalPage + 1) * PAGE_SIZE, canonicalTotal)
          }}
          von {{ canonicalTotal }}
        </span>
        <div class="flex gap-2">
          <button
            class="px-3 py-1 text-xs rounded border border-surface-3 text-text-secondary hover:bg-surface-2 disabled:opacity-50"
            :disabled="canonicalPage === 0"
            @click="canonicalPage--"
          >
            Zurück
          </button>
          <button
            class="px-3 py-1 text-xs rounded border border-surface-3 text-text-secondary hover:bg-surface-2 disabled:opacity-50"
            :disabled="(canonicalPage + 1) * PAGE_SIZE >= canonicalTotal"
            @click="canonicalPage++"
          >
            Weiter
          </button>
        </div>
      </div>
    </div>

    <!-- DB Aliases Table -->
    <div
      v-if="tab === 'db' && !error"
      class="bg-surface-1 rounded-card border border-surface-3/50 overflow-hidden"
    >
      <div class="px-4 py-3 border-b border-surface-3/30">
        <p class="text-xs text-text-muted">
          Auto-generiert vom Fuzzy-Matcher. Falsche Zuordnungen hier korrigieren &mdash; den DB-Key auf den richtigen Wert setzen. Einträge mit falschem Key führen zu fehlenden H2H-Daten.
        </p>
      </div>
      <div v-if="loading" class="p-8 text-center text-text-muted">
        Laden...
      </div>
      <div
        v-else-if="aliases.length === 0"
        class="p-8 text-center text-text-muted"
      >
        Keine Aliases gefunden.
      </div>
      <table v-else class="w-full text-sm">
        <thead>
          <tr
            class="text-left text-xs text-text-muted border-b border-surface-3/30"
          >
            <th class="px-4 py-2 font-medium">Liga</th>
            <th class="px-4 py-2 font-medium">Provider-Name</th>
            <th class="px-4 py-2 font-medium">DB-Key</th>
            <th class="px-4 py-2 font-medium text-right">Aktionen</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="a in aliases"
            :key="a.id"
            class="border-b border-surface-3/20 last:border-0"
          >
            <td class="px-4 py-2">
              <span
                class="text-[10px] px-1.5 py-0.5 rounded bg-surface-3 text-text-muted font-mono"
              >
                {{ sportLabel(a.sport_key) }}
              </span>
            </td>
            <td class="px-4 py-2 text-text-primary">{{ a.team_name }}</td>
            <td class="px-4 py-2">
              <template v-if="editingId === a.id">
                <input
                  v-model="editValue"
                  class="bg-surface-2 text-text-primary text-sm rounded px-2 py-1 border border-primary w-full"
                  @keyup.enter="saveAliasEdit(a)"
                  @keyup.escape="cancelEdit"
                />
              </template>
              <span v-else class="font-mono text-text-secondary">{{
                a.team_key
              }}</span>
            </td>
            <td class="px-4 py-2 text-right">
              <template v-if="editingId === a.id">
                <button
                  class="text-xs text-primary hover:underline mr-2"
                  @click="saveAliasEdit(a)"
                >
                  Speichern
                </button>
                <button
                  class="text-xs text-text-muted hover:underline"
                  @click="cancelEdit"
                >
                  Abbrechen
                </button>
              </template>
              <template v-else>
                <button
                  class="text-xs text-primary hover:underline mr-2"
                  @click="startEdit(a.id, a.team_key)"
                >
                  Bearbeiten
                </button>
                <button
                  class="text-xs text-danger hover:underline"
                  @click="deleteAlias(a)"
                >
                  X
                </button>
              </template>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- Pagination -->
      <div
        v-if="aliasTotal > PAGE_SIZE"
        class="flex items-center justify-between px-4 py-3 border-t border-surface-3/30"
      >
        <span class="text-xs text-text-muted">
          {{ aliasPage * PAGE_SIZE + 1 }}-{{
            Math.min((aliasPage + 1) * PAGE_SIZE, aliasTotal)
          }}
          von {{ aliasTotal }}
        </span>
        <div class="flex gap-2">
          <button
            class="px-3 py-1 text-xs rounded border border-surface-3 text-text-secondary hover:bg-surface-2 disabled:opacity-50"
            :disabled="aliasPage === 0"
            @click="aliasPage--"
          >
            Zurück
          </button>
          <button
            class="px-3 py-1 text-xs rounded border border-surface-3 text-text-secondary hover:bg-surface-2 disabled:opacity-50"
            :disabled="(aliasPage + 1) * PAGE_SIZE >= aliasTotal"
            @click="aliasPage++"
          >
            Weiter
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
