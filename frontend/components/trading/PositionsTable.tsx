import { Button } from "@/components/ui/button";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/table";

const positions = Array.from({ length: 10 }).map((_, i) => ({
  market: `Market ${i + 1}`,
  outcome: i % 2 ? "YES" : "NO",
  size: (110 + i * 22).toFixed(2),
  entry: (0.33 + i * 0.02).toFixed(3),
  current: (0.35 + i * 0.02).toFixed(3),
  pnl: (i % 2 ? 1 : -1) * (0.6 + i * 0.8),
  exposure: (2 + i * 0.9).toFixed(2),
  risk: (0.8 + i * 0.25).toFixed(2)
}));

export function PositionsTable() {
  return (
    <Table>
      <THead>
        <Tr><Th>Market</Th><Th>Outcome</Th><Th>Size USDC</Th><Th>Entry</Th><Th>Current</Th><Th>Unrealized P&L</Th><Th>Exposure %</Th><Th>Risk %</Th><Th /></Tr>
      </THead>
      <TBody>
        {positions.map((p) => (
          <Tr key={p.market}>
            <Td>{p.market}</Td><Td>{p.outcome}</Td><Td>{p.size}</Td><Td>{p.entry}</Td><Td>{p.current}</Td>
            <Td className={p.pnl >= 0 ? "text-emerald-300" : "text-rose-300"}>{p.pnl.toFixed(2)}%</Td>
            <Td>{p.exposure}</Td><Td>{p.risk}</Td><Td><Button className="text-xs">Close</Button></Td>
          </Tr>
        ))}
      </TBody>
    </Table>
  );
}
