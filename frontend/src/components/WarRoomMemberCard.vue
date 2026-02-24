<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { WarRoomMember } from "@/composables/useWarRoom";

const { t } = useI18n();

const props = defineProps<{
  member: WarRoomMember;
  phase: "pre_kickoff" | "revealed" | "live";
  isWinning: boolean;
  isLosing: boolean;
  isMaverick: boolean;
  homeTeam: string;
  awayTeam: string;
  flipDelay: number;
}>();

const selectionLabel = computed(() => {
  const sel = props.member.selection?.value;
  if (!sel) return "—";
  if (sel === "1") return props.homeTeam;
  if (sel === "2") return props.awayTeam;
  return t("match.draw");
});

const selectionShort = computed(() => props.member.selection?.value ?? "—");

const isFlipped = computed(() => props.phase !== "pre_kickoff");

const isMystery = computed(
  () =>
    props.phase === "pre_kickoff" &&
    props.member.has_tipped &&
    !props.member.is_self
);

const cardClass = computed(() => {
  const classes: string[] = [];

  if (props.isMaverick && props.phase !== "pre_kickoff") {
    classes.push("maverick-glow");
  } else if (props.phase === "live" && props.isWinning) {
    classes.push("winning-card");
  } else if (props.phase === "live" && props.isLosing) {
    classes.push("losing-card");
  } else if (
    props.phase === "pre_kickoff" &&
    !props.member.has_tipped
  ) {
    classes.push("border-warning/50", "bg-warning/5");
  } else {
    classes.push("border-surface-3/50");
  }

  return classes;
});
</script>

