"use client";

import { useEffect } from "react";

import { connectLiveSocket } from "@/lib/websocket";
import { useBotStore } from "@/store/useBotStore";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const LIVE_WS_TOKEN = process.env.NEXT_PUBLIC_LIVE_WS_TOKEN;

export function LiveSocketBridge() {
  const setRuntime = useBotStore((s) => s.setRuntime);

  useEffect(() => {
    const wsBase = `${API_BASE.replace("http", "ws")}/ws/live`;
    const wsUrl = LIVE_WS_TOKEN ? `${wsBase}?token=${encodeURIComponent(LIVE_WS_TOKEN)}` : wsBase;
    const socket = connectLiveSocket(wsUrl, (event) => {
      if (event?.payload?.latency_ms) setRuntime(event.payload.latency_ms);
    });

    const heartbeat = setInterval(() => {
      if (socket.readyState === 1) socket.send("ping");
    }, 10000);

    return () => {
      clearInterval(heartbeat);
      socket.close();
    };
  }, [setRuntime]);

  return null;
}
