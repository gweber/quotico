<script setup lang="ts">
import { useToast } from "@/composables/useToast";

const { toasts } = useToast();

const icons: Record<string, string> = {
  success: "\u2713",
  error: "\u2717",
  warning: "\u26A0",
  info: "\u2139",
};

const colors: Record<string, string> = {
  success: "border-primary bg-primary-muted/30",
  error: "border-danger bg-danger-muted/30",
  warning: "border-warning bg-warning/10",
  info: "border-secondary bg-secondary-muted/30",
};
</script>

<template>
  <div
    class="fixed top-4 right-4 z-50 flex flex-col gap-2 pointer-events-none"
    aria-live="polite"
    aria-atomic="false"
  >
    <TransitionGroup name="toast">
      <div
        v-for="toast in toasts"
        :key="toast.id"
        class="pointer-events-auto border-l-4 rounded-card px-4 py-3 shadow-lg max-w-sm"
        :class="colors[toast.type]"
        role="status"
      >
        <div class="flex items-start gap-2">
          <span class="text-lg leading-none mt-0.5" aria-hidden="true">
            {{ icons[toast.type] }}
          </span>
          <p class="text-sm text-text-primary">{{ toast.message }}</p>
        </div>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-enter-active {
  transition: all 0.3s ease-out;
}
.toast-leave-active {
  transition: all 0.2s ease-in;
}
.toast-enter-from {
  opacity: 0;
  transform: translateX(1rem);
}
.toast-leave-to {
  opacity: 0;
  transform: translateX(1rem);
}
</style>
