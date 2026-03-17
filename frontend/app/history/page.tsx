"use client";

import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/table";
import { detailedTrades } from "@/lib/mock-data";

export default function HistoryPage() {
  const [side, setSide] = useState("ALL");
  const [status, setStatus] = useState("ALL");

  const rows = useMemo(() => {
    return detailedTrades.filter((r) => (side === "ALL" ? true : r.side === side) && (status === "ALL" ? true : r.status === status));
  }, [side, status]);

  return (
    <div className="rounded-3xl border border-zinc-800 bg-zinc-900/60 p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <label className="text-xs text-zinc-400">
          <span className="mr-2">Side</span>
          <select className="rounded-2xl border border-zinc-700 bg-zinc-900 px-3 py-1 text-xs" value={side} onChange={(e) => setSide(e.target.value)}>
            <option>ALL</option><option>BUY</option><option>SELL</option>
          </select>
        </label>
        <label className="text-xs text-zinc-400">
          <span className="mr-2">Status</span>
          <select className="rounded-2xl border border-zinc-700 bg-zinc-900 px-3 py-1 text-xs" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option>ALL</option><option>FILLED</option><option>CANCELLED</option>
          </select>
        </label>
        <Button className="ml-auto">Export CSV</Button>
        <Button>Export JSON</Button>
      </div>
      {rows.length === 0 ? <p className="py-6 text-sm text-zinc-400">No trades found for this filter.</p> : null}
      <div className="overflow-x-auto">
        <Table className="min-w-[1300px]">
          <THead>
            <Tr>
              <Th>Timestamp</Th><Th>Market Title</Th><Th>Condition ID</Th><Th>Token ID</Th><Th>Side</Th><Th>Size</Th><Th>Entry Price</Th><Th>Implied Entry</Th><Th>Trigger Type</Th><Th>Kelly</Th><Th>Risk %</Th><Th>Est Profit Raw</Th><Th>Est Profit Adj</Th><Th>Slippage</Th><Th>Fees</Th><Th>Tx Hash</Th><Th>Status</Th><Th>Exec Latency</Th><Th>Post-Trade PnL Δ</Th>
            </Tr>
          </THead>
          <TBody>
            {rows.map((r) => (
              <Tr key={`${r.conditionId}-${r.timestamp}`}>
                <Td>{r.timestamp}</Td><Td>{r.marketTitle}</Td><Td className="font-mono">{r.conditionId}</Td><Td className="font-mono">{r.tokenId}</Td><Td>{r.side}</Td>
                <Td>{r.sizeShares} / ${r.sizeUsd}</Td><Td>{r.entryPrice}</Td><Td>{r.impliedEntry}</Td><Td>{r.trigger}</Td><Td>{r.kelly}</Td>
                <Td>{r.riskPct}%</Td><Td>{r.estProfitRaw}%</Td><Td>{r.estProfitAdj}%</Td><Td>{r.slippage}</Td><Td>{r.fees}</Td>
                <Td className="font-mono">{r.txHash}</Td><Td>{r.status}</Td><Td>{r.latency}</Td><Td className={r.postDelta >= 0 ? "text-emerald-300" : "text-rose-300"}>{r.postDelta.toFixed(2)}%</Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </div>
    </div>
  );
}
