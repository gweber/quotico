<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { Matchday } from "@/stores/matchday";
import { useI18n } from "vue-i18n";

const props = defineProps<{
  matchdays: Matchday[];
  currentId: string | null;
  totalMatchdays: number;
}>();
const { t } = useI18n();

const emit = defineEmits<{
  select: [id: string];
  preview: [id: string | null];
}>();

// --- Refs ---
const railRef = ref<HTMLElement | null>(null);
const isDragging = ref(false);
const dragSlotIndex = ref<number | null>(null);
const hoverSlotIndex = ref<number | null>(null);
// Bridges the gap between drag-release and currentId update
const pendingNumber = ref<number | null>(null);
// Suppresses the click event that fires after a drag gesture
const justDragged = ref(false);

// Clear pending once the parent confirms the new selection
watch(
  () => props.currentId,
  () => {
    pendingNumber.value = null;
  }
);

// Build a map of matchday_number -> Matchday for quick lookup
const matchdayMap = computed(() => {
  const map = new Map<number, Matchday>();
  for (const md of props.matchdays) {
    map.set(md.matchday_number, md);
  }
  return map;
});

// All slot positions 1..totalMatchdays
const slots = computed(() => {
  const result: {
    number: number;
    md: Matchday | null;
    leftPercent: number;
  }[] = [];
  const total = props.totalMatchdays;
  for (let i = 1; i <= total; i++) {
    result.push({
      number: i,
      md: matchdayMap.value.get(i) ?? null,
      leftPercent: total > 1 ? ((i - 1) / (total - 1)) * 100 : 50,
    });
  }
  return result;
});

// Completed count (from actual matchday data)
const completedCount = computed(() =>
  props.matchdays.filter((md) => md.all_resolved).length
);

// Selected matchday number (from parent's currentId)
const selectedNumber = computed(() => {
  if (!props.currentId) return null;
  const md = props.matchdays.find((m) => m.id === props.currentId);
  return md?.matchday_number ?? null;
});

// The "natural" current matchday — the next tippable day.
// Prefer upcoming (fully tippable), then in_progress, then last.
const seasonCurrentNumber = computed(() => {
  const upcoming = props.matchdays.find((md) => md.status === "upcoming");
  if (upcoming) return upcoming.matchday_number;
  const inProgress = props.matchdays.find((md) => md.status === "in_progress");
  if (inProgress) return inProgress.matchday_number;
  const last = props.matchdays[props.matchdays.length - 1];
  return last?.matchday_number ?? null;
});

// The active preview slot (drag takes priority over hover)
const activePreviewIndex = computed(
  () => dragSlotIndex.value ?? hoverSlotIndex.value
);

// Where the big dot visually sits: drag > pending > selected
const visualDotNumber = computed(
  () => dragSlotIndex.value ?? pendingNumber.value ?? selectedNumber.value
);

// Is the user viewing a different matchday than the season's current?
const isBrowsingAway = computed(
  () =>
    visualDotNumber.value != null &&
    seasonCurrentNumber.value != null &&
    visualDotNumber.value !== seasonCurrentNumber.value
);

// Progress rail width: up to the last completed matchday
const progressPercent = computed(() => {
  let lastCompleted = 0;
  for (const md of props.matchdays) {
    if (md.all_resolved && md.matchday_number > lastCompleted) {
      lastCompleted = md.matchday_number;
    }
  }
  if (lastCompleted === 0 || props.totalMatchdays <= 1) return 0;
  return ((lastCompleted - 1) / (props.totalMatchdays - 1)) * 100;
});

// Which numbers get labels: 1, every 5th, last, + visual dot position
const labelNumbers = computed(() => {
  const set = new Set<number>();
  set.add(1);
  set.add(props.totalMatchdays);
  for (let i = 5; i <= props.totalMatchdays; i += 5) {
    set.add(i);
  }
  if (visualDotNumber.value) set.add(visualDotNumber.value);
  return set;
});

// Tooltip data for the active preview
const tooltipSlot = computed(() => {
  const idx = activePreviewIndex.value;
  if (idx == null) return null;
  const slot = slots.value.find((s) => s.number === idx);
  if (!slot?.md) return null;
  return slot;
});

