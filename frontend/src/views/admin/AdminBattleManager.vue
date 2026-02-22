<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

const api = useApi();
const toast = useToast();

interface AdminSquad {
  id: string;
  name: string;
  member_count: number;
  invite_code: string;
}

const squads = ref<AdminSquad[]>([]);
const loading = ref(true);

// Battle creation
const showCreate = ref(false);
const squadAId = ref("");
const squadBId = ref("");
const startTime = ref("");
const endTime = ref("");
const submitting = ref(false);

onMounted(async () => {
  try {
    squads.value = await api.get<AdminSquad[]>("/admin/squads");
  } finally {
    loading.value = false;
  }
});

async function createBattle() {
  if (!squadAId.value || !squadBId.value || !startTime.value || !endTime.value) return;
  submitting.value = true;
  try {
    const result = await api.post<{ message: string }>("/admin/battles", {
      squad_a_id: squadAId.value,
      squad_b_id: squadBId.value,
      start_time: new Date(startTime.value).toISOString(),
      end_time: new Date(endTime.value).toISOString(),
    });
    toast.success(result.message);
    showCreate.value = false;
    squadAId.value = "";
    squadBId.value = "";
    startTime.value = "";
    endTime.value = "";
  } catch (e: any) {
    toast.error(e.message);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="max-w-3xl mx-auto p-4">
    <div class="flex items-center justify-between mb-6">
      <div>
        <RouterLink to="/admin" class="text-xs text-text-muted hover:text-text-primary">&larr; Dashboard</RouterLink>
        <h1 class="text-xl font-bold text-text-primary">Battle Management</h1>
      </div>
      <button
        class="px-4 py-2 text-sm rounded-lg bg-primary text-surface-0 hover:bg-primary-hover transition-colors"
        @click="showCreate = true"
      >
        Battle erstellen
      </button>
    </div>

    <!-- Squads Overview -->
    <div class="bg-surface-1 rounded-card border border-surface-3/50 mb-6">
      <h2 class="text-sm font-semibold text-text-primary px-5 py-3 border-b border-surface-3/50">
        Alle Squads ({{ squads.length }})
      </h2>
      <div v-if="loading" class="px-5 py-8 text-center text-text-muted">Laden...</div>
      <table v-else class="w-full text-sm">
        <thead>
          <tr class="text-xs text-text-muted border-b border-surface-3/30">
            <th class="text-left px-5 py-2 font-medium">Name</th>
            <th class="text-right px-5 py-2 font-medium">Mitglieder</th>
            <th class="text-right px-5 py-2 font-medium">Code</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="s in squads"
            :key="s.id"
            class="border-b border-surface-3/20 last:border-0"
          >
            <td class="px-5 py-3 text-text-primary">{{ s.name }}</td>
            <td class="px-5 py-3 text-right tabular-nums text-text-muted">{{ s.member_count }}</td>
            <td class="px-5 py-3 text-right font-mono text-xs text-text-muted">{{ s.invite_code }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Create Battle Modal -->
    <Teleport to="body">
      <Transition name="fade">
        <div
          v-if="showCreate"
          class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60"
          @click.self="showCreate = false"
        >
          <div class="bg-surface-1 rounded-card p-6 w-full max-w-md border border-surface-3">
            <h2 class="text-lg font-semibold text-text-primary mb-4">Battle erstellen</h2>
            <form @submit.prevent="createBattle" class="space-y-4">
              <div>
                <label class="block text-sm text-text-secondary mb-1">Squad A</label>
                <select
                  v-model="squadAId"
                  class="w-full px-3 py-2 rounded-lg bg-surface-2 border border-surface-3 text-text-primary text-sm focus:ring-2 focus:ring-primary focus:outline-none"
                  required
                >
                  <option value="" disabled>Squad auswählen...</option>
                  <option v-for="s in squads" :key="s.id" :value="s.id" :disabled="s.id === squadBId">
                    {{ s.name }} ({{ s.member_count }})
                  </option>
                </select>
              </div>
              <div>
                <label class="block text-sm text-text-secondary mb-1">Squad B</label>
                <select
                  v-model="squadBId"
                  class="w-full px-3 py-2 rounded-lg bg-surface-2 border border-surface-3 text-text-primary text-sm focus:ring-2 focus:ring-primary focus:outline-none"
                  required
                >
                  <option value="" disabled>Squad auswählen...</option>
                  <option v-for="s in squads" :key="s.id" :value="s.id" :disabled="s.id === squadAId">
                    {{ s.name }} ({{ s.member_count }})
                  </option>
                </select>
              </div>
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="block text-sm text-text-secondary mb-1">Start</label>
                  <input
                    v-model="startTime"
                    type="datetime-local"
                    class="w-full px-3 py-2 rounded-lg bg-surface-2 border border-surface-3 text-text-primary text-sm focus:ring-2 focus:ring-primary focus:outline-none"
                    required
                  />
                </div>
                <div>
                  <label class="block text-sm text-text-secondary mb-1">Ende</label>
                  <input
                    v-model="endTime"
                    type="datetime-local"
                    class="w-full px-3 py-2 rounded-lg bg-surface-2 border border-surface-3 text-text-primary text-sm focus:ring-2 focus:ring-primary focus:outline-none"
                    required
                  />
                </div>
              </div>
              <div class="flex gap-2 justify-end">
                <button
                  type="button"
                  class="px-4 py-2 text-sm rounded-lg text-text-secondary hover:bg-surface-2"
                  @click="showCreate = false"
                >Abbrechen</button>
                <button
                  type="submit"
                  :disabled="submitting || !squadAId || !squadBId"
                  class="px-4 py-2 text-sm rounded-lg bg-primary text-surface-0 hover:bg-primary-hover disabled:opacity-50"
                >{{ submitting ? "..." : "Erstellen" }}</button>
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
