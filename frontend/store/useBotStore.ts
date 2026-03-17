import { create } from "zustand";

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
  setWallet: (address?: string, balance?: number) => void;
  setRuntime: (latency: number) => void;
};

export const useBotStore = create<BotState>((set) => ({
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
  setWallet: (walletAddress, walletBalance = 0) => set({ walletAddress, walletBalance }),
  setRuntime: (latencyMs) => set({ latencyMs })
}));
