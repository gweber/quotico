<script setup lang="ts">
import { ref, watch, onMounted } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useSquadsStore } from "@/stores/squads";
import { useToast } from "@/composables/useToast";

const { t } = useI18n();
const router = useRouter();
const squads = useSquadsStore();
const toast = useToast();

const error = ref(false);
const showCreateModal = ref(false);
const showJoinModal = ref(false);
const createName = ref("");
const createDesc = ref("");
const joinCode = ref("");
const submitting = ref(false);

// Public squads
const searchQuery = ref("");
const requestedSquadIds = ref(new Set<string>());
const requestingId = ref<string | null>(null);
let searchTimeout: ReturnType<typeof setTimeout> | null = null;

watch(searchQuery, (q) => {
  if (searchTimeout) clearTimeout(searchTimeout);
  searchTimeout = setTimeout(() => {
    squads.fetchPublicSquads(q.trim());
  }, 300);
});

async function reload() {
  error.value = false;
  try {
    await Promise.all([squads.fetchMySquads(), squads.fetchPublicSquads()]);
  } catch {
    error.value = true;
  }
}

onMounted(() => reload());

async function handleCreate() {
  if (!createName.value.trim()) return;
  submitting.value = true;
  try {
    const squad = await squads.createSquad(createName.value.trim(), createDesc.value.trim() || undefined);
    showCreateModal.value = false;
    createName.value = "";
    createDesc.value = "";
    router.push(`/squads/${squad.id}`);
  } catch (e: any) {
    toast.error(e.message || t('squads.createError'));
  } finally {
    submitting.value = false;
  }
}

async function handleJoin() {
  if (!joinCode.value.trim()) return;
  submitting.value = true;
  try {
    const squad = await squads.joinSquad(joinCode.value.trim());
    showJoinModal.value = false;
    joinCode.value = "";
    router.push(`/squads/${squad.id}`);
  } catch (e: any) {
    toast.error(e.message || t('squads.invalidCode'));
  } finally {
    submitting.value = false;
  }
}

async function handleRequestJoin(squadId: string) {
  requestingId.value = squadId;
  try {
    await squads.requestJoin(squadId);
    requestedSquadIds.value.add(squadId);
  } catch (e: any) {
    toast.error(e.message || t('squads.requestFailed'));
  } finally {
    requestingId.value = null;
  }
}
</script>

