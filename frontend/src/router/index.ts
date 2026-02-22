import { createRouter, createWebHistory } from "vue-router";
import { useAuthStore } from "@/stores/auth";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/",
      name: "dashboard",
      component: () => import("@/views/DashboardView.vue"),
    },
    {
      path: "/login",
      name: "login",
      component: () => import("@/views/LoginView.vue"),
      meta: { guest: true },
    },
    {
      path: "/register",
      name: "register",
      component: () => import("@/views/RegisterView.vue"),
      meta: { guest: true },
    },
    {
      path: "/complete-profile",
      name: "complete-profile",
      component: () => import("@/views/CompleteProfileView.vue"),
      meta: { requiresAuth: true },
    },
    {
      path: "/leaderboard",
      name: "leaderboard",
      component: () => import("@/views/LeaderboardView.vue"),
    },
    {
      path: "/squads",
      name: "squads",
      component: () => import("@/views/SquadsView.vue"),
      meta: { requiresAuth: true },
    },
    {
      path: "/squads/:id",
      name: "squad-detail",
      component: () => import("@/views/SquadDetailView.vue"),
      meta: { requiresAuth: true },
    },
    {
      path: "/battles",
      name: "battles",
      component: () => import("@/views/BattleCenterView.vue"),
      meta: { requiresAuth: true },
    },
    {
      path: "/spieltag/:sport?/:matchday?",
      name: "spieltag",
      component: () => import("@/views/SpieltagView.vue"),
    },
    {
      path: "/settings",
      name: "settings",
      component: () => import("@/views/SettingsView.vue"),
      meta: { requiresAuth: true },
    },
    // Legal
    {
      path: "/legal/:section",
      name: "legal",
      component: () => import("@/views/LegalView.vue"),
    },
    { path: "/impressum", redirect: "/legal/impressum" },
    { path: "/datenschutz", redirect: "/legal/datenschutz" },
    { path: "/agb", redirect: "/legal/agb" },
    { path: "/jugendschutz", redirect: "/legal/jugendschutz" },
    // Admin routes
    {
      path: "/admin",
      name: "admin",
      component: () => import("@/views/admin/AdminDashboardView.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/matches",
      name: "admin-matches",
      component: () => import("@/views/admin/AdminMatchManager.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/users",
      name: "admin-users",
      component: () => import("@/views/admin/AdminUserManager.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/battles",
      name: "admin-battles",
      component: () => import("@/views/admin/AdminBattleManager.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/audit",
      name: "admin-audit",
      component: () => import("@/views/admin/AdminAuditLog.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
  ],
});

router.beforeEach((to) => {
  const auth = useAuthStore();

  // Auth guard: redirect unauthenticated users to login
  if (to.meta.requiresAuth && !auth.isLoggedIn) {
    return { name: "login", query: { redirect: to.fullPath } };
  }

  // Guest guard: redirect authenticated users away from login/register
  if (to.meta.guest && auth.isLoggedIn) {
    return { name: "dashboard" };
  }

  // Admin guard: redirect non-admins to dashboard
  if (to.meta.requiresAdmin && !auth.isAdmin) {
    return { name: "dashboard" };
  }

  // Age gate: redirect users who haven't completed profile
  if (
    auth.needsProfileCompletion &&
    to.name !== "complete-profile" &&
    to.name !== "login" &&
    to.name !== "register" &&
    to.name !== "legal"
  ) {
    return { name: "complete-profile" };
  }
});

export default router;
