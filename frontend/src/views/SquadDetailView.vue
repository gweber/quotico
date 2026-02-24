<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useSquadsStore } from "@/stores/squads";
import { useToast } from "@/composables/useToast";
import { useMatchdayStore } from "@/stores/matchday";
import SquadLeagueConfigManager from "@/components/SquadLeagueConfigManager.vue";

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const squads = useSquadsStore();
const matchdayStore = useMatchdayStore();
const toast = useToast();

const squadId = computed(() => route.params.id as string);
const squad = computed(() => squads.squads.find((s) => s.id === squadId.value) ?? null);
const copied = ref(false);
const showLeaveConfirm = ref(false);
const showDeleteConfirm = ref(false);
const togglingAutoBet = ref(false);
const lockMinutesDraft = ref(15);
const savingLockMinutes = ref(false);

async function handleToggleAutoBet() {
  if (!squad.value) return;
  togglingAutoBet.value = true;
  try {
    const newBlocked = !squad.value.auto_bet_blocked;
    await squads.toggleAutoBet(squadId.value, newBlocked);
    toast.success(newBlocked ? t('squadDetail.autoBetBlockedSuccess') : t('squadDetail.autoBetAllowedSuccess'));
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t('common.error'));
  } finally {
    togglingAutoBet.value = false;
  }
}
async function handleSaveLockMinutes() {
  if (!squad.value) return;
  savingLockMinutes.value = true;
  try {
    await squads.setLockMinutes(squadId.value, lockMinutesDraft.value);
    toast.success(t('squadDetail.deadlineSaved', { minutes: lockMinutesDraft.value }));
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t('common.error'));
  } finally {
    savingLockMinutes.value = false;
  }
}

async function handleToggleInviteVisible() {
  if (!squad.value) return;
  try {
    await squads.setInviteVisible(squadId.value, !squad.value.invite_visible);
    toast.success(squad.value.invite_visible ? t('squadDetail.inviteHiddenSuccess') : t('squadDetail.inviteShownSuccess'));
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t('common.error'));
  }
}

async function toggleVisibility() {
  if (!squad.value) return;
  try {
    await squads.setVisibility(squadId.value, !squad.value.is_public);
    toast.success(squad.value.is_public ? t('squadDetail.madePrivate') : t('squadDetail.madePublic'));
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t('common.error'));
  }
}

async function toggleOpen() {
  if (!squad.value) return;
  try {
    await squads.setOpen(squadId.value, !squad.value.is_open);
    toast.success(squad.value.is_open ? t('squadDetail.rejectsRequests') : t('squadDetail.acceptsRequests'));
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t('common.error'));
  }
}

async function handleApprove(requestId: string) {
  try {
    await squads.approveJoinRequest(squadId.value, requestId);
    toast.success(t('squadDetail.joinApproved'));
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t('common.error'));
  }
}

async function handleDecline(requestId: string) {
  try {
    await squads.declineJoinRequest(squadId.value, requestId);
    toast.success(t('squadDetail.requestDeclined'));
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t('common.error'));
  }
}

const error = ref(false);
const editingDescription = ref(false);
const descriptionDraft = ref("");

async function reload() {
  error.value = false;
  try {
    if (squads.squads.length === 0) {
      await squads.fetchMySquads();
    }
    // Fetch matchday sports for league config manager
    if (matchdayStore.sports.length === 0) {
      await matchdayStore.fetchSports();
    }
    if (squadId.value) {
      await squads.fetchLeaderboard(squadId.value);
    }
    if (squad.value) {
      lockMinutesDraft.value = squad.value.lock_minutes ?? 15;
      if (squad.value.is_admin && squad.value.pending_requests > 0) {
        await squads.fetchJoinRequests(squadId.value);
      }
    }
  } catch {
    error.value = true;
  }
}

onMounted(() => reload());

function inviteLink(): string {
  if (!squad.value) return "";
  return `${window.location.origin}/join/${squad.value.invite_code}`;
}

const canNativeShare = typeof navigator !== "undefined" && !!navigator.share;

async function copyLink() {
  await navigator.clipboard.writeText(inviteLink());
  copied.value = true;
  toast.success(t('squadDetail.inviteCopied'));
  setTimeout(() => (copied.value = false), 2000);
}

