import { useSyncExternalStore } from "react";

type BotState = {
  status: "LIVE" | "PAUSED";
  uptime: string;
  latencyMs: number;
  totalPnl: number;
  totalPnlPct: number;
  activePositions: number;
  portfolioTotal: number;
  capitalInTrade: number;
  walletAddress?: string;
  walletBalance: number;
  walletToken?: string;
  walletConnecting: boolean;
  setWallet: (address?: string, balance?: number, token?: string) => void;
  setWalletConnecting: (value: boolean) => void;
  setRuntime: (latency: number) => void;
};

const listeners = new Set<() => void>();

const state: BotState = {
  status: "LIVE",
  uptime: "03:41:12",
  latencyMs: 61,
  totalPnl: 2845.34,
  totalPnlPct: 11.42,
  activePositions: 12,
  portfolioTotal: 24877.45,
  capitalInTrade: 9321.11,
  walletAddress: undefined,
  walletBalance: 0,
  walletToken: undefined,
  walletConnecting: false,
  setWallet: (walletAddress, walletBalance = 0, walletToken) => setState({ walletAddress, walletBalance, walletToken }),
  setWalletConnecting: (walletConnecting) => setState({ walletConnecting }),
  setRuntime: (latencyMs) => setState({ latencyMs })
};

function setState(partial: Partial<BotState>) {
  Object.assign(state, partial);
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot() {
  return state;
}

export function useBotStore(): BotState;
export function useBotStore<T>(selector: (state: BotState) => T): T;
export function useBotStore<T>(selector?: (state: BotState) => T) {
  const select = selector ?? ((value: BotState) => value as unknown as T);
  return useSyncExternalStore(subscribe, () => select(getSnapshot()), () => select(getSnapshot()));
}