<template>
  <div
    class="war-room-scene rounded-card border transition-all duration-500"
    :class="cardClass"
    :aria-label="`${member.alias} — ${
      phase === 'pre_kickoff'
        ? member.has_tipped
          ? 'Tipp abgegeben'
          : 'Noch kein Tipp'
        : selectionLabel
    }`"
  >
    <div
      class="war-room-inner"
      :class="{ flipped: isFlipped }"
      :style="{ '--flip-delay': `${flipDelay}ms` }"
    >
      <!-- ============ FRONT FACE (pre-kickoff) ============ -->
      <div class="war-room-face war-room-front">
        <p
          class="text-xs font-medium text-text-primary truncate text-center mb-2 px-1"
        >
          {{ member.alias }}
          <span v-if="member.is_self" class="text-primary"> {{ t('squad.warRoomYou') }}</span>
        </p>

        <!-- Mystery: other user, tipped -->
        <template v-if="isMystery">
          <div class="mystery-seal mx-auto flex items-center justify-center">
            <svg
              class="w-5 h-5 text-text-muted/60"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              stroke-width="1.5"
            >
              <path
                stroke-linecap="round"
                stroke-linejoin="round"
                d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z"
              />
            </svg>
          </div>
          <p class="text-xs text-text-muted text-center mt-1">Gesperrt</p>
        </template>

        <!-- Self tip visible -->
        <template v-else-if="member.is_self && member.selection">
          <div class="text-center">
            <span class="text-2xl font-bold font-mono text-primary">{{
              selectionShort
            }}</span>
            <p class="text-[10px] text-text-muted mt-0.5 leading-tight">
              {{ selectionLabel }}
            </p>
          </div>
        </template>

        <!-- Not tipped: amber nudge -->
        <template v-else-if="!member.has_tipped">
          <div class="flex flex-col items-center gap-1">
            <div
              class="w-8 h-8 rounded-full bg-warning/20 flex items-center justify-center"
            >
              <svg
                class="w-4 h-4 text-warning"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                stroke-width="2"
              >
                <path
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
                />
              </svg>
            </div>
            <p class="text-[10px] text-warning">{{ t('squad.warRoomNoBetWarning') }}</p>
          </div>
        </template>

        <!-- Other user, tipped but not mystery (fallback) -->
        <template v-else>
          <div class="text-center">
            <div
              class="w-8 h-8 rounded-full bg-primary-muted/30 mx-auto flex items-center justify-center mb-1"
            >
              <svg
                class="w-4 h-4 text-primary"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                stroke-width="2"
              >
                <path
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  d="M4.5 12.75l6 6 9-13.5"
                />
              </svg>
            </div>
            <p class="text-[10px] text-primary">{{ t('squad.warRoomBetPlaced') }}</p>
          </div>
        </template>
      </div>

      <!-- ============ BACK FACE (revealed / live) ============ -->
      <div class="war-room-face war-room-back">
        <p
          class="text-xs font-medium text-text-primary truncate text-center mb-2 px-1"
        >
          {{ member.alias }}
          <span v-if="member.is_self" class="text-primary"> {{ t('squad.warRoomYou') }}</span>
        </p>

        <template v-if="member.selection">
          <div class="text-center">
            <span
              class="text-2xl font-bold font-mono"
              :class="{
                'text-primary': phase === 'live' && isWinning,
                'text-danger': phase === 'live' && isLosing,
                'text-text-primary': phase === 'revealed',
              }"
            >
              {{ selectionShort }}
            </span>
            <p class="text-[10px] text-text-muted mt-0.5 leading-tight">
              {{ selectionLabel }}
            </p>
            <p
              v-if="member.locked_odds"
              class="text-[10px] font-mono text-text-muted mt-0.5"
            >
              @ {{ member.locked_odds.toFixed(2) }}
            </p>
          </div>

          <!-- Live status pill -->
          <div v-if="phase === 'live'" class="mt-2 flex justify-center">
            <span
              v-if="isWinning"
              class="text-[10px] px-1.5 py-0.5 rounded-full bg-primary/20 text-primary winning-pulse"
            >
              Winning
            </span>
            <span
              v-else-if="isLosing"
              class="text-[10px] px-1.5 py-0.5 rounded-full bg-danger/10 text-danger/70"
            >
              Losing
            </span>
          </div>
        </template>

        <template v-else>
          <div class="text-center">
            <p class="text-xs text-text-muted">{{ t('squad.warRoomNoBet') }}</p>
          </div>
        </template>

        <!-- Maverick label -->
        <div v-if="isMaverick" class="mt-1 text-center">
          <span
            class="text-[9px] font-bold text-purple-400 uppercase tracking-wide"
            >Maverick</span
          >
        </div>

        <!-- Trash talk bubble (live + losing) -->
        <div
          v-if="phase === 'live' && isLosing && member.has_tipped"
          class="absolute -top-1.5 -right-1.5"
          title="Trash talk incoming..."
          aria-hidden="true"
        >
          <div
            class="w-5 h-5 rounded-full bg-danger/80 flex items-center justify-center shadow-lg"
          >
            <svg
              class="w-3 h-3 text-white"
              fill="currentColor"
              viewBox="0 0 20 20"
            >
              <path
                fill-rule="evenodd"
                d="M18 10c0 3.866-3.582 7-8 7a8.841 8.841 0 01-4.083-.98L2 17l1.338-3.123C2.493 12.767 2 11.434 2 10c0-3.866 3.582-7 8-7s8 3.134 8 7zM7 9H5v2h2V9zm8 0h-2v2h2V9zM9 9h2v2H9V9z"
                clip-rule="evenodd"
              />
            </svg>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 3D Card Flip */
.war-room-scene {
  perspective: 600px;
  position: relative;
  min-height: 130px;
}

.war-room-inner {
  position: relative;
  width: 100%;
  height: 130px;
  transition: transform 0.6s cubic-bezier(0.4, 0.2, 0.2, 1)
    var(--flip-delay, 0ms);
  transform-style: preserve-3d;
}

.war-room-inner.flipped {
  transform: rotateY(180deg);
}

.war-room-face {
  position: absolute;
  inset: 0;
  padding: 0.75rem 0.5rem;
  backface-visibility: hidden;
  -webkit-backface-visibility: hidden;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}

.war-room-back {
  transform: rotateY(180deg);
}

/* Glassmorphism Mystery Seal */
.mystery-seal {
  width: 40px;
  height: 40px;
  border-radius: 0.5rem;
  backdrop-filter: blur(8px) saturate(1.2);
  -webkit-backdrop-filter: blur(8px) saturate(1.2);
  background: rgba(17, 24, 39, 0.6);
  border: 1px solid rgba(255, 255, 255, 0.08);
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.06),
    0 4px 16px rgba(0, 0, 0, 0.4);
}

/* Maverick Neon Purple Glow */
.maverick-glow {
  box-shadow:
    0 0 0 1px rgba(168, 85, 247, 0.5),
    0 0 12px rgba(168, 85, 247, 0.35),
    0 0 28px rgba(168, 85, 247, 0.12);
  border-color: rgba(168, 85, 247, 0.5) !important;
}

/* Winning Card Pulse */
.winning-card {
  border-color: rgba(34, 197, 94, 0.6);
  animation: winning-border-pulse 2s ease-in-out infinite;
}

@keyframes winning-border-pulse {
  0%,
  100% {
    box-shadow:
      0 0 0 1px rgba(34, 197, 94, 0.4),
      0 0 8px rgba(34, 197, 94, 0.2);
  }
  50% {
    box-shadow:
      0 0 0 2px rgba(34, 197, 94, 0.7),
      0 0 20px rgba(34, 197, 94, 0.4);
  }
}

/* Winning Pill Pulse */
@keyframes winning-pill-pulse {
  0%,
  100% {
    box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.4);
    background-color: rgba(34, 197, 94, 0.15);
  }
  50% {
    box-shadow: 0 0 0 6px rgba(34, 197, 94, 0);
    background-color: rgba(34, 197, 94, 0.25);
  }
}

.winning-pulse {
  animation: winning-pill-pulse 2s ease-in-out infinite;
}

/* Losing Card Dim */
.losing-card {
  border-color: rgba(239, 68, 68, 0.3);
  background-color: rgba(239, 68, 68, 0.04);
}
</style>
