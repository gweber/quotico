<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useRouter } from "vue-router";
import { useSquadsStore } from "@/stores/squads";
import { useToast } from "@/composables/useToast";

const router = useRouter();
const squads = useSquadsStore();
const toast = useToast();

const showCreateModal = ref(false);
const showJoinModal = ref(false);
const createName = ref("");
const createDesc = ref("");
const joinCode = ref("");
const submitting = ref(false);

onMounted(() => {
  squads.fetchMySquads();
});

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
    toast.error(e.message || "Squad konnte nicht erstellt werden.");
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
    toast.error(e.message || "Ungültiger Einladungscode.");
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="max-w-2xl mx-auto p-4">
    <div class="flex items-center justify-between mb-6">
      <h1 class="text-xl font-bold text-text-primary">Meine Squads</h1>
      <div class="flex gap-2">
        <button
          class="px-4 py-2 text-sm rounded-lg bg-surface-2 text-text-primary hover:bg-surface-3 transition-colors"
          @click="showJoinModal = true"
        >
          Beitreten
        </button>
        <button
          class="px-4 py-2 text-sm rounded-lg bg-primary text-surface-0 hover:bg-primary-hover transition-colors"
          @click="showCreateModal = true"
        >
          Erstellen
        </button>
      </div>
    </div>

    <!-- Loading -->
    <div v-if="squads.loading" class="space-y-3">
      <div v-for="n in 3" :key="n" class="bg-surface-1 rounded-card h-24 animate-pulse" />
    </div>

    <!-- Empty -->
    <div v-else-if="squads.squads.length === 0" class="text-center py-16">
      <p class="text-4xl mb-4" aria-hidden="true">&#x1F465;</p>
      <h2 class="text-lg font-semibold text-text-primary mb-2">Noch kein Squad</h2>
      <p class="text-sm text-text-secondary mb-6">
        Erstelle einen Squad oder tritt einem bei, um gemeinsam zu tippen.
      </p>
      <div class="flex gap-3 justify-center">
        <button
          class="px-4 py-2 text-sm rounded-lg bg-surface-2 text-text-primary hover:bg-surface-3 transition-colors"
          @click="showJoinModal = true"
        >
          Code eingeben
        </button>
        <button
          class="px-4 py-2 text-sm rounded-lg bg-primary text-surface-0 hover:bg-primary-hover transition-colors"
          @click="showCreateModal = true"
        >
          Squad erstellen
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
            <span class="text-xs text-text-muted">{{ squad.member_count }} Mitglieder</span>
            <span
              v-if="squad.is_admin"
              class="block text-xs text-primary font-medium"
            >Admin</span>
          </div>
        </div>
      </RouterLink>
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
            <h2 class="text-lg font-semibold text-text-primary mb-4">Squad erstellen</h2>
            <form @submit.prevent="handleCreate" class="space-y-4">
              <div>
                <label class="block text-sm text-text-secondary mb-1">Name</label>
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
                <label class="block text-sm text-text-secondary mb-1">Beschreibung (optional)</label>
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
                >Abbrechen</button>
                <button
                  type="submit"
                  :disabled="submitting || !createName.trim()"
                  class="px-4 py-2 text-sm rounded-lg bg-primary text-surface-0 hover:bg-primary-hover transition-colors disabled:opacity-50"
                >{{ submitting ? "..." : "Erstellen" }}</button>
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
            <h2 class="text-lg font-semibold text-text-primary mb-4">Squad beitreten</h2>
            <form @submit.prevent="handleJoin" class="space-y-4">
              <div>
                <label class="block text-sm text-text-secondary mb-1">Einladungscode</label>
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
                >Abbrechen</button>
                <button
                  type="submit"
                  :disabled="submitting || !joinCode.trim()"
                  class="px-4 py-2 text-sm rounded-lg bg-primary text-surface-0 hover:bg-primary-hover transition-colors disabled:opacity-50"
                >{{ submitting ? "..." : "Beitreten" }}</button>
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
