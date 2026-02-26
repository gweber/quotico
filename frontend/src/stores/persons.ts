/**
 * frontend/src/stores/persons.ts
 *
 * Purpose:
 * Lightweight person cache for batch-resolving referee/player IDs to display
 * names in match cards without N+1 requests.
 *
 * Uses microtask batching: concurrent resolveByIds() calls within the same
 * tick are collected into a single POST /v3/persons/batch request.
 */

import { defineStore } from "pinia";
import { ref } from "vue";
import { useApi } from "@/composables/useApi";

interface PersonItem {
  id: number;
  type: string;
  name: string;
  common_name: string;
  image_path: string;
}

interface PersonCacheRow {
  value: PersonItem;
  cachedAt: number;
}

const PERSON_CACHE_TTL_MS = 30 * 60 * 1000;

export const usePersonsStore = defineStore("persons", () => {
  const api = useApi();
  const byId = ref<Map<number, PersonCacheRow>>(new Map());
  const pending = new Set<number>();
  let batchPromise: Promise<void> | null = null;

  function getPersonName(personId?: number | null): string | null {
    if (!personId || !byId.value.has(personId)) return null;
    const row = byId.value.get(personId);
    if (!row) return null;
    const now = Date.now();
    if (now - row.cachedAt > PERSON_CACHE_TTL_MS) return null;
    const common = String(row.value.common_name || "").trim();
    const name = String(row.value.name || "").trim();
    return common || name || null;
  }

  function flush(): Promise<void> {
    const ids = Array.from(pending);
    pending.clear();
    if (!ids.length) return Promise.resolve();
    return (async () => {
      const response = await api.post<{ items: PersonItem[] }>("/v3/persons/batch", { ids });
      const loaded = response.items || [];
      const stamp = Date.now();
      loaded.forEach((row) => {
        if (!row || typeof row.id !== "number") return;
        byId.value.set(row.id, { value: row, cachedAt: stamp });
      });
    })();
  }

  async function resolveByIds(ids: number[]): Promise<void> {
    const now = Date.now();
    const missing = ids
      .map(Number)
      .filter((id) => {
        if (!Number.isFinite(id) || id <= 0) return false;
        const cached = byId.value.get(id);
        return !cached || (now - cached.cachedAt > PERSON_CACHE_TTL_MS);
      });
    if (!missing.length) return;

    missing.forEach((id) => pending.add(id));

    if (!batchPromise) {
      // Schedule flush on next microtask so all concurrent callers queue first
      batchPromise = Promise.resolve().then(async () => {
        try {
          await flush();
        } finally {
          batchPromise = null;
        }
      });
    }

    await batchPromise;
  }

  return {
    resolveByIds,
    getPersonName,
  };
});
