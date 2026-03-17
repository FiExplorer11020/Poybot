import { Badge } from "@/components/ui/badge";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/table";

type TradeRow = {
  timestamp: string;
  market: string;
  side: string;
  size: string;
  entry: string;
  kelly: string;
  risk: string;
  status: string;
  latency: string;
};

export function TradeHistoryTable({ rows }: { rows: TradeRow[] }) {
  return (
    <div className="rounded-3xl border border-zinc-800 bg-zinc-900/60 p-3">
      <div className="overflow-x-auto">
        <Table className="min-w-[760px]">
          <THead>
            <Tr>
              <Th>Timestamp (ms)</Th><Th>Market</Th><Th>Side</Th><Th>Size</Th><Th>Entry Price</Th><Th>Kelly</Th><Th>Risk %</Th><Th>Status</Th><Th>Latency</Th>
            </Tr>
          </THead>
          <TBody>
            {rows.map((row) => (
              <Tr key={`${row.timestamp}-${row.market}`}>
                <Td>{row.timestamp}</Td>
                <Td>{row.market}</Td>
                <Td>{row.side}</Td>
                <Td>{row.size}</Td>
                <Td>{row.entry}</Td>
                <Td>{row.kelly}</Td>
                <Td>{row.risk}</Td>
                <Td>
                  <Badge className={row.status === "FILLED" ? "border-emerald-400/50 text-emerald-300" : "border-amber-500/50 text-amber-300"}>{row.status}</Badge>
                </Td>
                <Td>{row.latency}</Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </div>
    </div>
  );
}
