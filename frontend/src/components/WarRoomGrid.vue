<script setup lang="ts">
import type { WarRoomMember } from "@/composables/useWarRoom";
import WarRoomMemberCard from "./WarRoomMemberCard.vue";

defineProps<{
  members: WarRoomMember[];
  phase: "pre_kickoff" | "revealed" | "live";
  maverickIds: string[];
  homeTeam: string;
  awayTeam: string;
  tipStates: Map<string, { winning: boolean; losing: boolean }>;
}>();
</script>

<template>
  <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
    <TransitionGroup name="card-appear">
      <WarRoomMemberCard
        v-for="(member, idx) in members"
        :key="member.user_id"
        :member="member"
        :phase="phase"
        :is-winning="tipStates.get(member.user_id)?.winning ?? false"
        :is-losing="tipStates.get(member.user_id)?.losing ?? false"
        :is-maverick="maverickIds.includes(member.user_id)"
        :home-team="homeTeam"
        :away-team="awayTeam"
        :flip-delay="idx * 80"
      />
    </TransitionGroup>
  </div>
</template>

<style scoped>
.card-appear-enter-active {
  transition: all 0.35s ease-out;
}
.card-appear-enter-from {
  opacity: 0;
  transform: translateY(0.5rem) scale(0.95);
}
</style>
