import { defineStore } from "pinia";
import { ref } from "vue";
import { useApi } from "@/composables/useApi";

export interface Wallet {
  id: string;
  squad_id: string;
  league_id: number;
  season: number;
  balance: number;
  initial_balance: number;
  total_wagered: number;
  total_won: number;
  status: string;
  bankrupt_since: string | null;
  consecutive_bonus_days: number;
}

export interface BankrollBet {
  id: string;
  match_id: string;
  prediction: string;
  stake: number;
  locked_odds: number;
  potential_win: number;
  status: string;
  points_earned: number | null;
  resolved_at: string | null;
  created_at: string;
}

export interface OverUnderBet {
  id: string;
  match_id: string;
  prediction: string;
  line: number;
  locked_odds: number;
  stake: number | null;
  status: string;
  points_earned: number | null;
  created_at: string;
}

export interface WalletTransaction {
  id: string;
  type: string;
  amount: number;
  balance_after: number;
  description: string;
  created_at: string;
}

export interface Parlay {
  id: string;
  matchday_id: string;
  legs: ParlayLeg[];
  combined_odds: number;
  stake: number | null;
  potential_win: number;
  status: string;
  points_earned: number | null;
  created_at: string;
}

export interface ParlayLeg {
  match_id: string;
  prediction: string;
  locked_odds: number;
  result: string;
}

export const useWalletStore = defineStore("wallet", () => {
  const api = useApi();

  const wallet = ref<Wallet | null>(null);
  const bets = ref<BankrollBet[]>([]);
  const overUnderBets = ref<OverUnderBet[]>([]);
  const transactions = ref<WalletTransaction[]>([]);
  const parlay = ref<Parlay | null>(null);
  const loading = ref(false);
  const disclaimerAccepted = ref(false);

  async function fetchWallet(squadId: string, leagueId: number, season?: number) {
    loading.value = true;
    try {
      const params: Record<string, string> = { league_id: String(leagueId) };
      if (season) params.season = String(season);
      wallet.value = await api.get<Wallet>(`/wallet/${squadId}`, params);
    } catch {
      wallet.value = null;
    } finally {
      loading.value = false;
    }
  }

  async function fetchBets(squadId: string, matchdayId?: string) {
    try {
      const params: Record<string, string> = {};
      if (matchdayId) params.matchday_id = matchdayId;
      bets.value = await api.get<BankrollBet[]>(`/wallet/${squadId}/bets`, params);
    } catch {
      bets.value = [];
    }
  }

  async function placeBet(
    squadId: string,
    matchId: string,
    prediction: string,
    stake: number,
    displayedOdds: number,
  ): Promise<BankrollBet> {
    const bet = await api.post<BankrollBet>(`/wallet/${squadId}/bet`, {
      match_id: matchId,
      prediction,
      stake,
      displayed_odds: displayedOdds,
    });
    bets.value.push(bet);
    // Update wallet balance locally
    if (wallet.value) {
      wallet.value.balance -= stake;
      wallet.value.total_wagered += stake;
    }
    return bet;
  }

  async function fetchOverUnderBets(squadId: string, matchdayId?: string) {
    try {
      const params: Record<string, string> = {};
      if (matchdayId) params.matchday_id = matchdayId;
      overUnderBets.value = await api.get<OverUnderBet[]>(
        `/wallet/${squadId}/over-under`,
        params,
      );
    } catch {
      overUnderBets.value = [];
    }
  }

  async function placeOverUnderBet(
    squadId: string,
    matchId: string,
    prediction: string,
    stake: number,
    displayedOdds: number,
  ): Promise<OverUnderBet> {
    const bet = await api.post<OverUnderBet>(`/wallet/${squadId}/over-under`, {
      match_id: matchId,
      prediction,
      stake,
      displayed_odds: displayedOdds,
    });
    overUnderBets.value.push(bet);
    if (wallet.value && stake) {
      wallet.value.balance -= stake;
      wallet.value.total_wagered += stake;
    }
    return bet;
  }

  async function fetchTransactions(squadId: string, leagueId: number, season?: number) {
    try {
      const params: Record<string, string> = { league_id: String(leagueId) };
      if (season) params.season = String(season);
      transactions.value = await api.get<WalletTransaction[]>(
        `/wallet/${squadId}/transactions`,
        params,
      );
    } catch {
      transactions.value = [];
    }
  }

  async function acceptDisclaimer() {
    try {
      await api.post("/wallet/accept-disclaimer");
      disclaimerAccepted.value = true;
    } catch {
      // Best effort — let user proceed anyway
      disclaimerAccepted.value = true;
    }
  }

  async function fetchParlay(squadId: string, matchdayId: string) {
    try {
      parlay.value = await api.get<Parlay | null>(`/parlay/${squadId}`, {
        matchday_id: matchdayId,
      });
    } catch {
      parlay.value = null;
    }
  }

  async function createParlay(
    squadId: string,
    matchdayId: string,
    legs: { match_id: string; prediction: string; displayed_odds: number }[],
    stake: number | null,
  ): Promise<Parlay> {
    const result = await api.post<Parlay>(`/parlay/${squadId}`, {
      matchday_id: matchdayId,
      legs,
      stake,
    });
    parlay.value = result;
    if (wallet.value && stake) {
      wallet.value.balance -= stake;
    }
    return result;
  }

  function reset() {
    wallet.value = null;
    bets.value = [];
    overUnderBets.value = [];
    transactions.value = [];
    parlay.value = null;
  }

  return {
    wallet,
    bets,
    overUnderBets,
    transactions,
    parlay,
    loading,
    disclaimerAccepted,
    fetchWallet,
    fetchBets,
    placeBet,
    fetchOverUnderBets,
    placeOverUnderBet,
    fetchTransactions,
    acceptDisclaimer,
    fetchParlay,
    createParlay,
    reset,
  };
});
