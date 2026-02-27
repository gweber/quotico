import { defineStore } from "pinia";
import { ref } from "vue";

export const useAdminSyncStore = defineStore("adminSync", () => {
  const lastSyncCompletedAt = ref(0);

  function notifySyncCompleted() {
    lastSyncCompletedAt.value = Date.now();
  }

  return { lastSyncCompletedAt, notifySyncCompleted };
});
