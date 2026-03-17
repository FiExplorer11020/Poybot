"use client";

import { useEffect } from "react";

import { connectLiveSocket } from "@/lib/websocket";
import { useBotStore } from "@/store/useBotStore";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export function LiveSocketBridge() {
  const setRuntime = useBotStore((s) => s.setRuntime);

  useEffect(() => {
    const socket = connectLiveSocket(`${API_BASE.replace("http", "ws")}/ws/live`, (event) => {
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
