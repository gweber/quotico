<script setup lang="ts">
import { onMounted, computed, ref } from "vue";
import { useRoute } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import AppHeader from "@/components/layout/AppHeader.vue";
import ToastNotification from "@/components/ToastNotification.vue";

const route = useRoute();
const auth = useAuthStore();
const appReady = ref(false);

// Auth pages don't show the header
const isAuthPage = computed(() =>
  ["login", "register"].includes(route.name as string)
);

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
    <RouterView />
  </template>
</template>
