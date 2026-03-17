import { getDefaultConfig } from "@rainbow-me/rainbowkit";
import { http } from "viem";
import { polygon } from "viem/chains";

export const wagmiConfig = getDefaultConfig({
  appName: "ARB BOT",
  projectId: process.env.NEXT_PUBLIC_WALLETCONNECT_PROJECT_ID ?? "demo-project-id",
  chains: [polygon],
  transports: {
    [polygon.id]: http()
  },
  ssr: true
});