async function nativeShare() {
  if (!squad.value) return;
  try {
    await navigator.share({
      title: t('squadDetail.shareTitle', { name: squad.value.name }),
      text: t('squadDetail.shareText'),
      url: inviteLink(),
    });
  } catch {
    // User cancelled — ignore
  }
}

async function handleLeave() {
  try {
    await squads.leaveSquad(squadId.value);
    router.push("/squads");
  } catch {
    // Toast shown by store
  }
}

async function handleDelete() {
  try {
    await squads.deleteSquad(squadId.value);
    router.push("/squads");
  } catch {
    // Toast shown by store
  }
}

function startEditDescription() {
  descriptionDraft.value = squad.value?.description ?? "";
  editingDescription.value = true;
}

async function saveDescription() {
  try {
    const desc = descriptionDraft.value.trim() || null;
    await squads.updateSquad(squadId.value, desc);
    editingDescription.value = false;
    toast.success(t('squadDetail.descriptionUpdated'));
  } catch {
    // Toast shown by store
  }
}
</script>

<template>
  <div class="max-w-2xl mx-auto p-4">
    <!-- Back -->
    <RouterLink to="/squads" class="inline-flex items-center gap-1 text-sm text-text-muted hover:text-text-primary mb-4">
      <span aria-hidden="true">&larr;</span> {{ $t('squadDetail.allSquads') }}
    </RouterLink>

    <!-- Error -->
    <div v-if="error" class="text-center py-12">
      <p class="text-text-muted mb-3">{{ $t('common.loadError') }}</p>
      <button class="text-sm text-primary hover:underline" @click="reload">{{ $t('common.retry') }}</button>
    </div>

    <template v-else-if="squad">
      <!-- Header -->
      <div class="bg-surface-1 rounded-card p-5 border border-surface-3/50 mb-4">
        <div class="flex items-start justify-between">
          <div class="flex-1 min-w-0">
            <h1 class="text-xl font-bold text-text-primary">{{ squad.name }}</h1>

            <!-- Description: editable for admin -->
            <div v-if="editingDescription" class="mt-2 space-y-2">
              <textarea
                v-model="descriptionDraft"
                class="w-full bg-surface-2 border border-surface-3 rounded-lg px-3 py-2 text-sm text-text-primary placeholder:text-text-muted resize-none focus:outline-none focus:ring-1 focus:ring-primary"
                rows="2"
                :placeholder="$t('squadDetail.descriptionPlaceholder')"
                maxlength="200"
              />
              <div class="flex items-center gap-2">
                <button
                  class="text-xs px-3 py-1 rounded bg-primary text-surface-0 hover:bg-primary/80 transition-colors"
                  @click="saveDescription"
                >{{ $t('squadDetail.save') }}</button>
                <button
                  class="text-xs text-text-muted hover:text-text-primary"
                  @click="editingDescription = false"
                >{{ $t('common.cancel') }}</button>
              </div>
            </div>
            <div v-else class="mt-1 group">
              <p v-if="squad.description" class="text-sm text-text-muted">
                {{ squad.description }}
                <button
                  v-if="squad.is_admin"
                  class="ml-1 text-text-muted/50 hover:text-text-secondary opacity-0 group-hover:opacity-100 transition-opacity"
                  :title="$t('squadDetail.editDescription')"
                  @click="startEditDescription"
                >
                  <svg class="w-3 h-3 inline" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" /></svg>
                </button>
              </p>
              <button
                v-else-if="squad.is_admin"
                class="text-xs text-text-muted/50 hover:text-text-secondary transition-colors"
                @click="startEditDescription"
              >{{ $t('squadDetail.addDescription') }}</button>
            </div>

            <p class="text-xs text-text-muted mt-2">{{ squad.member_count }} {{ $t('squads.members') }}</p>
          </div>
          <span
            v-if="squad.is_admin"
            class="px-2 py-0.5 text-xs font-medium bg-primary-muted/20 text-primary rounded-full shrink-0"
          >{{ $t('squads.admin') }}</span>
        </div>

        <!-- Invite link (visible to admin, or members if invite_visible) -->
        <div v-if="squad.invite_code" class="mt-4 space-y-2">
          <label class="text-xs text-text-muted">{{ $t('squadDetail.inviteLink') }}</label>
          <div class="flex items-center gap-2">
            <div class="flex-1 bg-surface-2 rounded-lg px-3 py-2.5 text-sm text-text-secondary truncate border border-surface-3">
              {{ inviteLink() }}
            </div>
            <button
              class="shrink-0 px-4 py-2.5 text-sm rounded-lg bg-primary text-surface-0 font-medium hover:bg-primary/90 transition-colors"
              @click="copyLink"
            >
              {{ copied ? $t('squadDetail.copied') : $t('squadDetail.copy') }}
            </button>
            <button
              v-if="canNativeShare"
              class="shrink-0 p-2.5 rounded-lg bg-surface-2 text-text-secondary hover:bg-surface-3 transition-colors border border-surface-3"
              :title="$t('squadDetail.share')"
              @click="nativeShare"
            >
              <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M8.684 13.342C8.886 12.938 9 12.482 9 12c0-.482-.114-.938-.316-1.342m0 2.684a3 3 0 110-2.684m0 2.684l6.632 3.316m-6.632-6l6.632-3.316m0 0a3 3 0 105.367-2.684 3 3 0 00-5.367 2.684zm0 9.316a3 3 0 105.368 2.684 3 3 0 00-5.368-2.684z" />
              </svg>
            </button>
          </div>
          <!-- Invite visibility toggle (admin only) -->
          <div v-if="squad.is_admin" class="flex items-center justify-between pt-1">
            <span class="text-xs text-text-muted">
              {{ squad.invite_visible ? $t('squadDetail.inviteVisibleMembers') : $t('squadDetail.inviteHiddenMembers') }}
            </span>
            <button
              class="text-xs text-text-muted hover:text-text-secondary transition-colors"
              @click="handleToggleInviteVisible"
            >
              {{ squad.invite_visible ? $t('squadDetail.hideFromMembers') : $t('squadDetail.showToMembers') }}
            </button>
          </div>
        </div>

        <!-- War Room link -->
        <div class="mt-4 pt-4 border-t border-surface-3/50">
          <RouterLink
            :to="`/squads/${squadId}/war-room/next`"
            class="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-surface-2 text-text-secondary hover:bg-surface-3 hover:text-text-primary transition-colors text-sm font-medium border border-surface-3"
          >
            <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
            </svg>
            {{ $t('squadDetail.warRoom') }}
          </RouterLink>
        </div>

        <!-- Leave button (non-admin) -->
        <div v-if="!squad.is_admin" class="mt-4 pt-4 border-t border-surface-3/50">
          <button
            v-if="!showLeaveConfirm"
            class="text-xs text-text-muted hover:text-danger transition-colors"
            @click="showLeaveConfirm = true"
          >
            {{ $t('squadDetail.leaveSquad') }}
          </button>
          <div v-else class="flex items-center gap-2">
            <span class="text-xs text-text-muted">{{ $t('squadDetail.confirmLeave') }}</span>
            <button
              class="text-xs px-3 py-1 rounded bg-danger text-white hover:bg-danger/80 transition-colors"
              @click="handleLeave"
            >{{ $t('squadDetail.confirmLeaveYes') }}</button>
            <button
              class="text-xs text-text-muted hover:text-text-primary"
              @click="showLeaveConfirm = false"
            >{{ $t('common.cancel') }}</button>
          </div>
        </div>

      </div>

      <!-- Admin: Join Requests -->
      <div v-if="squad.is_admin && squads.joinRequests.length > 0" class="bg-surface-1 rounded-card p-5 border border-surface-3/50 mb-4">
        <h3 class="text-sm font-semibold text-text-primary mb-3">
          {{ $t('squadDetail.joinRequests') }}
          <span class="ml-1 px-1.5 py-0.5 text-xs bg-primary/15 text-primary rounded-full">{{ squads.joinRequests.length }}</span>
        </h3>
        <div class="space-y-3">
          <div
            v-for="req in squads.joinRequests"
            :key="req.id"
            class="flex items-center justify-between py-2 border-b border-surface-3/30 last:border-0"
          >
            <div>
              <p class="text-sm text-text-primary">{{ req.alias }}</p>
              <p class="text-xs text-text-muted">{{ new Date(req.created_at).toLocaleDateString("de-DE") }}</p>
            </div>
            <div class="flex gap-2">
              <button
                class="px-3 py-1 text-xs font-medium rounded-lg bg-primary/10 text-primary border border-primary/30 hover:bg-primary/20 transition-colors"
                @click="handleApprove(req.id)"
              >
                {{ $t('squadDetail.acceptRequest') }}
              </button>
              <button
                class="px-3 py-1 text-xs font-medium rounded-lg bg-surface-2 text-text-muted border border-surface-3 hover:bg-surface-3/50 transition-colors"
                @click="handleDecline(req.id)"
              >
                {{ $t('squadDetail.declineRequest') }}
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- Admin: Danger zone -->
      <div v-if="squad.is_admin" class="bg-surface-1 rounded-card p-5 border border-danger/20 mb-4">
        <h3 class="text-sm font-semibold text-danger mb-3">{{ $t('squadDetail.dangerZone') }}</h3>
        <div v-if="!showDeleteConfirm" class="flex items-center justify-between">
          <p class="text-xs text-text-muted">{{ $t('squadDetail.deleteWarning') }}</p>
          <button
            class="shrink-0 ml-4 px-4 py-2 text-xs font-medium rounded-lg border border-danger text-danger hover:bg-danger hover:text-white transition-colors"
            @click="showDeleteConfirm = true"
          >
            {{ $t('squadDetail.deleteSquad') }}
          </button>
        </div>
        <div v-else class="flex items-center gap-3">
          <span class="text-xs text-danger">{{ $t('squadDetail.sure') }}</span>
          <button
            class="text-xs px-4 py-2 rounded-lg bg-danger text-white font-medium hover:bg-danger/80 transition-colors"
            @click="handleDelete"
          >{{ $t('squadDetail.confirmDelete') }}</button>
          <button
            class="text-xs text-text-muted hover:text-text-primary"
            @click="showDeleteConfirm = false"
          >{{ $t('common.cancel') }}</button>
        </div>
      </div>

      <!-- League Configuration -->
      <div class="bg-surface-1 rounded-card p-5 border border-surface-3/50 mb-4">
        <SquadLeagueConfigManager
          :squad-id="squadId"
          :is-admin="squad.is_admin"
        />

        <!-- Admin Settings -->
        <template v-if="squad.is_admin">
          <!-- Auto-Tipp toggle -->
          <div class="mt-4 pt-4 border-t border-surface-3/50">
            <div class="flex items-center justify-between">
              <div>
                <p class="text-sm font-medium text-text-primary">{{ $t('squadDetail.autoBet') }}</p>
                <p class="text-xs text-text-muted">
                  {{ squad.auto_bet_blocked
                    ? $t('squadDetail.autoBetBlocked')
                    : $t('squadDetail.autoBetAllowed')
                  }}
                </p>
              </div>
              <button
                class="relative shrink-0 w-10 h-6 rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-primary/50"
                :class="squad.auto_bet_blocked ? 'bg-surface-3' : 'bg-primary'"
                :disabled="togglingAutoBet"
                @click="handleToggleAutoBet"
              >
                <span
                  class="absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform"
                  :class="squad.auto_bet_blocked ? '' : 'translate-x-4'"
                />
              </button>
            </div>
          </div>

          <!-- Lock deadline -->
          <div class="mt-4 pt-4 border-t border-surface-3/50">
            <div class="flex items-center justify-between">
              <div>
                <p class="text-sm font-medium text-text-primary">{{ $t('squadDetail.deadline') }}</p>
                <p class="text-xs text-text-muted">
                  {{ t('squadDetail.deadlineDescription', { minutes: squad.lock_minutes }) }}
                </p>
              </div>
              <div class="flex items-center gap-2">
                <select
                  v-model.number="lockMinutesDraft"
                  class="bg-surface-2 border border-surface-3 rounded-lg px-2 py-1.5 text-sm text-text-primary focus:outline-none focus:ring-1 focus:ring-primary"
                >
                  <option :value="0">0 Min.</option>
                  <option :value="5">5 Min.</option>
                  <option :value="10">10 Min.</option>
                  <option :value="15">15 Min.</option>
                  <option :value="30">30 Min.</option>
                  <option :value="60">60 Min.</option>
                  <option :value="120">120 Min.</option>
                </select>
                <button
                  v-if="lockMinutesDraft !== (squad.lock_minutes ?? 15)"
                  class="px-3 py-1.5 text-xs font-medium rounded-lg bg-primary text-surface-0 hover:bg-primary/80 transition-colors disabled:opacity-50"
                  :disabled="savingLockMinutes"
                  @click="handleSaveLockMinutes"
                >
                  {{ savingLockMinutes ? "..." : $t('squadDetail.save') }}
                </button>
              </div>
            </div>
          </div>

          <!-- Visibility toggle -->
          <div class="mt-4 pt-4 border-t border-surface-3/50">
            <div class="flex items-center justify-between">
              <div>
                <p class="text-sm font-medium text-text-primary">{{ $t('squadDetail.visibility') }}</p>
                <p class="text-xs text-text-muted mt-0.5">
                  {{ squad.is_public
                    ? $t('squadDetail.publicDescription')
                    : $t('squadDetail.privateDescription')
                  }}
                </p>
              </div>
              <button
                class="relative w-10 h-6 rounded-full transition-colors"
                :class="squad.is_public ? 'bg-primary' : 'bg-surface-3'"
                @click="toggleVisibility"
              >
                <span
                  class="absolute top-1 left-1 w-4 h-4 bg-white rounded-full transition-transform"
                  :class="squad.is_public ? 'translate-x-4' : ''"
                />
              </button>
            </div>
          </div>

          <!-- Open / Locked toggle -->
          <div class="mt-4 pt-4 border-t border-surface-3/50">
            <div class="flex items-center justify-between">
              <div>
                <p class="text-sm font-medium text-text-primary">{{ $t('squadDetail.joinRequests') }}</p>
                <p class="text-xs text-text-muted mt-0.5">
                  {{ squad.is_open
                    ? $t('squadDetail.openStatus')
                    : $t('squadDetail.closedStatus')
                  }}
                </p>
              </div>
              <button
                class="relative w-10 h-6 rounded-full transition-colors"
                :class="squad.is_open ? 'bg-primary' : 'bg-surface-3'"
                @click="toggleOpen"
              >
                <span
                  class="absolute top-1 left-1 w-4 h-4 bg-white rounded-full transition-transform"
                  :class="squad.is_open ? 'translate-x-4' : ''"
                />
              </button>
            </div>
          </div>
        </template>
      </div>

      <!-- Squad Leaderboard -->
      <div class="bg-surface-1 rounded-card border border-surface-3/50">
        <h2 class="text-sm font-semibold text-text-primary px-5 py-3 border-b border-surface-3/50">
          {{ $t('squadDetail.squadLeaderboard') }}
        </h2>
        <div v-if="squads.leaderboard.length === 0" class="px-5 py-8 text-center">
          <p class="text-sm text-text-muted">{{ $t('squadDetail.noBets') }}</p>
        </div>
        <table v-else class="w-full text-sm">
          <thead>
            <tr class="text-xs text-text-muted border-b border-surface-3/30">
              <th class="text-left px-5 py-2 font-medium w-12">#</th>
              <th class="text-left py-2 font-medium">{{ $t('squadDetail.player') }}</th>
              <th class="text-right py-2 font-medium pr-5">{{ $t('settings.points') }}</th>
              <th class="text-right py-2 font-medium pr-5 hidden sm:table-cell">{{ $t('nav.bets') }}</th>
              <th class="text-right py-2 font-medium pr-5 hidden sm:table-cell">&#x2300; Quote</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="entry in squads.leaderboard"
              :key="entry.user_id"
              class="border-b border-surface-3/20 last:border-0"
            >
              <td class="px-5 py-3 font-mono">
                <span v-if="entry.rank === 1" class="text-warning">&#x1F947;</span>
                <span v-else-if="entry.rank === 2">&#x1F948;</span>
                <span v-else-if="entry.rank === 3">&#x1F949;</span>
                <span v-else class="text-text-muted">{{ entry.rank }}</span>
              </td>
              <td class="py-3 text-text-primary truncate max-w-[200px]">{{ entry.alias }}</td>
              <td class="py-3 pr-5 text-right font-medium text-text-primary tabular-nums">
                {{ entry.points.toFixed(1) }}
              </td>
              <td class="py-3 pr-5 text-right text-text-muted tabular-nums hidden sm:table-cell">
                {{ entry.tip_count }}
              </td>
              <td class="py-3 pr-5 text-right text-text-muted tabular-nums hidden sm:table-cell">
                {{ entry.avg_odds.toFixed(2) }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- Not found -->
    <div v-else class="text-center py-16">
      <p class="text-sm text-text-muted">{{ $t('squadDetail.notFound') }}</p>
    </div>
  </div>
</template>
