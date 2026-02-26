/**
 * frontend/src/stores/persons.ts
 *
 * Purpose:
 * Lightweight person cache for batch-resolving referee/player IDs to display
 * names in match cards without N+1 requests.
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
  const inFlight = ref<Promise<void> | null>(null);

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

  async function resolveByIds(ids: number[]): Promise<void> {
    const unique = Array.from(new Set(ids.map((id) => Number(id)).filter((id) => Number.isFinite(id) && id > 0)));
    if (!unique.length) return;

    const now = Date.now();
    const missing = unique.filter((id) => {
      const cached = byId.value.get(id);
      return !cached || (now - cached.cachedAt > PERSON_CACHE_TTL_MS);
    });
    if (!missing.length) return;

    if (inFlight.value) {
      await inFlight.value;
      return;
    }

    inFlight.value = (async () => {
      const response = await api.post<{ items: PersonItem[] }>("/persons/batch", { ids: missing });
      const loaded = response.items || [];
      const stamp = Date.now();
      loaded.forEach((row) => {
        if (!row || typeof row.id !== "number") return;
        byId.value.set(row.id, { value: row, cachedAt: stamp });
      });
    })();

    try {
      await inFlight.value;
    } finally {
      inFlight.value = null;
    }
  }

  return {
    resolveByIds,
    getPersonName,
  };
});

