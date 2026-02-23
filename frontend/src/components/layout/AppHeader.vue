<script setup lang="ts">
import { ref, computed, onUnmounted } from "vue";
import { useRouter } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { useBetSlipStore } from "@/stores/betslip";
import { useToast } from "@/composables/useToast";

const router = useRouter();
const auth = useAuthStore();
const betslip = useBetSlipStore();
const toast = useToast();
const mobileMenuOpen = ref(false);

const navLinks = [
  { to: "/", label: "Tipps", icon: "\u26BD" },
  { to: "/teams", label: "Teams", icon: "\uD83C\uDFDF\uFE0F" },
  { to: "/spieltag", label: "Spieltag", icon: "\uD83D\uDCCB" },
  { to: "/squads", label: "Squads", icon: "\uD83D\uDC65" },
  { to: "/battles", label: "Battles", icon: "\u2694\uFE0F" },
  { to: "/leaderboard", label: "Rangliste", icon: "\uD83C\uDFC6" },
  { to: "/settings", label: "Einstellungen", icon: "\u2699\uFE0F" },
];

// Filter links if profile is incomplete
const visibleNavLinks = computed(() => {
  if (auth.isLoggedIn && auth.needsProfileCompletion) {
    return [];
  }
  return navLinks;
});

// Auto-close mobile menu on window resize to desktop
let mediaQuery: MediaQueryList | null = null;
function handleResize(e: MediaQueryListEvent) {
  if (e.matches) mobileMenuOpen.value = false;
}
if (typeof window !== "undefined") {
  mediaQuery = window.matchMedia("(min-width: 768px)");
  mediaQuery.addEventListener("change", handleResize);
}
onUnmounted(() => {
  mediaQuery?.removeEventListener("change", handleResize);
});

async function handleLogout() {
  await auth.logout();
  betslip.$reset();
  toast.success("Erfolgreich abgemeldet.");
  router.push("/login");
}

</script>

<template>
  <header class="bg-surface-1 border-b border-surface-3 sticky top-0 z-40">
    <div class="max-w-screen-2xl mx-auto px-4 h-14 flex items-center justify-between">
      <!-- Left: Logo + Badge -->
      <div class="flex items-center gap-3">
        <RouterLink to="/" class="text-xl font-bold text-primary tracking-tight">
          Quotico
        </RouterLink>
        <span
          class="hidden sm:inline-flex items-center px-2 py-0.5 text-xs font-medium bg-surface-2 text-text-secondary rounded-full border border-surface-3"
        >
          Tippspiel &mdash; kein Echtgeld
        </span>
      </div>

      <!-- Center: Desktop Nav -->
      <nav class="hidden md:flex items-center gap-1" aria-label="Hauptnavigation">
        <RouterLink
          v-for="link in visibleNavLinks"
          :key="link.to"
          :to="link.to"
          class="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm text-text-secondary hover:text-text-primary hover:bg-surface-2 transition-colors"
          active-class="!text-primary !bg-primary-muted/20"
        >
          <span aria-hidden="true">{{ link.icon }}</span>
          <span>{{ link.label }}</span>
        </RouterLink>
      </nav>

      <!-- Right: Auth + BetSlip toggle (mobile) -->
      <div class="flex items-center gap-2">
        <!-- BetSlip counter (mobile) -->
        <button
          v-if="betslip.itemCount > 0"
          class="md:hidden relative flex items-center justify-center w-touch h-touch rounded-lg bg-surface-2 text-text-primary hover:bg-surface-3 transition-colors"
          aria-label="Tippschein anzeigen"
          @click="betslip.isOpen = !betslip.isOpen"
        >
          <span aria-hidden="true" class="text-lg">📋</span>
          <span
            class="absolute -top-1 -right-1 bg-primary text-surface-0 text-xs font-bold w-5 h-5 flex items-center justify-center rounded-full"
          >
            {{ betslip.itemCount }}
          </span>
        </button>

        <!-- Auth -->
        <template v-if="auth.isLoggedIn">
          <RouterLink
            v-if="auth.isAdmin"
            to="/admin"
            class="hidden sm:inline-flex items-center gap-1 text-xs px-2 py-1 rounded-lg bg-warning/10 text-warning hover:bg-warning/20 transition-colors font-medium flex-shrink-0"
          >
            Admin
          </RouterLink>
          <span
            class="hidden sm:block text-sm text-text-secondary truncate max-w-[120px] lg:max-w-[160px]"
            :title="auth.user?.alias"
          >
            {{ auth.user?.alias }}
          </span>
          <button
            class="text-sm px-3 py-1.5 rounded-lg text-text-secondary hover:text-text-primary hover:bg-surface-2 transition-colors disabled:opacity-50"
            :disabled="auth.loading"
            @click="handleLogout"
          >
            {{ auth.loading ? '...' : 'Abmelden' }}
          </button>
        </template>
        <template v-else>
          <RouterLink
            to="/login"
            class="text-sm px-3 py-1.5 rounded-lg text-text-secondary hover:text-text-primary hover:bg-surface-2 transition-colors"
          >
            Anmelden
          </RouterLink>
          <RouterLink
            to="/register"
            class="text-sm px-3 py-1.5 rounded-lg bg-primary text-surface-0 hover:bg-primary-hover transition-colors"
          >
            Registrieren
          </RouterLink>
        </template>

        <!-- Mobile menu button -->
        <button
          class="md:hidden flex items-center justify-center w-touch h-touch rounded-lg hover:bg-surface-2 transition-colors"
          :aria-expanded="mobileMenuOpen"
          aria-label="Menü öffnen"
          @click="mobileMenuOpen = !mobileMenuOpen"
        >
          <svg class="w-5 h-5 text-text-primary" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path
              v-if="!mobileMenuOpen"
              stroke-linecap="round"
              stroke-linejoin="round"
              stroke-width="2"
              d="M4 6h16M4 12h16M4 18h16"
            />
            <path
              v-else
              stroke-linecap="round"
              stroke-linejoin="round"
              stroke-width="2"
              d="M6 18L18 6M6 6l12 12"
            />
          </svg>
        </button>
      </div>
    </div>

    <!-- Mobile nav dropdown -->
    <Transition name="slide-down">
      <nav
        v-if="mobileMenuOpen"
        class="md:hidden border-t border-surface-3 bg-surface-1 px-4 py-2"
        aria-label="Mobile Navigation"
      >
        <RouterLink
          v-for="link in visibleNavLinks"
          :key="link.to"
          :to="link.to"
          class="flex items-center gap-2 px-3 py-3 rounded-lg text-sm text-text-secondary hover:text-text-primary hover:bg-surface-2 transition-colors"
          active-class="!text-primary !bg-primary-muted/20"
          @click="mobileMenuOpen = false"
        >
          <span aria-hidden="true">{{ link.icon }}</span>
          <span>{{ link.label }}</span>
        </RouterLink>
        <!-- Mobile badge -->
        <div class="sm:hidden px-3 py-2 text-xs text-text-muted">
          Tippspiel &mdash; kein Echtgeld
        </div>
      </nav>
    </Transition>
  </header>
</template>

<style scoped>
.slide-down-enter-active {
  transition: all 0.2s ease-out;
}
.slide-down-leave-active {
  transition: all 0.15s ease-in;
}
.slide-down-enter-from,
.slide-down-leave-to {
  opacity: 0;
  transform: translateY(-0.5rem);
}
</style>