const tooltipDateRange = computed(() => {
  const md = tooltipSlot.value?.md;
  if (!md) return "";
  const fmt = (iso: string | null) => {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleDateString("de-DE", { day: "numeric", month: "short" });
  };
  const from = fmt(md.first_kickoff);
  const to = fmt(md.last_kickoff);
  if (!from) return "";
  if (from === to || !to) return from;
  return `${from} – ${to}`;
});

const tooltipStatus = computed(() => {
  const md = tooltipSlot.value?.md;
  if (!md) return "";
  if (md.all_resolved) return "abgeschlossen";
  if (md.status === "in_progress") return "läuft";
  return "geplant";
});

// --- Drag logic ---

function nearestMatchdayNumber(clientX: number): number {
  if (!railRef.value) return 1;
  const rect = railRef.value.getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  const mdNumber = Math.round(ratio * (props.totalMatchdays - 1)) + 1;
  return Math.max(1, Math.min(props.totalMatchdays, mdNumber));
}

function onDotPointerDown(e: PointerEvent) {
  isDragging.value = true;
  dragSlotIndex.value = visualDotNumber.value;
  (e.target as HTMLElement).setPointerCapture(e.pointerId);
  e.preventDefault();
}

function onRailPointerMove(e: PointerEvent) {
  if (!isDragging.value) return;
  const mdNum = nearestMatchdayNumber(e.clientX);
  if (mdNum !== dragSlotIndex.value) {
    dragSlotIndex.value = mdNum;
    const md = matchdayMap.value.get(mdNum);
    emit("preview", md?.id ?? null);
  }
}

function onRailPointerUp() {
  if (!isDragging.value) return;
  isDragging.value = false;
  // Suppress the click event that fires after pointerup on the original button
  justDragged.value = true;
  setTimeout(() => {
    justDragged.value = false;
  }, 100);

  const mdNum = dragSlotIndex.value;
  dragSlotIndex.value = null;
  if (mdNum != null) {
    const md = matchdayMap.value.get(mdNum);
    if (md) {
      pendingNumber.value = mdNum; // hold position until currentId catches up
      emit("select", md.id);
    }
  }
}

// --- Dot visuals ---

function dotColor(slot: { number: number; md: Matchday | null }): string {
  if (!slot.md) return "bg-surface-3 opacity-30";

  const isVisualDot = slot.number === visualDotNumber.value;

  if (isVisualDot) {
    // Green when at the season's current position, blue when browsing elsewhere
    return isBrowsingAway.value ? "bg-secondary" : "bg-primary";
  }

  if (slot.md.all_resolved) return "bg-emerald-500";
  if (slot.md.status === "in_progress") return "bg-primary/50";
  return "bg-surface-3";
}

function dotSize(slot: { number: number; md: Matchday | null }): string {
  // The main "viewing" dot — big
  if (slot.number === visualDotNumber.value) {
    const ringColor = isBrowsingAway.value
      ? "ring-secondary/30"
      : "ring-primary/30";
    return `w-3.5 h-3.5 ring-2 ${ringColor} z-10`;
  }
  // Season marker — medium dot when browsing away, so user can see "home"
  if (
    isBrowsingAway.value &&
    slot.number === seasonCurrentNumber.value
  ) {
    return "w-2.5 h-2.5 ring-1 ring-primary/40 z-[5]";
  }
  // In-progress matchday — slightly bigger than normal to stand out
  if (slot.md?.status === "in_progress") {
    return "w-3 h-3";
  }
  return "w-1.5 h-1.5";
}

// Season marker gets primary color even when not selected
function seasonMarkerColor(slot: {
  number: number;
  md: Matchday | null;
}): string | null {
  if (
    !isBrowsingAway.value ||
    slot.number !== seasonCurrentNumber.value ||
    slot.number === visualDotNumber.value
  ) {
    return null;
  }
  return "bg-primary/60";
}

function matchdayLabel(md: Matchday): string {
  return `${t("matchday.title")} ${md.matchday_number}`;
}
</script>

