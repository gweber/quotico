import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { useApi } from "@/composables/useApi";

export interface User {
  email: string;
  alias: string;
  alias_slug: string;
  has_custom_alias: boolean;
  points: number;
  is_admin: boolean;
  is_2fa_enabled: boolean;
  created_at: string;
}

export const useAuthStore = defineStore("auth", () => {
  const api = useApi();
  const user = ref<User | null>(null);
  const loading = ref(false);

  const isLoggedIn = computed(() => user.value !== null);
  const isAdmin = computed(() => user.value?.is_admin === true);

  async function fetchUser() {
    try {
      user.value = await api.get<User>("/auth/me");
    } catch {
      user.value = null;
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

  async function register(email: string, password: string) {
    loading.value = true;
    try {
      await api.post("/auth/register", { email, password });
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
    await api.post("/auth/logout");
    user.value = null;
  }

  return { user, loading, isLoggedIn, isAdmin, fetchUser, login, login2fa, register, logout };
});
