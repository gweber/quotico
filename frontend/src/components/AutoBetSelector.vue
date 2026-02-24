<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { useMatchdayStore } from "@/stores/matchday";

const { t } = useI18n();
const matchday = useMatchdayStore();

const options = computed(() => [
  {
    value: "none",
    label: t("matchday.autoBetNone"),
    desc: t("matchday.autoBetNoneDesc"),
  },
  {
    value: "q_bot",
    label: t("matchday.autoBetQBot"),
    desc: t("matchday.autoBetQBotDesc"),
  },
  {
    value: "favorite",
    label: t("matchday.autoBetFavorite"),
    desc: t("matchday.autoBetFavoriteDesc"),
  },
  {
    value: "draw",
    label: t("matchday.autoBetDraw"),
    desc: t("matchday.autoBetDrawDesc"),
  },
]);
</script>

<template>
  <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
    <h3 id="auto-tipp-label" class="text-sm font-semibold text-text-primary mb-2">{{ $t('matchday.autoBetTitle') }}</h3>
    <p class="text-xs text-text-muted mb-3">
      {{ $t('matchday.autoBetApplied') }}
    </p>
    <div role="radiogroup" aria-labelledby="auto-tipp-label" class="space-y-2">
      <label
        v-for="opt in options"
        :key="opt.value"
        :for="`auto-tipp-${opt.value}`"
        class="flex items-start gap-3 p-3 rounded-lg cursor-pointer transition-all border border-transparent"
        :class="
          matchday.draftAutoStrategy === opt.value
            ? 'bg-primary-muted/10 border-primary/20'
            : 'hover:bg-surface-2'
        "
      >
        <input
          :id="`auto-tipp-${opt.value}`"
          type="radio"
          name="auto-tipp"
          :value="opt.value"
          :checked="matchday.draftAutoStrategy === opt.value"
          class="mt-1 h-4 w-4 accent-primary"
          @change="matchday.draftAutoStrategy = opt.value"
        />
        <div class="select-none">
          <span class="text-sm font-medium text-text-primary">{{ opt.label }}</span>
          <p class="text-xs text-text-muted">{{ opt.desc }}</p>
        </div>
      </label>
    </div>
  </div>
</template>