<template>
  <div class="select-none">
    <!-- Header -->
    <div class="flex items-baseline justify-between mb-2">
      <span class="text-xs text-text-muted">Saison-Fortschritt</span>
      <span class="text-xs text-text-secondary font-medium tabular-nums">
        {{ completedCount }}&thinsp;/&thinsp;{{ totalMatchdays }}
      </span>
    </div>

    <!-- Track -->
    <div
      ref="railRef"
      class="relative h-8 flex items-center"
      style="touch-action: pan-y"
      @pointermove="onRailPointerMove"
      @pointerup="onRailPointerUp"
      @pointercancel="onRailPointerUp"
    >
      <!-- Background rail -->
      <div
        class="absolute inset-x-0 h-0.5 bg-surface-3/40 top-1/2 -translate-y-1/2 rounded-full"
      />
      <!-- Completed rail -->
      <div
        class="absolute left-0 h-0.5 bg-emerald-500/40 top-1/2 -translate-y-1/2 rounded-full transition-all duration-500"
        :style="{ width: progressPercent + '%' }"
      />

      <!-- Tooltip -->
      <Transition
        enter-active-class="transition duration-100"
        enter-from-class="opacity-0 translate-y-1"
        leave-active-class="transition duration-75"
        leave-to-class="opacity-0"
      >
        <div
          v-if="tooltipSlot"
          class="absolute bottom-full mb-2 -translate-x-1/2 pointer-events-none z-20"
          :style="{ left: tooltipSlot.leftPercent + '%' }"
        >
          <div
            class="bg-surface-2 border border-surface-3 rounded-lg px-3 py-2 text-xs whitespace-nowrap shadow-lg"
          >
            <div class="font-semibold text-text-primary">
              {{ matchdayLabel(tooltipSlot.md!) }}
            </div>
            <div v-if="tooltipDateRange" class="text-text-secondary mt-0.5">
              {{ tooltipDateRange }}
            </div>
            <div class="text-text-muted mt-0.5">
              {{ tooltipSlot.md!.match_count }} Spiele · {{ tooltipStatus }}
            </div>
          </div>
          <!-- Arrow -->
          <div
            class="absolute left-1/2 -translate-x-1/2 -bottom-1 w-2 h-2 bg-surface-2 border-r border-b border-surface-3 rotate-45"
          />
        </div>
      </Transition>

      <!-- Dots -->
      <button
        v-for="slot in slots"
        :key="slot.number"
        class="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 rounded-full cursor-pointer hover:scale-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
        :class="[
          seasonMarkerColor(slot) || dotColor(slot),
          dotSize(slot),
          !slot.md && 'cursor-default hover:scale-100',
          isDragging ? 'transition-none' : 'transition-all duration-200',
        ]"
        :style="{ left: slot.leftPercent + '%' }"
        :disabled="!slot.md"
        :aria-label="
          slot.md
            ? `${matchdayLabel(slot.md)}${slot.md.all_resolved ? ', abgeschlossen' : slot.md.status === 'in_progress' ? ', läuft' : ', geplant'}`
            : `${t('matchday.title')} ${slot.number}`
        "
        :aria-current="slot.md?.id === currentId ? 'true' : undefined"
        @click="!isDragging && !justDragged && slot.md && emit('select', slot.md.id)"
        @pointerdown="
          slot.number === visualDotNumber
            ? onDotPointerDown($event)
            : undefined
        "
        @mouseenter="hoverSlotIndex = slot.number"
        @mouseleave="hoverSlotIndex = null"
      />
    </div>

    <!-- Number labels -->
    <div class="relative h-4">
      <span
        v-for="slot in slots"
        :key="'label-' + slot.number"
        v-show="labelNumbers.has(slot.number)"
        class="absolute -translate-x-1/2 text-[10px] tabular-nums"
        :class="
          slot.number === visualDotNumber
            ? isBrowsingAway
              ? 'text-secondary font-semibold'
              : 'text-primary font-semibold'
            : 'text-text-muted'
        "
        :style="{ left: slot.leftPercent + '%' }"
      >
        {{ slot.number }}
      </span>
    </div>
  </div>
</template>
