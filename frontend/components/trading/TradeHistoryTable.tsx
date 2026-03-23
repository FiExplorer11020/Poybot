import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/table";
import { apiHeaders, apiUrl } from "@/lib/api";
import { Trade } from "@/store/useBotStore";
import { ArrowUpRight, ArrowDownRight, Clock } from "lucide-react";

export function TradeHistoryTable({ rows }: { rows: Trade[] }) {
  const [loadingId, setLoadingId] = useState<string | null>(null);

  const handleClose = async (id: string) => {
    setLoadingId(id);
    try {
      const resp = await fetch(apiUrl(`/api/v1/trades/${id}/close`), {
        method: "POST",
        headers: apiHeaders(),
      });
      if (!resp.ok) console.error("Failed to close position");
    } catch (e) {
      console.error(e);
    } finally {
      setLoadingId(null);
    }
  };

  if (!rows || rows.length === 0) {
    return <div className="p-10 text-center text-zinc-500 font-mono text-xs tracking-wider">NO POSITIONS YET. BOT IS ANALYZING.</div>;
  }

  return (
    <div className="overflow-x-auto w-full">
      <Table className="w-full text-sm">
        <THead className="bg-zinc-900/30 border-b border-zinc-800 text-xs">
          <Tr>
            <Th className="font-semibold text-zinc-400 uppercase tracking-wider py-4 pl-6">Time</Th>
            <Th className="font-semibold text-zinc-400 uppercase tracking-wider min-w-[250px]">Position</Th>
            <Th className="font-semibold text-zinc-400 uppercase tracking-wider">Side</Th>
            <Th className="font-semibold text-zinc-400 uppercase tracking-wider">Entry & Size</Th>
            <Th className="font-semibold text-zinc-400 uppercase tracking-wider">Status</Th>
            <Th className="font-semibold text-zinc-400 uppercase tracking-wider text-right">PnL</Th>
            <Th className="font-semibold text-zinc-400 uppercase tracking-wider text-right pr-6">Action</Th>
          </Tr>
        </THead>
        <TBody>
          {rows.map((row) => {
            const isWin = row.pnl_abs > 0;
            const isLoss = row.pnl_abs < 0;
            return (
              <Tr key={row.id} className="border-b border-zinc-800/40 hover:bg-zinc-800/20 transition-colors">
                <Td className="font-mono text-[11px] text-zinc-500 pl-6"><span className="flex items-center gap-1.5"><Clock size={12} className="text-zinc-600"/>{new Date(row.timestamp).toLocaleTimeString()}</span></Td>
                <Td className="text-zinc-200 font-medium text-[13px] truncate max-w-[300px]" title={row.market_title}>{row.market_title}</Td>
                <Td>
                  <Badge className={`font-mono text-[10px] uppercase tracking-wider px-2 py-0.5 ${row.side.includes("YES") ? "border-emerald-500/30 bg-emerald-500/5 text-emerald-400" : "border-rose-500/30 bg-rose-500/5 text-rose-400"}`}>
                    {row.side.replace("BUY_", "")}
                  </Badge>
                </Td>
                <Td className="font-mono text-zinc-300 text-xs">{row.size.toFixed(2)} <span className="text-zinc-600 px-1">@</span> <span className="text-zinc-400">${row.price.toFixed(3)}</span></Td>
                <Td>
                  <Badge className={`font-mono text-[10px] tracking-wider uppercase px-2 py-0.5 ${row.status === "OPEN" ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20" : "bg-zinc-800 text-zinc-400 border-zinc-700"}`}>
                    {row.status}
                  </Badge>
                </Td>
                <Td className="text-right font-mono text-xs">
                  <span className={`flex items-center justify-end gap-1 font-semibold ${isWin ? 'text-emerald-400' : isLoss ? 'text-rose-400' : 'text-zinc-500'}`}>
                    {isWin && <ArrowUpRight size={14}/>}
                    {isLoss && <ArrowDownRight size={14}/>}
                    ${Math.abs(row.pnl_abs).toFixed(2)}
                  </span>
                </Td>
                <Td className="text-right pr-6">
                  {row.status === "OPEN" ? (
                    <button 
                      onClick={() => handleClose(row.id)} 
                      disabled={loadingId === row.id}
                      className="px-2 py-1 rounded-md h-6 text-[10px] text-rose-400 border border-rose-500/20 hover:bg-rose-500/20 w-[60px] cursor-pointer disabled:opacity-50"
                    >
                      {loadingId === row.id ? "..." : "Close"}
                    </button>
                  ) : (
                    <span className="text-zinc-600 font-mono text-[10px] uppercase">Closed</span>
                  )}
                </Td>
              </Tr>
            );
          })}
        </TBody>
      </Table>
    </div>
  );
}
