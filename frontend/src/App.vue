<script setup lang="ts">
import { onMounted, computed, ref } from "vue";
import { useRoute } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import AppHeader from "@/components/layout/AppHeader.vue";
import AppFooter from "@/components/layout/AppFooter.vue";
import ToastNotification from "@/components/ToastNotification.vue";
import AgeGateModal from "@/components/AgeGateModal.vue";

const route = useRoute();
const auth = useAuthStore();
const appReady = ref(false);
const ageGateDismissed = ref(false);

// Auth pages don't show the header/footer
const isAuthPage = computed(() =>
  ["login", "register", "complete-profile"].includes(route.name as string)
);

const showAgeGate = computed(() => {
  if (auth.isLoggedIn) return false;
  if (isAuthPage.value) return false;
  if (route.name === "legal") return false;
  if (ageGateDismissed.value) return false;

  try {
    const accepted = localStorage.getItem("ageGateAcceptedAt");
    if (accepted) {
      const acceptedDate = new Date(accepted);
      const thirtyDaysAgo = new Date();
      thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30);
      if (acceptedDate > thirtyDaysAgo) return false;
    }
  } catch {
    // localStorage unavailable — show modal
  }
  return true;
});

function handleAgeGateConfirmed() {
  ageGateDismissed.value = true;
}

onMounted(async () => {
  // Restore session before rendering anything
  await auth.fetchUser();
  appReady.value = true;
});
</script>

<template>
  <template v-if="appReady">
    <a href="#main-content" class="skip-link">Zum Inhalt springen</a>
    <AppHeader v-if="!isAuthPage" />
    <ToastNotification />
    <AgeGateModal v-if="showAgeGate" @confirmed="handleAgeGateConfirmed" />
    <RouterView />
    <AppFooter v-if="!isAuthPage" />
  </template>
</template>
