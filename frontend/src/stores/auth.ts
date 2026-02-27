/**
 * frontend/src/stores/auth.ts
 *
 * Purpose:
 * Auth/session state including persona-governance fields returned by /auth/me.
 */

import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { useApi } from "@/composables/useApi";
import type { TipPersona, TipPersonaSource } from "@/types/persona";

export interface User {
  email: string;
  alias: string;
  alias_slug: string;
  has_custom_alias: boolean;
  points: number;
  is_admin: boolean;
  is_2fa_enabled: boolean;
  is_adult: boolean;
  terms_accepted_version: string | null;
  tip_persona: TipPersona;
  tip_persona_effective: TipPersona;
  tip_persona_source: TipPersonaSource;
  tip_persona_updated_at: string | null;
  tip_override_persona: TipPersona | null;
  created_at: string;
}

export const useAuthStore = defineStore("auth", () => {
  const api = useApi();
  const user = ref<User | null>(null);
  const loading = ref(false);
  const initialized = ref(false);

  // Promise that resolves once the first fetchUser() completes.
  // Router guards await this before checking auth state.
  let _initResolve: (() => void) | null = null;
  const initPromise = new Promise<void>((resolve) => {
    _initResolve = resolve;
  });

  const isLoggedIn = computed(() => user.value !== null);
  const isAdmin = computed(() => user.value?.is_admin === true);
  const needsProfileCompletion = computed(
    () => user.value !== null && !user.value.is_adult
  );

  async function fetchUser() {
    try {
      user.value = await api.get<User>("/auth/me");
    } catch {
      user.value = null;
    } finally {
      if (!initialized.value) {
        initialized.value = true;
        _initResolve?.();
      }
    }
  }

  async function login(email: string, password: string) {
    loading.value = true;
    try {
      const result = await api.post<{ message?: string; requires_2fa?: boolean }>(
        "/auth/login",
        { email, password }
      );
      if (result.requires_2fa) {
        return { requires2fa: true };
      }
      await fetchUser();
      return { requires2fa: false };
    } finally {
      loading.value = false;
    }
  }

  async function register(
    email: string,
    password: string,
    birthDate: string,
    disclaimerAccepted: boolean
  ) {
    loading.value = true;
    try {
      await api.post("/auth/register", {
        email,
        password,
        birth_date: birthDate,
        disclaimer_accepted: disclaimerAccepted,
      });
      await fetchUser();
    } finally {
      loading.value = false;
    }
  }

  async function completeProfile(birthDate: string, disclaimerAccepted: boolean) {
    loading.value = true;
    try {
      await api.post("/auth/complete-profile", {
        birth_date: birthDate,
        disclaimer_accepted: disclaimerAccepted,
      });
      await fetchUser();
    } finally {
      loading.value = false;
    }
  }

  async function login2fa(email: string, password: string, code: string) {
    loading.value = true;
    try {
      await api.post("/auth/login/2fa", { email, password, code });
      await fetchUser();
    } finally {
      loading.value = false;
    }
  }

  async function logout() {
    try {
      await api.post("/auth/logout");
    } catch {
      // Server-side logout failed — clear local state anyway
    }
    user.value = null;
  }

  async function updateTipPersona(tip_persona: TipPersona) {
    await api.patch("/user/tip-persona", { tip_persona });
    await fetchUser();
  }

  return {
    user, loading, initialized, initPromise,
    isLoggedIn, isAdmin, needsProfileCompletion,
    fetchUser, login, login2fa, register, completeProfile, logout, updateTipPersona,
  };
});
