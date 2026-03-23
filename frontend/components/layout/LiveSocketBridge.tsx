"use client";

import { useEffect, useRef } from "react";
import { toast } from "sonner";

import { apiHeaders, API_BASE } from "@/lib/api";
import { connectLiveSocket } from "@/lib/websocket";
import { useLiveStore } from "@/store/useLiveStore";

const LIVE_WS_TOKEN = process.env.NEXT_PUBLIC_LIVE_WS_TOKEN?.trim();
const MAX_RECONNECT_DELAY_MS = 15_000;
const INITIAL_RECONNECT_DELAY_MS = 1_000;

type LiveEvent = {
  type?: string;
  payload?: Record<string, unknown>;
  reason?: unknown;
  details?: unknown;
};

const snapshotUrl = `${API_BASE}/api/v1/live-summary`;

export function LiveSocketBridge() {
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectAttemptRef = useRef(0);

  useEffect(() => {
    let active = true;

    const store = useLiveStore.getState();

    const loadSnapshot = async () => {
      try {
        const response = await fetch(snapshotUrl, {
          headers: apiHeaders(),
        });
        if (!response.ok) {
          throw new Error(`Bootstrap failed with status ${response.status}`);
        }

        const data = await response.json();
        if (active && data?.data) {
          useLiveStore.getState().processBootstrap(data.data);
        }
      } catch (error) {
        console.error("live-summary bootstrap failed", error);
      }
    };

    const clearReconnectTimer = () => {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = undefined;
      }
    };

    const scheduleReconnect = () => {
      if (!active) {
        return;
      }

      reconnectAttemptRef.current += 1;
      const delay = Math.min(
        INITIAL_RECONNECT_DELAY_MS * 2 ** (reconnectAttemptRef.current - 1),
        MAX_RECONNECT_DELAY_MS
      );

      useLiveStore.getState().setConnectionState("reconnecting", reconnectAttemptRef.current);

      clearReconnectTimer();
      reconnectTimerRef.current = setTimeout(() => {
        connect();
      }, delay);
    };

    const handleEvent = (event: LiveEvent) => {
      if (!active) {
        return;
      }

      const liveStore = useLiveStore.getState();

      switch (event?.type) {
        case "bootstrap":
        case "control": {
          if (event.payload) {
            liveStore.processBootstrap(event.payload);
          }
          break;
        }
        case "tick": {
          if (event.payload) {
            liveStore.processTick(event.payload);
          }
          break;
        }
        case "trade":
        case "trade_closed": {
          if (!event.payload) {
            break;
          }

          const tradeLabel = `${String(event.payload.side ?? "TRADE")} ${String(event.payload.market_title ?? "")}`;
          liveStore.processTrade(event.payload);
          toast.success(tradeLabel.trim(), {
            description: `${Number(event.payload.notional ?? 0).toFixed(2)} USD notional`,
          });
          break;
        }
        case "halt": {
          const haltPayload = event.payload ?? {
            reason: event.reason,
            details: event.details,
          };
          liveStore.processHalt(haltPayload);
          toast.error(`Kill switch: ${String(haltPayload.reason ?? "halt")}`, {
            description:
              typeof haltPayload.details === "string"
                ? haltPayload.details
                : haltPayload.details
                  ? JSON.stringify(haltPayload.details)
                  : "Trading halted by backend guardrail.",
            duration: 10_000,
          });
          break;
        }
        default:
          break;
      }
    };

    const connect = () => {
      if (!active) {
        return;
      }

      const wsBase = `${API_BASE.replace(/^http/, "ws")}/ws/live`;
      const wsUrl = LIVE_WS_TOKEN ? `${wsBase}?token=${encodeURIComponent(LIVE_WS_TOKEN)}` : wsBase;

      try {
        socketRef.current = connectLiveSocket(wsUrl, handleEvent);
      } catch (error) {
        console.error("live websocket connection failed", error);
        scheduleReconnect();
        return;
      }

      socketRef.current.onopen = () => {
        reconnectAttemptRef.current = 0;
        useLiveStore.getState().setConnectionState("connected", 0);
      };

      socketRef.current.onerror = (error) => {
        console.error("live websocket error", error);
      };

      socketRef.current.onclose = () => {
        if (!active) {
          useLiveStore.getState().setConnectionState("disconnected", 0);
          return;
        }

        scheduleReconnect();
      };
    };

    store.setConnectionState("reconnecting", 0);
    loadSnapshot();
    connect();

    return () => {
      active = false;
      clearReconnectTimer();
      useLiveStore.getState().setConnectionState("disconnected", 0);
      socketRef.current?.close();
    };
  }, []);

  return null;
}
