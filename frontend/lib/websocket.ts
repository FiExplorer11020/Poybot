export type LiveEventHandler = (payload: any) => void;

export const connectLiveSocket = (url: string, onEvent: LiveEventHandler) => {
  const ws = new WebSocket(url);
  ws.onmessage = (event) => {
    try {
      onEvent(JSON.parse(event.data));
    } catch {
      // noop
    }
  };
  return ws;
};
