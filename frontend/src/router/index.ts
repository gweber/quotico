/**
 * frontend/src/router/index.ts
 *
 * Purpose:
 *     Central Vue Router configuration with auth/admin guards and route-level lazy loading.
 */
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
      path: "/join/:code",
      name: "join-squad",
      component: () => import("@/views/JoinSquadView.vue"),
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
      path: "/squads/:id/war-room/:matchId",
      name: "squad-war-room",
      component: () => import("@/views/SquadWarRoomView.vue"),
      meta: { requiresAuth: true },
    },
    {
      path: "/battles",
      name: "battles",
      component: () => import("@/views/BattleCenterView.vue"),
      meta: { requiresAuth: true },
    },
    {
      path: "/matchday/:sport?/:matchday?",
      name: "matchday",
      component: () => import("@/views/MatchdayView.vue"),
    },
    {
      path: "/qbot",
      name: "qbot",
      component: () => import("@/views/QBotView.vue"),
    },
    {
      path: "/qtip-performance",
      name: "qtip-performance",
      component: () => import("@/views/QTipPerformanceView.vue"),
    },
    {
      path: "/settings",
      name: "settings",
      component: () => import("@/views/SettingsView.vue"),
      meta: { requiresAuth: true },
    },
    // Public analysis
    {
      path: "/analysis/:league?",
      name: "analysis",
      component: () => import("@/views/AnalysisView.vue"),
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
      name: "admin-dashboard",
      component: () => import("@/views/admin/AdminDashboardView.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/ingest",
      name: "admin-ingest",
      component: () => import("@/views/admin/AdminIngestDiscoveryView.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/users",
      name: "admin-users",
      component: () => import("@/views/admin/AdminUserManager.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/matches",
      name: "admin-matches",
      component: () => import("@/views/admin/AdminMatchManager.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/matches/:matchId",
      name: "admin-match-detail",
      component: () => import("@/views/admin/AdminMatchDetailView.vue"),
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
    {
      path: "/admin/event-bus",
      name: "admin-event-bus",
      component: () => import("@/views/admin/AdminEventBusMonitor.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/time-machine-justice",
      name: "admin-time-machine-justice",
      component: () => import("@/views/admin/AdminTimeMachineJusticeView.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/team-tower",
      name: "admin-team-tower",
      component: () => import("@/views/admin/AdminTeamTowerView.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/qbot-lab",
      name: "admin-qbot-lab",
      component: () => import("@/views/admin/QbotLabView.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/qbot/strategies/:strategyId",
      name: "admin-qbot-strategy-detail",
      component: () => import("@/views/admin/AdminQbotStrategyDetailView.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/provider-status",
      name: "admin-provider-status",
      component: () => import("@/views/admin/AdminProviderStatus.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/views",
      name: "admin-views-catalog",
      component: () => import("@/views/admin/AdminViewsCatalogView.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/leagues",
      name: "admin-leagues",
      component: () => import("@/views/admin/AdminLeagueTowerView.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/qtip-trace/:matchId",
      name: "admin-qtip-trace",
      component: () => import("@/views/admin/AdminQtipTraceView.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: "/admin/data-audit",
      name: "admin-data-audit",
      component: () => import("@/views/admin/AdminDataAuditView.vue"),
      meta: { requiresAuth: true, requiresAdmin: true },
    },
  ],
});

router.beforeEach(async (to) => {
  const auth = useAuthStore();

  // Wait for the initial auth check to complete before making guard decisions.
  // Without this, guards redirect to /login before fetchUser() resolves
  // (e.g. after Google OAuth redirect).
  if (!auth.initialized) {
    await auth.initPromise;
  }

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
    to.name !== "legal" &&
    to.name !== "join-squad" &&
    to.name !== "qbot" &&
    to.name !== "qtip-performance"
  ) {
    return { name: "complete-profile" };
  }
});

export default router;
