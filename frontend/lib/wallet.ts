const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

declare global {
  interface Window {
    ethereum?: {
      request: (args: { method: string; params?: unknown[] | object }) => Promise<unknown>;
    };
  }
}

export type WalletSession = {
  token: string;
  address: string;
  expiresInSeconds: number;
};

export async function connectAndAuthenticateWallet(): Promise<WalletSession> {
  if (typeof window === "undefined" || !window.ethereum) {
    throw new Error("Aucun wallet EVM détecté. Installe MetaMask ou WalletConnect.");
  }

  const accounts = (await window.ethereum.request({ method: "eth_requestAccounts" })) as string[];
  const address = accounts?.[0];
  if (!address) {
    throw new Error("Impossible de récupérer une adresse wallet.");
  }

  const nonceRes = await fetch(`${API_BASE}/api/v1/wallet/nonce`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address })
  });
  if (!nonceRes.ok) {
    throw new Error("Le backend a refusé la génération de nonce.");
  }

  const noncePayload = (await nonceRes.json()) as { data: { message: string } };
  const message = noncePayload.data.message;
  const signature = (await window.ethereum.request({
    method: "personal_sign",
    params: [message, address]
  })) as string;

  const verifyRes = await fetch(`${API_BASE}/api/v1/wallet/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address, signature })
  });
  if (!verifyRes.ok) {
    throw new Error("La signature wallet n'a pas pu être vérifiée.");
  }

  const verifyPayload = (await verifyRes.json()) as {
    data: { token: string; address: string; expires_in_seconds: number };
  };

  return {
    token: verifyPayload.data.token,
    address: verifyPayload.data.address,
    expiresInSeconds: verifyPayload.data.expires_in_seconds
  };
}

export async function fetchWalletBalance(address: string): Promise<number> {
  if (typeof window === "undefined" || !window.ethereum) {
    return 0;
  }
  const raw = (await window.ethereum.request({
    method: "eth_getBalance",
    params: [address, "latest"]
  })) as string;
  const wei = BigInt(raw);
  return Number(wei) / 1e18;
}
