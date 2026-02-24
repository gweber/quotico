<script setup lang="ts">
import { ref, computed } from "vue";
import { useI18n } from "vue-i18n";
import { useSquadsStore } from "@/stores/squads";
import { useMatchdayStore } from "@/stores/matchday";
import { useToast } from "@/composables/useToast";
import { GAME_MODE_I18N_KEYS, type GameModeType } from "@/types/league";
import { SPORT_LABELS } from "@/types/sports";

const { t } = useI18n();

const props = defineProps<{
  squadId: string;
  isAdmin: boolean;
}>();

const squadsStore = useSquadsStore();
const matchday = useMatchdayStore();
const toast = useToast();
const saving = ref(false);
const showAddMenu = ref(false);
const addModePick = ref<GameModeType>("classic");

const sportLabels = SPORT_LABELS;

const modes: { value: GameModeType; label: string }[] = [
  { value: "classic", label: "Classic" },
  { value: "moneyline", label: "Moneyline" },
  { value: "bankroll", label: "Bankroll" },
  { value: "survivor", label: "Survivor" },
  { value: "over_under", label: "Over/Under" },
  { value: "fantasy", label: "Fantasy" },
];

const activeConfigs = computed(() =>
  squadsStore.getActiveLeagueConfigs(props.squadId)
);

const configuredSportKeys = computed(() =>
  new Set(activeConfigs.value.map((lc) => lc.sport_key))
);

const availableToAdd = computed(() =>
  matchday.sports.filter((s) => !configuredSportKeys.value.has(s.sport_key))
);

async function addLeague(sportKey: string) {
  saving.value = true;
  try {
    await squadsStore.setLeagueConfig(props.squadId, sportKey, addModePick.value);
    toast.success(
      `${sportLabels[sportKey] || sportKey} ${t("common.added")}`
    );
    showAddMenu.value = false;
    addModePick.value = "classic";
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t("common.error"));
  } finally {
    saving.value = false;
  }
}

async function removeLeague(sportKey: string) {
  if (
    !confirm(
      t("squadDetail.deactivateConfirm")
    )
  )
    return;

  saving.value = true;
  try {
    await squadsStore.removeLeagueConfig(props.squadId, sportKey);
    toast.success(t("squadDetail.deactivated"));
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t("common.error"));
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="space-y-3">
    <div class="flex items-center justify-between">
      <h3 class="text-sm font-semibold text-text-primary">
        {{ $t('squadDetail.leagueConfig') }}
      </h3>
      <button
        v-if="isAdmin && availableToAdd.length > 0"
        class="text-xs text-primary font-medium hover:underline"
        @click="showAddMenu = !showAddMenu"
      >
        {{ $t('squadDetail.addLeague') }}
      </button>
    </div>

    <!-- Active leagues -->
    <div v-if="activeConfigs.length > 0" class="space-y-2">
      <div
        v-for="config in activeConfigs"
        :key="config.sport_key"
        class="flex items-center justify-between p-3 bg-surface-1 rounded-lg border border-surface-3"
      >
        <div class="flex-1 min-w-0">
          <div class="text-sm font-medium text-text-primary truncate">
            {{ sportLabels[config.sport_key] || config.sport_key }}
          </div>
        </div>

        <div class="flex items-center gap-2 ml-3">
          <span
            class="text-xs px-2 py-1 rounded bg-primary/10 text-primary font-medium"
          >
            {{ $t(GAME_MODE_I18N_KEYS[config.game_mode]) }}
          </span>

          <button
            v-if="isAdmin"
            class="text-text-muted hover:text-red-500 transition-colors p-1"
            :disabled="saving"
            :title="$t('squadDetail.deactivated')"
            @click="removeLeague(config.sport_key)"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              class="h-4 w-4"
              viewBox="0 0 20 20"
              fill="currentColor"
            >
              <path
                fill-rule="evenodd"
                d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z"
                clip-rule="evenodd"
              />
            </svg>
          </button>
        </div>
      </div>
    </div>

    <!-- Empty state -->
    <div
      v-else
      class="text-center py-6 text-text-muted text-sm"
    >
      {{ $t('squadDetail.noLeagues') }}
    </div>

    <!-- Add league menu -->
    <div
      v-if="showAddMenu && isAdmin"
      class="bg-surface-1 border border-surface-3 rounded-lg p-3 space-y-2"
    >
      <div class="flex items-center gap-2">
        <span class="text-xs text-text-muted">{{ $t('squadDetail.gameMode') }}:</span>
        <select
          v-model="addModePick"
          class="text-xs bg-surface-2 border border-surface-3 rounded px-2 py-1 text-text-primary"
        >
          <option v-for="m in modes" :key="m.value" :value="m.value">
            {{ m.label }}
          </option>
        </select>
      </div>
      <div class="space-y-1">
        <button
          v-for="sport in availableToAdd"
          :key="sport.sport_key"
          class="w-full text-left px-3 py-2 rounded text-sm text-text-primary hover:bg-surface-2 transition-colors"
          :disabled="saving"
          @click="addLeague(sport.sport_key)"
        >
          {{ sportLabels[sport.sport_key] || sport.sport_key }}
        </button>
      </div>
    </div>
  </div>
</template>
