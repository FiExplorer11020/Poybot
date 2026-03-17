"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { connectAndAuthenticateWallet, fetchWalletBalance } from "@/lib/wallet";
import { truncateAddress } from "@/lib/utils";
import { useBotStore } from "@/store/useBotStore";

export function WalletButton() {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const walletAddress = useBotStore((s) => s.walletAddress);
  const walletBalance = useBotStore((s) => s.walletBalance);
  const walletToken = useBotStore((s) => s.walletToken);
  const walletConnecting = useBotStore((s) => s.walletConnecting);
  const setWallet = useBotStore((s) => s.setWallet);
  const setWalletConnecting = useBotStore((s) => s.setWalletConnecting);
  const isConnected = Boolean(walletAddress && walletToken);

  const connectWallet = async () => {
    setError(null);
    setWalletConnecting(true);
    try {
      const session = await connectAndAuthenticateWallet();
      const balance = await fetchWalletBalance(session.address);
      setWallet(session.address, balance, session.token);
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Connexion wallet impossible.");
    } finally {
      setWalletConnecting(false);
    }
  };

  if (isConnected) {
    return (
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs text-zinc-300">{truncateAddress(walletAddress)} • {walletBalance.toFixed(4)} MATIC</span>
        <Button onClick={() => setWallet(undefined, 0, undefined)} className="text-xs">Disconnect</Button>
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
          <p className="text-xs text-zinc-400">Authentification wallet via signature (nonce backend).</p>
          <Button className="w-full" onClick={connectWallet} disabled={walletConnecting}>
            {walletConnecting ? "Connexion..." : "Connecter MetaMask / Wallet EVM"}
          </Button>
          {error ? <p className="text-xs text-amber-300">{error}</p> : null}
          <Button className="w-full" onClick={() => setOpen(false)}>Cancel</Button>
        </div>
      </Dialog>
    </>
  );
}
