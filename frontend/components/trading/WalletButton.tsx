"use client";

import { useState } from "react";
import { Wallet } from "lucide-react";
import { useAccount, useConnect, useDisconnect } from "wagmi";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { truncateAddress } from "@/lib/utils";
import { useBotStore } from "@/store/useBotStore";

export function WalletButton() {
  const [open, setOpen] = useState(false);
  const { address, isConnected } = useAccount();
  const { connectors, connect } = useConnect();
  const { disconnect } = useDisconnect();
  const setWallet = useBotStore((s) => s.setWallet);
  const walletBalance = useBotStore((s) => s.walletBalance);

  const onConnect = (id: string) => {
    const connector = connectors.find((c) => c.name.toLowerCase().includes(id));
    if (!connector) return;
    connect({ connector });
    setWallet(address, 1200);
    setOpen(false);
  };

  if (isConnected) {
    return (
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs text-zinc-300">{truncateAddress(address)} • {walletBalance.toFixed(2)} USDC</span>
        <Button onClick={() => { disconnect(); setWallet(undefined, 0); }} className="text-xs">Disconnect</Button>
      </div>
    );
  }

  return (
    <>
      <Button className="flex items-center gap-2" onClick={() => setOpen(true)}>
        <Wallet className="h-4 w-4" /> Connect Wallet
      </Button>
      <Dialog open={open}>
        <div className="space-y-4">
          <h3 className="font-sans text-lg">Connect Wallet</h3>
          <p className="text-xs text-zinc-400">Network: Polygon</p>
          <Button className="w-full" onClick={() => onConnect("injected")}>MetaMask</Button>
          <Button className="w-full" onClick={() => onConnect("walletconnect")}>WalletConnect</Button>
          <Button className="w-full" onClick={() => setOpen(false)}>Cancel</Button>
        </div>
      </Dialog>
    </>
  );
}