<template>
  <div class="max-w-2xl mx-auto p-4">
    <div class="flex items-center justify-between mb-6">
      <h1 class="text-xl font-bold text-text-primary">{{ $t('squads.mySquads') }}</h1>
      <div class="flex gap-2">
        <button
          class="px-4 py-2 text-sm rounded-lg bg-surface-2 text-text-primary hover:bg-surface-3 transition-colors"
          @click="showJoinModal = true"
        >
          {{ $t('squads.join') }}
        </button>
        <button
          class="px-4 py-2 text-sm rounded-lg bg-primary text-surface-0 hover:bg-primary-hover transition-colors"
          @click="showCreateModal = true"
        >
          {{ $t('squads.create') }}
        </button>
      </div>
    </div>

    <!-- Loading -->
    <div v-if="squads.loading" class="space-y-3">
      <div v-for="n in 3" :key="n" class="bg-surface-1 rounded-card h-24 animate-pulse" />
    </div>

    <!-- Error -->
    <div v-else-if="error" class="text-center py-12">
      <p class="text-text-muted mb-3">{{ $t('common.loadError') }}</p>
      <button class="text-sm text-primary hover:underline" @click="reload">{{ $t('common.retry') }}</button>
    </div>

    <!-- Empty -->
    <div v-else-if="squads.squads.length === 0" class="text-center py-16">
      <p class="text-4xl mb-4" aria-hidden="true">&#x1F465;</p>
      <h2 class="text-lg font-semibold text-text-primary mb-2">{{ $t('squads.noSquads') }}</h2>
      <p class="text-sm text-text-secondary mb-6">
        {{ $t('squads.noSquadsDescription') }}
      </p>
      <div class="flex gap-3 justify-center">
        <button
          class="px-4 py-2 text-sm rounded-lg bg-surface-2 text-text-primary hover:bg-surface-3 transition-colors"
          @click="showJoinModal = true"
        >
          {{ $t('squads.enterCode') }}
        </button>
        <button
          class="px-4 py-2 text-sm rounded-lg bg-primary text-surface-0 hover:bg-primary-hover transition-colors"
          @click="showCreateModal = true"
        >
          {{ $t('squads.createSquad') }}
        </button>
      </div>
    </div>

    <!-- Squad Cards -->
    <div v-else class="space-y-3">
      <RouterLink
        v-for="squad in squads.squads"
        :key="squad.id"
        :to="`/squads/${squad.id}`"
        class="block bg-surface-1 rounded-card p-4 border border-surface-3/50 hover:border-surface-3 transition-colors"
      >
        <div class="flex items-center justify-between">
          <div>
            <h3 class="text-sm font-semibold text-text-primary">{{ squad.name }}</h3>
            <p v-if="squad.description" class="text-xs text-text-muted mt-0.5">{{ squad.description }}</p>
          </div>
          <div class="text-right shrink-0">
            <span class="text-xs text-text-muted">{{ squad.member_count }} {{ $t('squads.members') }}</span>
            <span
              v-if="squad.is_admin"
              class="block text-xs text-primary font-medium"
            >{{ $t('squads.admin') }}</span>
          </div>
        </div>
      </RouterLink>
    </div>

    <!-- Public Squads -->
    <div class="mt-10">
      <h2 class="text-lg font-bold text-text-primary mb-4">{{ $t('squads.discover') }}</h2>

      <!-- Search -->
      <input
        v-model="searchQuery"
        type="text"
        placeholder="Squad suchen..."
        class="w-full bg-surface-1 border border-surface-3/50 rounded-lg px-4 py-2.5 text-sm text-text-primary placeholder:text-text-muted/50 focus:outline-none focus:ring-1 focus:ring-primary mb-4"
      />

      <!-- Loading -->
      <div v-if="squads.publicLoading" class="space-y-3">
        <div v-for="n in 3" :key="n" class="bg-surface-1 rounded-card h-16 animate-pulse" />
      </div>

      <!-- Results -->
      <div v-else-if="squads.publicSquads.length > 0" class="space-y-3">
        <div
          v-for="ps in squads.publicSquads"
          :key="ps.id"
          class="bg-surface-1 rounded-card p-4 border border-surface-3/50 flex items-center justify-between"
        >
          <div class="min-w-0 flex-1">
            <h3 class="text-sm font-semibold text-text-primary">{{ ps.name }}</h3>
            <p v-if="ps.description" class="text-xs text-text-muted mt-0.5 truncate">{{ ps.description }}</p>
            <span class="text-xs text-text-muted">{{ ps.member_count }} {{ $t('squads.members') }}</span>
          </div>
          <div class="shrink-0 ml-4">
            <button
              v-if="ps.is_open && !requestedSquadIds.has(ps.id)"
              class="px-3 py-1.5 text-xs font-medium rounded-lg bg-primary/10 text-primary border border-primary/30 hover:bg-primary/20 transition-colors disabled:opacity-50"
              :disabled="requestingId === ps.id"
              @click="handleRequestJoin(ps.id)"
            >
              {{ requestingId === ps.id ? "..." : "Anfrage senden" }}
            </button>
            <span
              v-else-if="requestedSquadIds.has(ps.id)"
              class="px-3 py-1.5 text-xs font-medium rounded-lg bg-success/10 text-success border border-success/30"
            >
              Angefragt
            </span>
            <span
              v-else
              class="px-3 py-1.5 text-xs font-medium rounded-lg bg-surface-2 text-text-muted border border-surface-3"
            >
              Geschlossen
            </span>
          </div>
        </div>
      </div>

      <!-- Empty -->
      <div v-else class="text-center py-8">
        <p class="text-sm text-text-muted">
          {{ searchQuery.trim() ? $t('squads.noResults') : $t('squads.noPublic') }}
        </p>
      </div>
    </div>

    <!-- Create Modal -->
    <Teleport to="body">
      <Transition name="fade">
        <div
          v-if="showCreateModal"
          class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60"
          @click.self="showCreateModal = false"
        >
          <div class="bg-surface-1 rounded-card p-6 w-full max-w-sm border border-surface-3">
            <h2 class="text-lg font-semibold text-text-primary mb-4">{{ $t('squads.createSquad') }}</h2>
            <form @submit.prevent="handleCreate" class="space-y-4">
              <div>
                <label class="block text-sm text-text-secondary mb-1">{{ $t('squads.name') }}</label>
                <input
                  v-model="createName"
                  type="text"
                  maxlength="40"
                  class="w-full px-3 py-2 rounded-lg bg-surface-2 border border-surface-3 text-text-primary text-sm focus:ring-2 focus:ring-primary focus:outline-none"
                  placeholder="z.B. BVB Stammtisch"
                  required
                />
              </div>
              <div>
                <label class="block text-sm text-text-secondary mb-1">{{ $t('squads.descriptionOptional') }}</label>
                <input
                  v-model="createDesc"
                  type="text"
                  maxlength="120"
                  class="w-full px-3 py-2 rounded-lg bg-surface-2 border border-surface-3 text-text-primary text-sm focus:ring-2 focus:ring-primary focus:outline-none"
                  placeholder="Kurze Beschreibung..."
                />
              </div>
              <div class="flex gap-2 justify-end">
                <button
                  type="button"
                  class="px-4 py-2 text-sm rounded-lg text-text-secondary hover:bg-surface-2 transition-colors"
                  @click="showCreateModal = false"
                >{{ $t('common.cancel') }}</button>
                <button
                  type="submit"
                  :disabled="submitting || !createName.trim()"
                  class="px-4 py-2 text-sm rounded-lg bg-primary text-surface-0 hover:bg-primary-hover transition-colors disabled:opacity-50"
                >{{ submitting ? "..." : $t('squads.create') }}</button>
              </div>
            </form>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- Join Modal -->
    <Teleport to="body">
      <Transition name="fade">
        <div
          v-if="showJoinModal"
          class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60"
          @click.self="showJoinModal = false"
        >
          <div class="bg-surface-1 rounded-card p-6 w-full max-w-sm border border-surface-3">
            <h2 class="text-lg font-semibold text-text-primary mb-4">{{ $t('squads.joinSquad') }}</h2>
            <form @submit.prevent="handleJoin" class="space-y-4">
              <div>
                <label class="block text-sm text-text-secondary mb-1">{{ $t('squads.inviteCode') }}</label>
                <input
                  v-model="joinCode"
                  type="text"
                  class="w-full px-3 py-2 rounded-lg bg-surface-2 border border-surface-3 text-text-primary text-sm font-mono tracking-wider text-center focus:ring-2 focus:ring-primary focus:outline-none"
                  placeholder="QUO-123-AB"
                  required
                />
              </div>
              <div class="flex gap-2 justify-end">
                <button
                  type="button"
                  class="px-4 py-2 text-sm rounded-lg text-text-secondary hover:bg-surface-2 transition-colors"
                  @click="showJoinModal = false"
                >{{ $t('common.cancel') }}</button>
                <button
                  type="submit"
                  :disabled="submitting || !joinCode.trim()"
                  class="px-4 py-2 text-sm rounded-lg bg-primary text-surface-0 hover:bg-primary-hover transition-colors disabled:opacity-50"
                >{{ submitting ? "..." : $t('squads.join') }}</button>
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
