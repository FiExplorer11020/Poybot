"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { truncateAddress } from "@/lib/utils";
import { useBotStore } from "@/store/useBotStore";

export function WalletButton() {
  const [open, setOpen] = useState(false);
  const walletAddress = useBotStore((s) => s.walletAddress);
  const setWallet = useBotStore((s) => s.setWallet);
  const walletBalance = useBotStore((s) => s.walletBalance);
  const isConnected = Boolean(walletAddress);

  const connectWallet = (label: string) => {
    const mockAddress = label === "MetaMask" ? "0xA11ce00000000000000000000000000000B0B01" : "0xB0b00000000000000000000000000000000C0DE";
    setWallet(mockAddress, 1200);
    setOpen(false);
  };

  if (isConnected) {
    return (
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs text-zinc-300">{truncateAddress(walletAddress)} • {walletBalance.toFixed(2)} USDC</span>
        <Button onClick={() => setWallet(undefined, 0)} className="text-xs">Disconnect</Button>
      </div>
    );
  }

  return (
    <>
      <Button className="flex items-center gap-2" onClick={() => setOpen(true)}>
        Connect Wallet
      </Button>
      <Dialog open={open} onClose={() => setOpen(false)}>
        <div className="space-y-4 pt-2">
          <h3 className="font-sans text-lg">Connect Wallet</h3>
          <p className="text-xs text-zinc-400">Network: Polygon</p>
          <Button className="w-full" onClick={() => connectWallet("MetaMask")}>MetaMask</Button>
          <Button className="w-full" onClick={() => connectWallet("WalletConnect")}>WalletConnect</Button>
          <Button className="w-full" onClick={() => setOpen(false)}>Cancel</Button>
        </div>
      </Dialog>
    </>
  );
}
