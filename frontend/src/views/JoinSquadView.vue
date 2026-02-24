<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useAuthStore } from "@/stores/auth";
import { useSquadsStore } from "@/stores/squads";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const squads = useSquadsStore();
const api = useApi();
const toast = useToast();

const code = (route.params.code as string).toUpperCase();

const preview = ref<{
  name: string;
  description?: string | null;
  member_count: number;
  is_full: boolean;
} | null>(null);
const loading = ref(true);
const joining = ref(false);
const error = ref<string | null>(null);

onMounted(async () => {
  // Persist invite code so it survives login/register/complete-profile redirects
  localStorage.setItem("pendingInvite", code);

  // Fetch public squad preview
  try {
    preview.value = await api.get(`/squads/preview/${code}`);
  } catch {
    error.value = t('joinSquad.invalidLink');
  } finally {
    loading.value = false;
  }

  // If already logged in, auto-join
  if (auth.isLoggedIn && preview.value && !preview.value.is_full) {
    await handleJoin();
  }
});

async function handleJoin() {
  if (joining.value) return;
  joining.value = true;
  try {
    const squad = await squads.joinSquad(code);
    localStorage.removeItem("pendingInvite");
    router.push(`/squads/${squad.id}`);
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : t('joinSquad.joinFailed');
    if (msg.includes("bereits Mitglied")) {
      // Already a member — redirect to squad list
      localStorage.removeItem("pendingInvite");
      toast.success(t('joinSquad.alreadyMember'));
      await squads.fetchMySquads();
      const existing = squads.squads.find(
        (s) => s.invite_code === code
      );
      router.push(existing ? `/squads/${existing.id}` : "/squads");
    } else {
      error.value = msg;
    }
  } finally {
    joining.value = false;
  }
}

const loginUrl = `/login?redirect=/join/${code}`;
const registerUrl = `/register?redirect=/join/${code}`;
</script>

<template>
  <div class="min-h-[60vh] flex items-center justify-center p-4">
    <div class="w-full max-w-sm">
      <!-- Loading -->
      <div v-if="loading" class="text-center space-y-4">
        <div class="h-6 bg-surface-2 rounded animate-pulse w-48 mx-auto" />
        <div class="h-4 bg-surface-2 rounded animate-pulse w-32 mx-auto" />
      </div>

      <!-- Error -->
      <div v-else-if="error && !preview" class="text-center space-y-4">
        <div class="w-12 h-12 rounded-full bg-danger/10 flex items-center justify-center mx-auto">
          <svg class="w-6 h-6 text-danger" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </div>
        <p class="text-sm text-text-muted">{{ error }}</p>
        <RouterLink
          to="/"
          class="inline-block text-sm text-primary hover:underline"
        >{{ $t('joinSquad.backHome') }}</RouterLink>
      </div>

      <!-- Squad Preview -->
      <div v-else-if="preview" class="bg-surface-1 rounded-card p-6 border border-surface-3/50 space-y-5">
        <div class="text-center space-y-2">
          <p class="text-xs text-text-muted uppercase tracking-wider">{{ $t('joinSquad.invitation') }}</p>
          <h1 class="text-xl font-bold text-text-primary">{{ preview.name }}</h1>
          <p v-if="preview.description" class="text-sm text-text-muted">
            {{ preview.description }}
          </p>
          <p class="text-xs text-text-muted">
            {{ preview.member_count }} {{ $t('squads.members') }}
          </p>
        </div>

        <!-- Already logged in: show join button -->
        <template v-if="auth.isLoggedIn">
          <div v-if="preview.is_full" class="text-center">
            <p class="text-sm text-text-muted">{{ $t('joinSquad.squadFull') }}</p>
          </div>
          <div v-else-if="error" class="text-center space-y-3">
            <p class="text-sm text-danger">{{ error }}</p>
          </div>
          <div v-else class="text-center">
            <button
              class="w-full py-3 rounded-lg bg-primary text-surface-0 font-semibold hover:bg-primary/90 transition-colors disabled:opacity-50"
              :disabled="joining"
              @click="handleJoin"
            >
              {{ joining ? $t('joinSquad.joining') : $t('joinSquad.joinButton') }}
            </button>
          </div>
        </template>

        <!-- Not logged in: show auth options -->
        <template v-else>
          <div v-if="preview.is_full" class="text-center">
            <p class="text-sm text-text-muted">{{ $t('joinSquad.squadFull') }}</p>
          </div>
          <div v-else class="space-y-3">
            <RouterLink
              :to="registerUrl"
              class="block w-full py-3 rounded-lg bg-primary text-surface-0 font-semibold text-center hover:bg-primary/90 transition-colors"
            >
              {{ $t('joinSquad.registerAndJoin') }}
            </RouterLink>
            <RouterLink
              :to="loginUrl"
              class="block w-full py-3 rounded-lg bg-surface-2 text-text-primary font-medium text-center hover:bg-surface-3 transition-colors border border-surface-3"
            >
              {{ $t('joinSquad.loginAndJoin') }}
            </RouterLink>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>
